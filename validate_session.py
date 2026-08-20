#!/usr/bin/env python3
"""Deterministic trust check for the day's selected workout.

Answers: "did the script glitch, or is this
workout actually right?" — WITHOUT an LLM. Reads the selector's output + the live data
dates and returns a GREEN / AMBER / RED badge:
  GREEN  ✅ — fresh data, no errors, decision internally consistent → just follow it.
  AMBER  ⚠️ — a soft issue worth a glance (e.g. Strava sync lag) → likely fine, named.
  RED    ⛔ — the decision can't be trusted (stale/blocked/missing model) → don't follow.

Prints a JSON badge to stdout: {status, emoji, summary, detail:[...]}.
Pure read-only (selector JSON + read-only DB). No side effects.
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "dynamic_session.json"
DB = Path(os.environ["HEALTH_DB_PATH"]) if os.environ.get("HEALTH_DB_PATH") else None

# Freshness tolerances (days). Gentle, to avoid false alarms on rest days / sync timing.
OURA_STALE_DAYS = 1     # Oura should sync every morning
STRAVA_STALE_DAYS = 3   # he may legitimately rest a couple days
RENPHO_STALE_DAYS = 4   # body-comp, not decision-critical
MIN_CONFIDENCE = 60


def _days_since(d, today):
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    elif isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    return (today - d).days


def _db_dates():
    """Latest data date per source, read-only. Returns {} if DB unavailable."""
    if DB is None or not DB.exists():
        return {}
    try:
        import duckdb
    except Exception:
        return {}
    try:
        c = duckdb.connect(str(DB), read_only=True)
    except Exception:
        return {}
    out = {}
    for key, sql in (
        ("strava", "SELECT max(start_date) FROM strava_activities"),
        ("oura", "SELECT max(day) FROM oura_sleep"),
        ("renpho", "SELECT max(measured_at) FROM renpho_measurements"),
    ):
        try:
            out[key] = c.execute(sql).fetchone()[0]
        except Exception:
            out[key] = None
    c.close()
    return out


def check_single_mode(session):
    """A running workout must be single-mode: every controllable section is either
    PACE-based (pace + duration) or HR-based (target-HR + duration), never mixed.
    Steps with mode None (rest) or "effort" (strides pickups) are exempt. Returns a
    list of RED issues (empty = clean). Pure — testable without a DB."""
    steps = (session or {}).get("steps") or []
    controllable = [s for s in steps if s.get("mode") in ("pace", "hr")]
    if not controllable:
        return []  # rest / gym / all-effort session — nothing to enforce
    modes = {s.get("mode") for s in controllable}
    issues = []
    if len(modes) > 1:
        issues.append(f"Mixed-mode workout — sections use {sorted(modes)}; must be all-pace OR all-hr.")
    for s in controllable:
        nm = s.get("name", "?")
        if not (s.get("duration") or "").strip():
            issues.append(f"Section '{nm}' has no duration.")
        field = "pace" if s.get("mode") == "pace" else "hr"
        val = (s.get(field) or "").strip().lower()
        if not val or val == "n/a":
            issues.append(f"{s['mode']}-mode section '{nm}' is missing its {field}.")
    return issues


def validate(decision=None, today=None):
    today = today or date.today()
    reds, ambers = [], []

    # ── Selector output present & parseable ───────────────────────────────
    if decision is not None:
        d = decision
    else:
        try:
            d = json.loads(DATA.read_text())
        except Exception as e:
            return {"status": "red", "emoji": "⛔",
                    "summary": "Workout could not be read — script likely failed.",
                    "detail": [f"dynamic_session.json unreadable: {e}"]}

    # ── Engine-reported data integrity (strongest signal) ─────────────────
    # The selector records any missing/stale CORE input and falls back to easy/rest.
    # Surface that as RED so a degraded prescription is never mistaken for a normal one.
    for issue in (d.get("data_issues") or []):
        reds.append(f"Core data issue: {issue} (engine fell back to easy/rest).")

    # ── Decision freshness ────────────────────────────────────────────────
    gen = d.get("generated")
    if gen != today.isoformat():
        reds.append(f"Decision is from {gen or '?'}, not today — selector did not run.")

    # ── Decision validity / internal consistency ──────────────────────────
    rec = d.get("recommendation", {}) or {}
    winner = rec.get("workout")
    scores = d.get("scores", {}) or {}
    blocked = d.get("blocked", {}) or {}
    if not winner:
        reds.append("No workout was selected.")
    else:
        if scores.get(winner, -1) < 0:
            reds.append(f"Selected '{winner}' but its score is blocked/negative — inconsistent.")
        if winner in blocked:
            reds.append(f"Selected '{winner}' yet it is also in the blocked list — contradiction.")
    conf = rec.get("confidence")
    if conf is None:
        ambers.append("No confidence score on the decision.")
    elif conf < MIN_CONFIDENCE:
        ambers.append(f"Low decision confidence ({conf}).")

    # ── Single-mode session structure ─────────────────────────────────────
    # A mixed pace/HR prescription is a build bug — surface as RED so it's never followed.
    for issue in check_single_mode(d.get("session")):
        reds.append(issue)

    # ── Required models present ───────────────────────────────────────────
    models = d.get("models", {}) or {}
    for m in ("readiness", "load"):
        if not models.get(m):
            reds.append(f"Missing {m} model — core input absent.")

    # ── Live data freshness ───────────────────────────────────────────────
    dates = _db_dates()
    if dates:
        for key, limit, label in (
            ("oura", OURA_STALE_DAYS, "Oura (readiness)"),
            ("strava", STRAVA_STALE_DAYS, "Strava (load)"),
            ("renpho", RENPHO_STALE_DAYS, "Renpho (body-comp)"),
        ):
            ds = _days_since(dates.get(key), today)
            # Oura is the core safety input — stale Oura is RED, not a soft amber.
            bucket = reds if key == "oura" else ambers
            if ds is None:
                bucket.append(f"{label} has no data.")
            elif ds > limit:
                bucket.append(f"{label} last synced {ds}d ago — {'core safety input stale, do not trust' if key == 'oura' else 'may be stale'}.")
    elif decision is None:
        ambers.append("Could not read data dates (DB busy) — freshness unverified.")

    # ── Verdict ───────────────────────────────────────────────────────────
    label = (rec.get("label") or winner or "session").strip()
    if reds:
        return {"status": "red", "emoji": "⛔",
                "summary": f"Don't trust today's {label} — {reds[0]}",
                "detail": reds + ambers}
    if ambers:
        return {"status": "amber", "emoji": "⚠️",
                "summary": f"{label} looks right, glance first: {ambers[0]}",
                "detail": ambers}
    return {"status": "green", "emoji": "✅",
            "summary": f"Verified — data current, no errors, decision consistent. Run the {label}.",
            "detail": []}


if __name__ == "__main__":
    badge = validate()
    print(json.dumps(badge, indent=2, sort_keys=True))
    # Exit 0 always — this is an advisory stamp, not a build gate.
    sys.exit(0)
