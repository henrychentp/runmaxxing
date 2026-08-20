#!/usr/bin/env python3
"""
Smart Session Selector — stimulus-based daily workout picker
Goal: grow fitness (CTL) while keeping form (TSB) in a productive zone.
No fixed rotation. Pure stimulus-based scoring.

Evidence base (Perplexity research 2026-04-07):
  - TSB < -30: high overreaching/injury risk (research consensus)
  - TSB -5 to -20: optimal beginner training zone
  - Intensity model: at <6h/week, POLARIZED outperforms pyramidal (2026 research)
    Target: 80% Z1, 5% Z2, 15% Z3 (80/5/15 split)
    Z2 threshold is least efficient zone for low-volume runners
    Prioritize Z1 volume + Z3 quality over Z2 threshold grinds
  - Intervals need 2+ speed-work runs (avg HR >153 OR max >=168 w/ avg >145, 15-75min) in past 21 days (neuromuscular prep proxy; Strava summary can't detect in-run strides pickups)
  - HRV red day (suppressed + falling trend) = no hard session
  - Gym: heavy (3-6 reps 80-90% 1RM) > plyometrics for running economy
  - Deload: every 3-5 weeks OR when TSB sustained < -30

Scoring logic:
  score = base_score * overdue_factor * tsb_modifier
  winner = highest valid score that clears all gates

Gates:
  - readiness < 55 → only rest
  - TSB below session minimum → blocked
  - back-to-back hard days → hard blocked
  - day after very_high load → all runs blocked
  - > 2 hard sessions this week → hard blocked
  - HRV falling trend + readiness < 70 → hard sessions blocked
  - deload mode (TSB < -30) → hard sessions blocked, easy/gym only
  - same-category gym < 3 days ago → blocked (connective tissue recovery)
  - weekly gym cap: lower 2x, upper 1x → blocked when hit
"""

import json
import os
import subprocess
import sys
import statistics
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rhr_baseline import rhr_baseline  # single source of truth, shared with analytics.sh
from hrmax_baseline import hrmax_baseline  # single source of truth for max HR (12mo rolling, guarded)


def dump_json(obj) -> str:
    """Stable public JSON: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    """Atomic write via tmp + rename so concurrent readers never see partial writes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

DATA_DIR = Path(__file__).resolve().parent / "data"


def apply_runtime_env():
    """Honor SESSION_SELECTOR_DATA_DIR for tests and replay. Call at start of main()."""
    global DATA_DIR
    override = os.environ.get("SESSION_SELECTOR_DATA_DIR")
    if override:
        DATA_DIR = Path(override)


def selector_today():
    """Honor SESSION_SELECTOR_TODAY=YYYY-MM-DD so a week can be replayed."""
    raw = os.environ.get("SESSION_SELECTOR_TODAY")
    if raw:
        return date.fromisoformat(raw)
    return date.today()


def load_recent_activities():
    """Load recent activities without calling a private Strava helper.

    Order:
      1. SESSION_SELECTOR_RECENT_CMD — shell command that prints a JSON array
      2. data/recent_activities.json
    """
    cmd = os.environ.get("SESSION_SELECTOR_RECENT_CMD")
    if cmd:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        try:
            return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
        except json.JSONDecodeError:
            return []
    path = DATA_DIR / "recent_activities.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []

VOLUME_FLOOR_MIN = 150  # aerobic minutes/week for VAT mobilization

# ── Goal priority: the ONE explicit place the concurrent-goal trade-off lives ──
# Athletes often juggle overlapping goals (fat loss, race speed, muscle retention). Previously the
# ACTIVE trade-off between them was implied only by which scattered magic constant
# happened to be larger (easy-aerobic +10 here, a 220 quality floor there). That made
# "what is the engine optimising for THIS block?" unanswerable without reading the
# whole file. This declares it once. The bonuses/floors below read GOAL_PROFILE
# instead of bare numbers, so flipping GOAL_PRIORITY re-weights the engine in one edit.
# Defaults reproduce prior behaviour (fat_loss): easy_aerobic_bonus 10 == the old +10.
GOAL_PRIORITY = "speed"   # "fat_loss" | "speed" | "balanced"
GOAL_PROFILES = {
    # easy_aerobic_bonus : pts added to easy_z2 when readiness/load allow (fat-ox volume)
    "fat_loss": {"easy_aerobic_bonus": 10},
    "speed":    {"easy_aerobic_bonus":  5},
    "balanced": {"easy_aerobic_bonus":  8},
}
GOAL_PROFILE = GOAL_PROFILES[GOAL_PRIORITY]

# ── Raw weekly-volume ramp caps (research lever #1, 2026-06-03) ──────────
# Distance jumps >30% over 2 weeks raised distance-related injury risk (HR≈1.6,
# PMID 25155475); BSI guidance endorses ≤10%/wk. Sit at the conservative end of the band when ground-reaction force per step is high.
VOL_RAMP_CAP     = 1.12   # ≤12% week-over-week vs trailing 4-week average
VOL_RAMP_2WK_CAP = 1.30   # ≤30% over any 2-week window
# paces.json is rewritten daily (daily-prefill 8am + pre-briefing) from duckdb
# VDOT + actual Z2. Tolerate a 0-1d lag (selector may run before the 8am refresh)
# and one missed day; flag as a data-integrity issue at 3+ days old, which means
# the refresh pipeline is down and pace-led targets are no longer tracking data.
PACES_STALE_DAYS = 2      # stale when (today - updated) > this

# Karvonen Z2 HR targeting (HRmax 190 = Strava-observed max; RHR = rolling 30d Oura average)
HRMAX = hrmax_baseline()  # 12-month rolling observed max HR (guarded); falls back to 190
RHR_DEFAULT = rhr_baseline()  # rolling 30d average of Oura nightly RHR; falls back to 57

def karvonen_z2_target(tsb, rhr=RHR_DEFAULT):
    """Return (target_bpm, ceiling_bpm, label) for Z2 sessions."""
    hrr = HRMAX - rhr
    ceil = min(round(rhr + 0.70 * hrr), 149)  # hard cap at 149
    if tsb < -30:
        target = round(rhr + 0.61 * hrr)  # deload: ~138
        return target, ceil, f"target HR {target} bpm (deload)"
    target = round(rhr + 0.64 * hrr)      # normal: ~142
    return target, ceil, f"target HR {target} bpm"

# ── TSB thresholds (research-calibrated 2026-04-07) ──────────
# Source: coaching literature + Perplexity research synthesis
# Beginner target zone: -5 to -20. Below -30 = high injury/overreach risk.
TSB_PEAK     =   5   # peak freshness — push hard
TSB_GOOD     = -10   # optimal training zone
TSB_MODERATE = -20   # getting tired — reduce intensity
TSB_FATIGUED = -30   # research danger threshold — easy/gym only
TSB_CRITICAL = -40   # overreaching territory — recovery mode

READY_GREEN  = 85
READY_YELLOW = 70
READY_RED    = 55

# ── HRV thresholds (RMSSD-based, research-calibrated 2026-04-12) ──
# Source: Perplexity research - HRV4Training methodology, Kubios validation
# Normal range = baseline mean +/- 0.5 SD over 14-30 day window
# Below normal range = downgrade hard to easy
# 24-48h dip after hard session is expected, don't react
HRV_BASELINE_DAYS = 30   # days for baseline calculation
HRV_ROLLING_DAYS  = 7    # rolling average window
HRV_SD_BAND       = 0.5  # +/- standard deviations for normal range

# ── Session catalogue ──────────────────────────────────────
# base_score    : urgency weight for 10K goal improvement
# type          : "hard" | "easy" | "gym" | "rest"
# min_tsb       : minimum TSB to unlock this session
# min_readiness : minimum readiness to unlock
# ideal_freq    : target days between sessions of this type
CATALOGUE = {
    # Phase 1 speed work: strides must come before intervals
    # 2-3 weeks of strides → then intervals unlock
    "strides": {
        "base_score": 72,  "type": "moderate",
        "min_tsb": -25,    "min_readiness": 60,  "ideal_freq": 4,
    },
    # VO2max session (unlocked after strides established). Protocol ALTERNATES in
    # build_session: 5×3min @ ~vVO2max is the backbone, 4×4min Helgerud rotates in
    # ~every 3rd week (research 2026-06-03: long 3-4min reps beat 400/800m repeats
    # and 30/30s for VO2max gain + time≥90% VO2max, at lower injury/mental cost).
    # One key (not 400/800 split) so the two protocols don't starve each other.
    "intervals_vo2": {
        "base_score": 90,  "type": "hard",
        "min_tsb": -15,    "min_readiness": 75,  "ideal_freq": 7,
    },
    # Threshold is the FALLBACK quality session, not a co-flagship (training-rules
    # L8 2026-05-26d: weekly hard = VO2 intervals; tempo minimized during ADS,
    # surfaced only when intervals are gated by readiness<75 / not strides_ready).
    # base_score sits below long_run(75) so it can't out-compete the flagship slot
    # on a normal day, but above easy(55) so it wins when intervals are blocked.
    "threshold": {
        "base_score": 62,  "type": "hard",
        "min_tsb": -25,    "min_readiness": 65,  "ideal_freq": 7,
    },
    "long_run": {
        "base_score": 75,  "type": "easy",
        "min_tsb": -35,    "min_readiness": 60,  "ideal_freq": 7,
    },
    "easy_z2": {
        "base_score": 55,  "type": "easy",
        "min_tsb": -999,   "min_readiness": 55,  "ideal_freq": 2,
    },
    "full_body_gym": {
        "base_score": 44,  "type": "gym",
        "min_tsb": -999,   "min_readiness": 0,   "ideal_freq": 4,
    },
    "rest": {
        "base_score":  0,  "type": "rest",
        "min_tsb": -999,   "min_readiness": 0,   "ideal_freq": 7,
    },
}


def _nested(data, path, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


_SLEEP_ORDER = {"green": 0, "yellow": 1, "red": 2}


def sleep_band(duration_h, score):
    """Worse-of sleep duration band vs Oura sleep-score band (doc §5).

    Too little sleep is a direct problem regardless of score, so short duration
    downgrades on its own; a poor score on adequate duration (fragmentation) also
    downgrades. Return whichever signal is worse so neither is masked by the other.
    """
    d = "green"
    if duration_h is not None:
        if duration_h < 6.0:
            d = "red"
        elif duration_h < 7.0:
            d = "yellow"
    s = "green"
    if score:
        if score < 65:
            s = "red"
        elif score < 80:
            s = "yellow"
    return d if _SLEEP_ORDER[d] >= _SLEEP_ORDER[s] else s


def update_vo2max_history(recovery_signals):
    """Upsert the latest Oura VO2max reading (keyed by its own measurement day)
    into a small rolling history. Oura computes VO2max infrequently, so a *trend*
    only becomes available once several readings accumulate — this banks them.
    Returns the sorted [(day, vo2max), ...] history."""
    path = DATA_DIR / "vo2max_history.json"
    try:
        hist = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        hist = {}
    day = _nested(recovery_signals, ["oura", "vo2_max", "day"])
    val = _num(_nested(recovery_signals, ["oura", "vo2_max", "vo2_max"]), 0)
    if day and val > 0:
        hist[str(day)] = round(val, 1)
    hist = dict(sorted(hist.items())[-365:])
    try:
        atomic_write_text(path, json.dumps(hist, indent=2))
    except OSError:
        pass
    return sorted(hist.items())


def vo2max_status(history):
    """Trend from VO2max history. Needs >=2 readings spanning >=21 days, else
    'insufficient' (mirrors the HRV cold-start fallback — no fake trend from a
    single point). Returns {status, latest, delta}."""
    if not history:
        return {"status": "insufficient", "latest": None, "delta": None}
    latest = history[-1][1]
    if len(history) < 2:
        return {"status": "insufficient", "latest": latest, "delta": None}
    try:
        first_day = datetime.strptime(history[0][0], "%Y-%m-%d").date()
        last_day = datetime.strptime(history[-1][0], "%Y-%m-%d").date()
    except (ValueError, TypeError, IndexError):
        return {"status": "insufficient", "latest": latest, "delta": None}
    if (last_day - first_day).days < 21:
        return {"status": "insufficient", "latest": latest, "delta": None}
    delta = round(latest - history[0][1], 1)
    # +/-2.0 ml/kg/min band: Oura's VO2max estimate has test-retest noise wider
    # than 1.0, so a 1-point move over 3 weeks is within error and would flip the
    # label on noise. Require a 2-point move to call a real trend.
    status = "improving" if delta >= 2.0 else ("declining" if delta <= -2.0 else "stable")
    return {"status": status, "latest": latest, "delta": delta}


# ── Eight Sleep history + RR-based illness early-warning ───────────────────
# WHY: sleeping respiratory rate is the single best wearable illness pre-symptom
# signal — it rises ~1-2 breaths/min 1-2 nights BEFORE symptoms (Oura/8sleep
# COVID-era validation). The Pod measures it independently of the wrist (Oura),
# so it's a true second sensor. But an absolute RR is meaningless without a
# PERSONAL baseline (resting sleeping RR varies 12-20 between people), so we bank
# a rolling history and threshold against the individual median — never a flat cut.
EIGHT_SLEEP_RR_HISTORY_DAYS = 30   # baseline window
EIGHT_SLEEP_RR_MIN_NIGHTS   = 7    # cold-start floor before any RR call fires
EIGHT_SLEEP_RR_ABS_DELTA    = 1.5  # breaths/min over baseline = elevated (lit: 1-2)
EIGHT_SLEEP_RR_PCT_DELTA    = 0.08 # AND >=8% over baseline (noise guard)


def update_eight_sleep_history(recovery_signals):
    """Bank last night's Eight Sleep RR + HRV (keyed by session date) into a
    rolling history so a personal baseline can form. Returns sorted history list
    [(day, {"rr":x,"hrv":y}), ...]."""
    path = DATA_DIR / "eight_sleep_history.json"
    try:
        hist = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        hist = {}
    es = _nested(recovery_signals, ["eight_sleep"], {}) or {}
    day = es.get("date")
    rr = _num(es.get("respiratory_rate"), 0)
    hrv = _num(es.get("hrv"), 0)
    if day and (rr > 0 or hrv > 0):
        hist[str(day)] = {"rr": round(rr, 1) if rr else None,
                          "hrv": round(hrv, 1) if hrv else None}
    hist = dict(sorted(hist.items())[-365:])
    try:
        atomic_write_text(path, json.dumps(hist, indent=2))
    except OSError:
        pass
    return sorted(hist.items())


def eight_sleep_rr_status(history):
    """Personal-baseline RR illness watch. Compares the latest night's sleeping
    respiratory rate to the trailing median (excluding the latest). Fires
    'elevated' only when BOTH an absolute (>=1.5 bpm) AND relative (>=8%) rise
    clear — conservative, to keep a zero-noise illness signal. Returns
    {status, latest, baseline, delta}."""
    rr_series = [(d, v.get("rr")) for d, v in history if v.get("rr")]
    if len(rr_series) < EIGHT_SLEEP_RR_MIN_NIGHTS:
        return {"status": "insufficient", "latest": None, "baseline": None, "delta": None}
    latest = rr_series[-1][1]
    baseline_pool = [v for _d, v in rr_series[:-1][-EIGHT_SLEEP_RR_HISTORY_DAYS:]]
    if not baseline_pool:
        return {"status": "insufficient", "latest": latest, "baseline": None, "delta": None}
    baseline = statistics.median(baseline_pool)
    delta = round(latest - baseline, 1)
    elevated = (delta >= EIGHT_SLEEP_RR_ABS_DELTA
                and baseline > 0 and (delta / baseline) >= EIGHT_SLEEP_RR_PCT_DELTA)
    return {"status": "elevated" if elevated else "normal",
            "latest": latest, "baseline": round(baseline, 1), "delta": delta}


def compute_sleep_debt(oura_data, today=None):
    """Acute multi-night sleep deprivation — a recovery HARD-STOP distinct from the
    chronic 14d-avg modifier (which is already in base readiness). Severe acute sleep
    loss blunts adaptation and raises injury risk, so it blocks hard work outright.
    Conservative thresholds so it won't nag on a single okay-ish night.

    Indexed by CALENDAR night (the `day` field), not by list position: a night Oura
    failed to sync is treated as UNKNOWN (a gap), never silently bridging two
    non-adjacent bad nights into a false "2 consecutive" pair, and never dropped so a
    bad night vanishes from the count. Stale data (newest night >1 day old) does not
    assert an acute debt. Returns (is_debt, reason)."""
    nightly = oura_data.get("nightly", []) or []
    by_date = {}
    for n in nightly:
        if not isinstance(n, dict):
            continue
        h = _num(n.get("sleep_h"))
        d = n.get("day") or n.get("date") or n.get("summary_date")
        if h and d:
            try:
                by_date[datetime.strptime(str(d)[:10], "%Y-%m-%d").date()] = h
            except ValueError:
                continue
    if not by_date:
        return False, ""

    last_date = max(by_date)
    # Don't fire on stale data — if the newest night is >1 day old, the acute signal
    # is unknowable (sync gap / device off), so stay silent rather than assert debt.
    if today is not None and (today - last_date).days > 1:
        return False, ""

    # Build the last 4 CALENDAR nights ending at the newest synced night; None = gap.
    nights = [(last_date - timedelta(days=i), by_date.get(last_date - timedelta(days=i)))
              for i in range(3, -1, -1)]
    last_h = by_date[last_date]
    if last_h < 5.0:
        return True, f"last night {last_h:.1f}h (<5h)"
    # 2 consecutive ACTUAL (present + calendar-adjacent) nights <6h
    for (da, ha), (db, hb) in zip(nights, nights[1:]):
        if ha is not None and hb is not None and (db - da).days == 1 and ha < 6.0 and hb < 6.0:
            return True, f"2 consecutive nights <6h ({ha:.1f}, {hb:.1f})"
    # 3+ of the last 4 calendar nights <6h (present nights only)
    if sum(1 for _d, h in nights if h is not None and h < 6.0) >= 3:
        return True, "3+ of last 4 nights <6h"
    return False, ""


def _category_of(key):
    """Map a session key to its weekly category (for adherence comparison)."""
    if key in ("intervals_vo2", "threshold"):
        return "quality"
    if key in ("easy_z2", "strides"):
        return "easy"
    if key == "long_run":
        return "long"
    if key == "full_body_gym":
        return "gym"
    if key == "rest":
        return "rest"
    return key


def log_decision(today, winner, label, sess_type, confidence, metrics):
    """Append/replace today's recommendation in a rolling decision log (keyed by
    date, ~180 days kept). This is the accumulating record that lets future
    personal calibration compare what the selector recommended vs what actually
    happened — without it, the system can never learn from its own track record."""
    path = DATA_DIR / "decision_log.json"
    try:
        log = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        log = {}
    log[today.isoformat()] = {
        "recommended": winner,
        "category": _category_of(winner),
        "label": label,
        "type": sess_type,
        "confidence": confidence,
        **metrics,
    }
    log = dict(sorted(log.items())[-180:])
    try:
        atomic_write_text(path, json.dumps(log, indent=2))
    except OSError:
        pass
    return log


def compute_adherence(log, recent_activities, today, last_analysis=None):
    """Compare past recommendations vs what actually happened (Strava). Returns
    adherence rate over 14/30d windows + recent mismatches, so persistent overrides
    surface as a signal that the rotation/gates may be mis-tuned."""
    rank = {"quality": 4, "long": 3, "gym": 2, "easy": 1}
    actual_by_date = {}
    for a in recent_activities:
        d = a.get("date")
        cat = _category_of(classify(a)) if (d and classify(a)) else None
        if cat is None:
            continue
        if d not in actual_by_date or rank.get(cat, 0) > rank.get(actual_by_date[d], 0):
            actual_by_date[d] = cat

    def window(days):
        hit = tot = 0
        misses = []
        for d, rec in log.items():
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if dd >= today or (today - dd).days > days:
                continue
            rec_cat = rec.get("category")
            act_cat = actual_by_date.get(d, "rest")   # no activity that day = rest taken
            tot += 1
            if rec_cat == act_cat:
                hit += 1
            else:
                misses.append({"date": d, "recommended": rec_cat, "actual": act_cat})
        return {"window_days": days, "n": tot, "adhered": hit,
                "rate": round(hit / tot, 2) if tot else None, "misses": misses[-5:]}

    out = {"last_14d": window(14), "last_30d": window(30)}
    if last_analysis and last_analysis.get("verdict"):
        out["last_run_verdict"] = {
            "date": last_analysis.get("date"),
            "verdict": last_analysis.get("verdict"),
            "classified_zone": last_analysis.get("classified_zone"),
            "pace_diff_sec": last_analysis.get("pace_diff_sec"),
        }
    return out


def compute_decision_models(ctx, readiness, tsb, ctl, atl, acwr, hrv_status,
                            avg_sleep, rhr_trend, injury, week_hard,
                            week_gym_count, days_since_gym, zones,
                            week_km, weekly_km_target, sleep_score=None,
                            vo2max=None, sleep_debt=(False, ""), hrv_trend=""):
    """Layered state model used by the workout scorer.

    This keeps the daily selector from overreacting to one noisy metric while
    still enforcing hard stops when multiple risk signals line up.
    """
    temp_dev = _num(_nested(ctx, ["readiness", "latest", "temperature_deviation"]), 0.0)
    resp_rate_high = bool(_nested(ctx, ["anomalies", "respiratory_rate_high"], False))
    recovery_signals = ctx.get("recovery_signals", {}) if isinstance(ctx, dict) else {}
    recovery_alerts = recovery_signals.get("alerts", []) if isinstance(recovery_signals, dict) else []
    spo2_avg = _num(_nested(recovery_signals, ["oura", "spo2", "average"]), 0)
    bdi = _num(_nested(recovery_signals, ["oura", "spo2", "breathing_disturbance_index"]), 0)
    stress_high = _num(_nested(recovery_signals, ["oura", "stress", "stress_high_min"]), 0)
    resilience_level = str(_nested(recovery_signals, ["oura", "resilience", "level"], "") or "").lower()
    # Eight Sleep sleeping-RR illness early-warning (personal baseline; see
    # eight_sleep_rr_status). Independent of the Oura wrist sensor → true second
    # opinion. Elevated alone = caution (-6); elevated + corroborating temp/SpO2 = illness.
    es_rr_status = str(_nested(recovery_signals, ["eight_sleep", "rr_status", "status"], "") or "")
    es_rr_elevated = es_rr_status == "elevated"
    es_rr_delta = _nested(recovery_signals, ["eight_sleep", "rr_status", "delta"])
    renpho_water = _num(_nested(recovery_signals, ["renpho", "water"]), 0)
    renpho_weight = _num(_nested(recovery_signals, ["renpho", "weight"]), 0)
    weight_delta_1d = _num(_nested(recovery_signals, ["renpho", "weight_delta_1d"]), 0)
    # Overnight weight drop matters as % of bodyweight, not an absolute kg figure
    # (doc §7). A ~1% / ~1kg drop is a caution; only a large drop (>=2% bodyweight)
    # WITH a corroborating dehydration sign — or an extreme >=2.5% drop — is a hard
    # stop. This stops a single rehydratable overnight drop from killing quality work.
    low_water = bool(renpho_water and renpho_water < 55)
    weight_drop_pct = (abs(weight_delta_1d) / renpho_weight) if (renpho_weight and weight_delta_1d < 0) else 0.0
    dehydration_signs = low_water or (rhr_trend == "RISING") or (avg_sleep is not None and avg_sleep < 6.5)
    hydration_block = (weight_drop_pct >= 0.02 and dehydration_signs) or weight_drop_pct >= 0.025
    hydration_caution = (not hydration_block) and (weight_drop_pct >= 0.012 or low_water)
    hydration_risk = hydration_block   # name kept for downstream hard-gate consumers
    # Temperature: tier it (doc §temp) instead of a flat 0.3 full-running block.
    # Small blips are noise; >=0.6 (or respiratory/SpO2 issues) is treated as illness.
    temp_illness = temp_dev >= 0.6
    temp_caution = 0.3 <= temp_dev < 0.6
    # Elevated 8sleep RR is a hard illness signal ONLY when corroborated by another
    # axis (temp blip or low SpO2); on its own it's a watch (handled below as caution).
    es_rr_corroborated = es_rr_elevated and (temp_caution or temp_illness or (spo2_avg and spo2_avg < 95))
    illness_flag = (temp_illness or resp_rate_high or (spo2_avg and spo2_avg < 95)
                    or bdi >= 15 or es_rr_corroborated)

    readiness_score = readiness
    readiness_reasons = [f"base readiness {readiness}"]
    if hrv_status.get("status") == "suppressed":
        readiness_score -= 10
        readiness_reasons.append("HRV suppressed")
    elif hrv_status.get("status") == "elevated":
        readiness_score += 3
        readiness_reasons.append("HRV elevated/robust")
    if rhr_trend == "RISING":
        readiness_score -= 7
        readiness_reasons.append("RHR rising")
    # Sleep is already a ~30% component of the base `readiness` input (and is baked
    # into Oura's own readiness contribution), so it is NOT re-deducted here — doing
    # so triple-counted sleep across the recovery axis (doc §4). Sleep still modulates
    # *intensity* (how hard today) via the worse-of band in score_session, which is
    # the doc-sanctioned use of a recovery metric. Surface the band as context only.
    s_band = sleep_band(avg_sleep, sleep_score)
    if s_band != "green":
        # avg_sleep can be None (Oura key present with null value) — guard the :.1f
        # format the same way the dehydration check above guards `avg_sleep is not None`.
        sleep_str = f"{avg_sleep:.1f}h" if avg_sleep is not None else "n/a"
        readiness_reasons.append(
            f"sleep {sleep_str} / score {sleep_score if sleep_score else 'n/a'} "
            f"({s_band}) — modulates intensity, not re-scored into readiness")
    sleep_debt_flag, sleep_debt_reason = sleep_debt
    if sleep_debt_flag:
        readiness_score -= 8
        readiness_reasons.append(f"acute sleep debt: {sleep_debt_reason}")
    if illness_flag:
        readiness_score = min(readiness_score, 55)
        msg = "possible illness signal"
        if es_rr_corroborated:
            msg += f" (8sleep RR +{es_rr_delta}bpm + temp/SpO2 corroboration)"
        readiness_reasons.append(msg)
    elif temp_caution:
        readiness_score -= 6
        readiness_reasons.append(f"temp deviation {temp_dev:.2f}°C (illness watch)")
    elif es_rr_elevated:
        # Elevated sleeping RR alone (uncorroborated) — soft watch, not a block.
        readiness_score -= 6
        readiness_reasons.append(
            f"8sleep sleeping RR +{es_rr_delta}bpm over baseline (illness watch)")
    if stress_high >= 120:
        readiness_score -= 6
        readiness_reasons.append(f"Oura high stress {stress_high:.0f}min")
    if resilience_level in ("limited", "low"):
        readiness_score -= 6
        readiness_reasons.append(f"Oura resilience {resilience_level}")
    if low_water:
        readiness_score -= 4
        readiness_reasons.append(f"Renpho water {renpho_water:.1f}%")
    if hydration_block:
        readiness_score -= 8
        readiness_reasons.append(
            f"overnight -{abs(weight_delta_1d):.1f}kg ({weight_drop_pct*100:.1f}%) + signs — dehydration risk")
    elif hydration_caution:
        readiness_score -= 3
        readiness_reasons.append(
            f"overnight -{abs(weight_delta_1d):.1f}kg ({weight_drop_pct*100:.1f}%) — hydrate, not a stop")
    readiness_score = max(0, min(100, round(readiness_score)))

    if readiness_score >= 75:
        readiness_color = "Green"
    elif readiness_score >= 60:
        readiness_color = "Yellow"
    elif readiness_score >= 45:
        readiness_color = "Orange"
    else:
        readiness_color = "Red"
    # NOTE: readiness color is intentionally NOT downgraded for TSB/ACWR here.
    # Load (TSB/ACWR/CTL-ramp) is owned by the load domain below; recovery owns
    # readiness/HRV/sleep/RHR. Folding load into readiness too double-counted the
    # same fatigue (doc §4). Acute-load safety is enforced by explicit load gates
    # (Overreached + ACWR>1.5 interval/long block) in apply_layered_decision.

    if tsb >= 0:
        load_status, load_score = "Fresh", 80
    elif tsb >= -10:
        load_status, load_score = "Productive", 70
    elif tsb >= -20:
        load_status, load_score = "Caution", 55
    else:
        load_status, load_score = "Overreached", 35
    load_reasons = [f"TSB {tsb:.1f}", f"CTL {ctl:.1f}", f"ATL {atl:.1f}"]
    if acwr:
        load_reasons.append(f"ACWR {acwr:.2f}")
        if acwr > 1.8:
            load_status, load_score = "Overreached", min(load_score, 25)
            load_reasons.append("ACWR very high")
        elif acwr > 1.5:
            load_status, load_score = ("Caution" if load_status != "Overreached" else load_status), min(load_score, 45)
            load_reasons.append("ACWR high")
        elif acwr > 1.3:
            load_score -= 5
            if load_status == "Fresh":
                load_status = "Productive"
            load_reasons.append("ACWR elevated")
        elif acwr < 0.8:
            # Undertraining: ACWR <0.8 carries elevated risk specifically WHEN
            # returning to higher load (research [2][6]). Not a block — flags headroom
            # to build; the ramp back stays gradual via the volume-ramp guard (≤12%/wk).
            load_reasons.append("ACWR <0.8 — undertrained; headroom to build, ramp gradually")
    if week_hard >= 3:
        load_status, load_score = "Caution", min(load_score, 45)
        load_reasons.append(f"{week_hard} hard runs this week")
    # Oura stress/resilience and hydration are recovery signals — they adjust the
    # readiness (recovery) domain only, not load. Keeping them out of load avoids
    # double-counting the same fatigue across two gates (doc §4).

    cutting = _nested(ctx, ["body_comp_goal", "cutting_safety"], {})
    cutting_status = (cutting or {}).get("status") or "OK"
    bf = _num(_nested(ctx, ["body_comp", "latest", "body_fat"]), 0)
    muscle_alert = None
    body_status = "OnTarget"
    body_score = 70
    body_reasons = [f"cutting safety {cutting_status}"]
    if cutting_status not in ("OK", "ok", "OnTarget", None):
        body_status, body_score = "LEA_Risk", 45
        body_reasons.append("cutting safety not OK")
    # LEA / under-fuelling cluster. A triple-AND (low readiness AND HRV suppressed
    # AND RHR rising) has low recall in the one domain training-rules L183 calls
    # non-negotiable ("in a deficit + HRV amber/red → ALWAYS downgrade"). Fire on
    # 2-of-3 stress signals; during an active cut, ANY single HRV-stress signal is
    # enough. HRV-stress carries a cold-start fallback so the gate isn't silently
    # absent when <14 days of banked readings make status "insufficient_data".
    hrv_stress = (hrv_status.get("status") == "suppressed") or (
        hrv_status.get("status") == "insufficient_data" and hrv_trend == "FALLING")
    stress_signals = sum((
        readiness_color in ("Orange", "Red"),
        hrv_stress,
        rhr_trend == "RISING",
    ))
    cut_active = bool(cutting)   # cutting_safety is tracked → in a cut/deficit phase
    if stress_signals >= 2 or (cut_active and hrv_stress):
        body_status, body_score = "LEA_Risk", min(body_score, 45)
        tag = " (cut-sensitive)" if (cut_active and hrv_stress and stress_signals < 2) else ""
        body_reasons.append(f"LEA cluster: {stress_signals}/3 stress signals{tag}")
    if bf and bf > 17 and body_status != "LEA_Risk":
        body_reasons.append(f"BF {bf:.1f}%: favor easy aerobic volume")
    if week_gym_count < 2:
        body_reasons.append(f"gym {week_gym_count}/2 this week")
    if days_since_gym >= 7:
        muscle_alert = f"gym overdue {days_since_gym}d"

    z2_progress = _nested(ctx, ["z2_pace_progress", "monthly_improvement_sec"])
    decoupling = _num(_nested(ctx, ["aerobic_efficiency", "latest", "aerobic_decoupling"]), 0)
    perf_status = "Stable"
    perf_score = 65
    perf_reasons = []
    if z2_progress is not None:
        z2_delta = _num(z2_progress)
        if z2_delta >= 5:
            perf_status, perf_score = "Improving", 75
            perf_reasons.append(f"Z2 pace improving {z2_delta:.0f}s/month")
        elif z2_delta <= -10:
            perf_status, perf_score = "Regressing", 50
            perf_reasons.append(f"Z2 pace regressed {abs(z2_delta):.0f}s/month")
        else:
            perf_reasons.append("Z2 pace stable")
    if decoupling:
        perf_reasons.append(f"latest decoupling {decoupling:.1f}%")
        if decoupling > 8:
            perf_status, perf_score = "Fatigued", min(perf_score, 45)
        elif decoupling > 5:
            perf_score -= 5
    if vo2max and vo2max.get("latest"):
        if vo2max["status"] == "improving":
            if perf_status != "Fatigued":
                perf_status = "Improving"
            perf_score = max(perf_score, 75)
            perf_reasons.append(f"VO2max {vo2max['latest']} ↑{vo2max['delta']}")
        elif vo2max["status"] == "declining":
            perf_score -= 5
            perf_reasons.append(f"VO2max {vo2max['latest']} ↓{abs(vo2max['delta'])}")
        elif vo2max["status"] == "stable":
            perf_reasons.append(f"VO2max {vo2max['latest']} (stable)")
        else:
            perf_reasons.append(f"VO2max {vo2max['latest']} (baseline; trend pending)")
    if not perf_reasons:
        perf_reasons.append("not enough performance trend data")

    weekly_progress = week_km / weekly_km_target if weekly_km_target else 0
    return {
        "readiness": {
            "score": readiness_score,
            "color": readiness_color,
            "illness_flag": illness_flag,
            "reasons": readiness_reasons,
            "temperature_deviation": temp_dev,
            "temp_caution": temp_caution,
            "sleep_debt": sleep_debt_flag,
            "sleep_debt_reason": sleep_debt_reason,
            "recovery_alerts": recovery_alerts,
            "hydration_risk": hydration_risk,
            "hydration_caution": hydration_caution,
            "weight_drop_pct": round(weight_drop_pct, 4),
        },
        "load": {
            "status": load_status,
            "score": max(0, min(100, round(load_score))),
            "acwr": round(acwr, 2) if acwr else None,
            "reasons": load_reasons,
        },
        "body_comp": {
            "status": body_status,
            "score": body_score,
            "muscle_alert": muscle_alert,
            "reasons": body_reasons,
        },
        "performance": {
            "status": perf_status,
            "score": max(0, min(100, round(perf_score))),
            "reasons": perf_reasons,
            "latest_decoupling": round(decoupling, 2) if decoupling else None,
            "vo2max": vo2max or {"status": "insufficient", "latest": None, "delta": None},
        },
        "weekly_structure": {
            "week_km": round(week_km, 1),
            "week_target": weekly_km_target,
            "progress": round(weekly_progress, 2),
            "week_gym_count": week_gym_count,
            "mid_pct": zones.get("mid_pct", 0),
            "high_pct": zones.get("high_pct", 0),
        },
    }


def apply_layered_decision(scores, blocked, models, catalogue, ds, yest_type,
                           week_hard, week_gym_count, today_dow, injury=0):
    """Apply cross-domain hard blocks and additive score adjustments.

    The older selector used mostly multiplicative scoring. This pass is more
    explicit: safety blocks first, then goal/structure boosts.
    """
    adjustments = {k: [] for k in scores}
    readiness_color = models["readiness"]["color"]
    illness = models["readiness"]["illness_flag"]
    temp_caution = models["readiness"].get("temp_caution", False)
    sleep_debt = models["readiness"].get("sleep_debt", False)
    hydration_risk = models["readiness"].get("hydration_risk", False)
    load_status = models["load"]["status"]
    acwr = models["load"]["acwr"] or 0
    body_status = models["body_comp"]["status"]
    perf_status = models["performance"]["status"]
    decoupling = models["performance"].get("latest_decoupling") or 0

    for key, info in catalogue.items():
        if scores.get(key, -1) < 0:
            continue

        is_run = info["type"] in ("hard", "easy", "moderate")
        is_hard_run = info["type"] == "hard"
        is_long = key == "long_run"
        is_gym = key == "full_body_gym"

        # Niggle traffic-light (2026-06-03). injury: 0=green, 1=amber, 2=red.
        # RED = stop running (gym/cross-train/rest only). AMBER = "treat it like
        # red for that structure" (research §6) — drop quality + long, keep easy
        # Z2 + pain-free strides; volume is halved upstream.
        if injury >= 2 and is_run:
            scores[key] = -1
            blocked[key] = "niggle RED — stop running; gym/cross-train/rest only"
            continue
        if injury == 1 and (is_hard_run or is_long or key == "strides"):
            scores[key] = -1
            blocked[key] = "niggle AMBER — quality/long/strides blocked; easy Z2 only (no speed work on a niggle)"
            continue
        if illness and key not in ("rest", "full_body_gym"):
            scores[key] = -1
            blocked[key] = "illness signal — running intensity/volume blocked"
            continue
        if hydration_risk and (is_hard_run or is_long):
            scores[key] = -1
            blocked[key] = "hydration/weight-drop risk — hard/long run blocked"
            continue
        if sleep_debt and is_hard_run:
            scores[key] = -1
            blocked[key] = "acute sleep debt — hard run blocked (adaptation/injury risk)"
            continue
        if readiness_color == "Red" and key != "rest":
            scores[key] = -1
            blocked[key] = "red readiness — rest only"
            continue
        if readiness_color == "Orange" and is_hard_run:
            scores[key] = -1
            blocked[key] = "orange readiness — hard running blocked"
            continue
        if yest_type in ("hard", "long_run") and is_hard_run:
            scores[key] = -1
            blocked[key] = "no hard run after hard/long run"
            continue
        if load_status == "Overreached" and (is_hard_run or is_long):
            scores[key] = -1
            blocked[key] = "overreached load — hard/long run blocked"
            continue
        if acwr > 1.8 and (is_hard_run or is_long):
            scores[key] = -1
            blocked[key] = "ACWR > 1.8 — hard/long run blocked"
            continue
        if acwr > 1.5 and (key == "intervals_vo2" or is_long):
            scores[key] = -1
            blocked[key] = "ACWR > 1.5 — intervals/long run blocked (acute load spike)"
            continue
        if body_status == "LEA_Risk" and is_hard_run:
            scores[key] = -1
            blocked[key] = "LEA risk — hard running blocked"
            continue

        if readiness_color in ("Green", "Yellow") and load_status in ("Fresh", "Productive") and key == "easy_z2":
            _eab = GOAL_PROFILE["easy_aerobic_bonus"]
            scores[key] += _eab
            adjustments[key].append(f"+{_eab} easy aerobic work fits readiness/load ({GOAL_PRIORITY})")
        if key == "long_run" and ds.get("long_run", 999) >= 7 and decoupling < 5 and load_status != "Caution":
            scores[key] += 20
            adjustments[key].append("+20 long run due and decoupling good")
        elif key == "long_run" and decoupling > 7:
            scores[key] -= 15
            adjustments[key].append("-15 recent decoupling high; cap long run")
        if is_hard_run and perf_status == "Improving" and week_hard <= 1:
            scores[key] += 10
            adjustments[key].append("+10 performance improving and hard-run budget available")
        if is_hard_run and perf_status in ("Fatigued", "Regressing"):
            scores[key] -= 15
            adjustments[key].append("-15 performance fatigue/regression")
        if is_hard_run and temp_caution:
            scores[key] -= 15
            adjustments[key].append("-15 mild temp elevation (illness watch)")
        if is_gym and week_gym_count < 2:
            urgency = 20 if today_dow in ("Friday", "Saturday", "Sunday") else 12
            scores[key] += urgency
            adjustments[key].append(f"+{urgency} strength minimum not met")
        if is_gym and body_status == "LEA_Risk":
            scores[key] -= 10
            adjustments[key].append("-10 LEA risk: keep strength lighter")
        if key == "rest" and (readiness_color in ("Orange", "Red") or load_status == "Overreached" or body_status == "LEA_Risk" or sleep_debt):
            scores[key] += 40
            adjustments[key].append("+40 recovery risk cluster")
        if sleep_debt and key in ("easy_z2", "rest"):
            scores[key] += 15
            adjustments[key].append("+15 acute sleep debt — favor recovery")

    return scores, blocked, {k: v for k, v in adjustments.items() if v}


def recent_from_briefing(ctx):
    """Fallback recent activity list from briefing_context.json.

    The primary Strava shell command can produce empty stdout during transient
    auth/network failures. The briefing context is refreshed earlier in the
    morning and has enough fields for recency, weekly volume, and hard-day gates.
    """
    runs = _nested(ctx, ["recent_runs", "runs"], []) or []
    out = []
    for r in runs:
        _dur = _num(r.get("duration_min"), 0)
        out.append({
            "name": r.get("name") or "Run",
            "type": r.get("type") or "Run",
            "date": r.get("day") or r.get("date"),
            "start": r.get("start") or r.get("start_date_local"),
            "distance_km": _num(r.get("distance_km"), 0),
            "duration_min": _dur,
            # Pass through big-event fields if the briefing carries them; else fall
            # back to moving time for elapsed so the duration path still fires.
            "elapsed_min": _num(r.get("elapsed_min"), _dur),
            "suffer_score": r.get("suffer_score"),
            "avg_hr": r.get("average_heartrate") or r.get("avg_hr"),
            "max_hr": r.get("max_heartrate") or r.get("max_hr"),
        })
    return [a for a in out if a.get("date")]


def build_final_decision(winner, scores, blocked, models, score_adjustments,
                         readiness_raw, tsb, ctl, week_km, runs_done,
                         week_gym_count, muscle_alert, days_since_gym):
    valid = sorted([(k, v) for k, v in scores.items() if v >= 0], key=lambda x: -x[1])
    top3 = valid[:3]
    top3_str = ", ".join(f"{k}({v})" for k, v in top3)
    runner_up = valid[1] if len(valid) > 1 else None
    gap = round(scores[winner] - runner_up[1], 1) if runner_up else 999
    confidence = 95 if not runner_up else max(55, min(95, round(60 + gap / max(scores[winner], 1) * 80)))

    hard_blocked = [
        f"{k}: {v}" for k, v in blocked.items()
        if k in CATALOGUE and CATALOGUE[k]["type"] == "hard"
    ]

    why = [
        f"{models['readiness']['color']} readiness {models['readiness']['score']} ({readiness_raw} raw)",
        f"{models['load']['status']} load: {', '.join(models['load']['reasons'])}",
        f"{models['body_comp']['status']} body-comp: {', '.join(models['body_comp']['reasons'])}",
        f"{models['performance']['status']} performance: {', '.join(models['performance']['reasons'])}",
    ]
    why.extend(score_adjustments.get(winner, []))
    if runner_up:
        why.append(f"beat next option {runner_up[0]} by {gap} points")

    # Fueling targets: protein 1.6-1.8 g/kg; carbs periodized to session load;
    # maintenance on quality/long/lift days; modest deficit only on true easy days;
    # energy availability about 40 kcal/kg FFM.
    nutrition_flags = ["protein 1.6-1.8 g/kg across 4-5 meals (muscle retention)"]
    if winner in ("threshold", "intervals_vo2", "long_run"):
        nutrition_flags.append("QUALITY/LONG day: eat at maintenance — carbs 5-7g/kg, "
                               "1-4g/kg pre, 30-60g/h if >75min, 20-40g protein + carbs after")
    elif winner == "full_body_gym":
        nutrition_flags.append("LIFT day: protein priority, carbs to fuel the lift; do not under-eat")
    elif winner == "easy_z2":
        nutrition_flags.append("EASY day: modest deficit OK (300-500kcal), carbs ~3-5g/kg; don't stack deficit days")
    if models["body_comp"]["status"] == "LEA_Risk":
        nutrition_flags.append("LEA risk: calories to maintenance for several days (EA <30 erodes muscle + adaptation)")

    reason = (
        f"{models['readiness']['color']} readiness {models['readiness']['score']} "
        f"({readiness_raw} raw), {models['load']['status']} load, "
        f"{models['body_comp']['status']} body-comp, {models['performance']['status']} performance. "
        f"TSB {round(tsb,1)}, CTL {round(ctl,1)}. "
        f"Week: {round(week_km,1)}km, {runs_done} runs + {week_gym_count} gym. "
        f"Top scores: {top3_str}."
        + (f" Blocked hard: {'; '.join(hard_blocked)}." if hard_blocked else "")
        + (f" {muscle_alert}" if muscle_alert else "")
        + (f" Gym overdue: {days_since_gym}d." if days_since_gym >= 7 else "")
    )

    return {
        "winner": winner,
        "confidence": confidence,
        "score_gap_to_next": None if not runner_up else gap,
        "runner_up": None if not runner_up else {"key": runner_up[0], "score": runner_up[1]},
        "acceptable_alternatives": [
            {"key": k, "score": v}
            for k, v in valid[1:4]
            if runner_up and scores[winner] > 0 and (scores[winner] - v) / scores[winner] <= 0.25
        ],
        "top3": [{"key": k, "score": v} for k, v in top3],
        "why_won": why,
        "blocked_hard": hard_blocked,
        "nutrition_flags": nutrition_flags,
        "reason": reason,
    }

# ── Weekly rotation (v2.1 recalibration, Apr 2026) ────────
# Default shape for the week. Body data (Oura/Strava/Renpho) gates
# still override — if readiness is low, hard sessions get blocked
# regardless of rotation. Rotation prevents the scorer from picking
# strides on a rest day just because they're overdue.
# Weekly template: heavy lifts (Mon/Thu)
# are kept OFF the quality run days (Wed VO2, Fri threshold/10K-pace) to minimise
# concurrent-training interference. 5 runs (2 quality + 1 long + 2 easy) + 2 strength.
ROTATION = {
    "Monday":    ["full_body_gym", "easy_z2"],   # lower-body lift + easy run
    "Tuesday":   ["easy_z2", "strides"],
    "Wednesday": ["intervals_vo2"],               # VO2max (flagship quality)
    "Thursday":  ["full_body_gym", "easy_z2"],    # upper-body lift (+ optional easy)
    "Friday":    ["easy_z2", "strides"],          # easy + pickups (tempo is fallback-only, not a standing slot)
    "Saturday":  ["long_run"],
    "Sunday":    ["rest", "easy_z2"],             # rest or shake-out
}
ROTATION_ON_BOOST  = 1.8   # sessions matching today's rotation
ROTATION_OFF_HARD  = 0.25  # hard/moderate sessions NOT on rotation day
ROTATION_OFF_EASY  = 0.7   # easy sessions NOT on rotation day

# ── Heat correction ────────────────────────────────────────
# In the heat, HR drifts up at a fixed pace (~0.5-1.5 bpm/°C steady-state + larger
# cardiovascular drift over a sustained run) — a hot easy run can read 15-25 bpm above
# its temperate-equivalent average and falsely cross the 158 "hard" line, polluting
# week_hard / strides_ready / rolling-density with a session that was aerobically easy
# (research 2026-06-14: don't classify by raw HR in heat). We de-rate avg HR BEFORE
# any HR-based classification so the correction flows through every gate consistently.
# CONSERVATIVE + FAIL-SAFE: only outdoor runs (trainer != true) with a real device temp
# >18°C are corrected; coefficient 0.7 (low end) and a 15 bpm cap avoid demoting
# genuinely hard runs; treadmill / missing-temp runs are untouched. Source temp is
# Strava's device `average_temp` (often null; we simply don't correct when absent).
HEAT_CORR_BASE_C = 18.0   # below this, no correction
HEAT_CORR_COEF   = 0.7    # bpm subtracted per °C above base (low end of 0.5-1.5)
HEAT_CORR_CAP    = 15.0   # max bpm correction (slope is non-linear; don't over-correct)
HEAT_FLAG_C      = 28.0   # device temp at/above this = notable heat load (hydration nudge)


def heat_hr_correction(avg_hr, temp_c, is_trainer):
    """Return (corrected_hr, bpm_subtracted). No-op for indoor / missing-temp runs."""
    if not avg_hr or is_trainer or temp_c is None:
        return avg_hr, 0.0
    try:
        t = float(temp_c)
    except (TypeError, ValueError):
        return avg_hr, 0.0
    if t <= HEAT_CORR_BASE_C:
        return avg_hr, 0.0
    sub = min(HEAT_CORR_COEF * (t - HEAT_CORR_BASE_C), HEAT_CORR_CAP)
    return round(avg_hr - sub, 1), round(sub, 1)


# ── Activity classification ────────────────────────────────
def classify(act):
    """Classify a Strava activity dict into a CATALOGUE key."""
    atype = act.get("type", "")
    hr    = act.get("avg_hr") or 0
    mx    = act.get("max_hr") or 0
    dur   = act.get("duration_min") or 0

    if atype == "WeightTraining":
        return "full_body_gym"

    if atype == "Run":
        if hr > 165 and dur < 60:
            return "intervals_vo2"
        # Threshold by sustained avg (>158) OR a clear hard PEAK (max >= 168 = rep-band
        # floor, training-rules 2026-06-20b) on a non-easy average (>145). Whole-session
        # avg alone misses real threshold/interval work whose warm-up + recoveries drag
        # it below 158, e.g. Jun-19 (avg 157.1/max 174) and the Jul-08 5x3 VO2 session
        # (avg 146.7/max 171): at the old 173/149 floors it was counted as easy_z2 AND
        # a recovery day, so on Jul-11 the selector re-prescribed quality with the weekly
        # quota already met. Calibrated on 90d of data (2026-07-11): interval-day avgs
        # run 146.7-149.2; Z2/strides days never max above 163, so 168 keeps a 5-bpm
        # margin. Mirrors the sync_session_data.py HARD_PEAK_FLOOR=168 quality rule.
        if (hr > 158 or (mx >= 168 and hr > 145)) and dur <= 75:
            return "threshold"
        if dur >= 75:   # 75 not 70 — the prescribed strides session is ~70min (60 Z2 +
            return "long_run"   # ~10 strides); at 70 it was mis-tagged long_run, inflating
        return "easy_z2"        # week_long_done and blocking intervals via 3-day spacing.

    return None


def _scoreboard_summary(items, rec_done, rec_target):
    """One actionable line for the Today page / Friday nudge.

    Old behaviour dumped all four quota counters with a ⚠ on every behind/at_risk
    category — including ones the remaining days physically can't fit (status
    'at_risk' = remaining > days_left). That reads as guilt with no path to fix it,
    which trains the reader to ignore the line. New behaviour:
      • surface ONLY the single highest-leverage gap that is still ACHIEVABLE this
        week (status behind/on_track, unmet) — urgency-first, leverage as tiebreak;
      • demote truly out-of-reach (at_risk) categories to a muted note, no alarm —
        there is no action that closes them, so don't nag;
      • reframe recovery as a weekly owe, not a same-day 'need rest' command.
    """
    names = {"quality": "Quality", "long_run": "Long run", "full_body_gym": "Lifts"}
    # Leverage order for an aerobic + recomp goal: long run (aerobic keystone)
    # > quality (the week's single hard stimulus) > lifts (supportive for recomp).
    priority = ["long_run", "quality", "full_body_gym"]
    by_cat = {it["category"]: it for it in items}

    # Unmet + still safely completable. Built in leverage order, then sorted so the
    # time-sensitive 'behind' items lead 'on_track' ones (stable sort keeps leverage
    # order within each group). actionable = the one thing worth doing next.
    achievable = [by_cat[c] for c in priority
                  if by_cat.get(c)
                  and by_cat[c]["status"] in ("behind", "on_track")
                  and by_cat[c]["remaining"] > 0]
    achievable.sort(key=lambda it: 0 if it["status"] == "behind" else 1)
    actionable = achievable[0] if achievable else None

    # Out of reach this week — can't fit remaining in days_left even one/day.
    out_of_reach = [by_cat[c] for c in priority
                    if by_cat.get(c) and by_cat[c]["status"] == "at_risk"]

    parts = []
    if actionable:
        urgency = "do soon" if actionable["status"] == "behind" else "on track"
        parts.append(f"Next: {names[actionable['category']]} "
                     f"{actionable['done']}/{actionable['target']} ({urgency})")
    elif not out_of_reach:
        parts.append("Week's key work on track ✓")
    if out_of_reach:
        labels = ", ".join(names[it["category"]] for it in out_of_reach)
        parts.append(f"{labels} out of reach this wk")

    if rec_done >= rec_target:
        parts.append(f"Recovery {rec_done}/{rec_target} ✓")
    else:
        owe = rec_target - rec_done
        parts.append(f"owe {owe} rest day{'s' if owe > 1 else ''} this wk")
    return " · ".join(parts)


def days_since_each(activities, today):
    """Return {session_key: days_since_last_occurrence}."""
    last = {}
    for act in activities:
        key = classify(act)
        if key is None:
            continue
        keys = ("intervals_vo2",) if "interval" in key else (key,)
        day = act.get("date")
        if not day:
            continue
        for k in keys:
            # Keep the LATEST occurrence per key. Dates are "%Y-%m-%d" so
            # lexical comparison == chronological — robust to unsorted input.
            if k not in last or day > last[k]:
                last[k] = day

    out = {}
    for key, info in CATALOGUE.items():
        if key in last:
            d = (today - datetime.strptime(last[key], "%Y-%m-%d").date()).days
            out[key] = d
        else:
            out[key] = info["ideal_freq"] * 3   # never done → very overdue
    return out


# ── Big-event recovery window (post-ultra recovery guard, 2026-06-14) ──
# WHY THIS EXISTS: a single monster effort (ultra / 24h / backyard / very long
# race) leaves a multi-day-to-2-week PHYSIOLOGICAL RECOVERY TAIL during which hard
# training raises illness, injury, and overreaching risk. The neuromuscular hit
# alone (force/power still −12% at 5 days; running economy degraded ~30 days after
# a long ultra) plus the immune/inflammatory perturbation (largest + longest after
# ultra-endurance) make hard work ill-advised for 1-2 weeks. TSB/ATL/ACWR miss this
# entirely: they are EWMAs that decay back to neutral within ~a week, so every load
# gate reads green while the athlete is still deep in the recovery tail.
#   Real failure this fixes: after a long ultra, TSB can look "Productive" within a
#   week while physiology is still in a recovery tail. Hard work too soon raises
#   illness risk. This guard anchors the window to the EVENT, independent of TSB.
# CALIBRATION (research 2026-06-14, exercise-immunology + ultra-recovery lit):
#   - DURATION drives the perturbation, NOT HR-intensity (Diment 2015: only the
#     120-min bout suppressed in-vivo immunity, despite the high-intensity bout
#     producing MORE cortisol; Pedersen: duration is the prime driver of the IL-6
#     response, amplified by glycogen depletion). => detection is duration/distance-
#     led; Strava suffer (HR-based) is a SECONDARY trigger only — it systematically
#     under-counts a long low-HR ultra, the exact event we must catch.
#   - Onset ~90 min, robust by ~2 h (Nieman; Walsh 2011) => big-event bout ≥120 min.
#   - Time-on-feet override catches backyard/24h formats whose individual loops are
#     short but whose cumulative elapsed time is enormous.
#   - Sleep loss is the cleanest illness lever (Prather 2015: <5h sleep → OR 4.5 for
#     cold) => an overnight/24h event (≥10h on feet) extends the window.
#   NOTE: the "open-window immunosuppression → infection" causal story is contested
#   (Campbell & Turner 2018); we frame this as recovery/load management, not
#   "your immune system is suppressed." Same protective behavior, defensible science.
BIG_EVENT_MIN      = 120   # single continuous bout (min) — perturbation robust by 2h
BIG_EVENT_KM       = 25    # cumulative event distance (km) — mechanical load proxy
BIG_EVENT_SUFFER   = 200   # SECONDARY: summed run relative-effort (HR-based, under-counts)
BIG_EVENT_TOF      = 360   # cumulative time-on-feet (min) = 6h → big event regardless of HR
ULTRA_MIN          = 240   # single bout ≥4h → ultra tier (function recovery diverges)
ULTRA_KM           = 42    # marathon+
ULTRA_TOF          = 600   # cumulative time-on-feet ≥10h → ultra tier (overnight/24h)
STITCH_GAP_H       = 6     # activities starting <6h apart = ONE event (loops, midnight split)
RECOVERY_WINDOW_DAYS       = 7    # standard big-event window (markers 1-3d, function 1-2wk)
RECOVERY_WINDOW_DAYS_ULTRA = 14   # ultra tier — functional recovery runs 3-4+ weeks
RECOVERY_FULL_BLOCK_FRAC   = 0.5  # first 50%: hard+long+strides blocked (easy aerobic OK early)
ILLNESS_RAMP_DAYS          = 3    # days after the sick flag clears to block hard/long/strides


def _stitch_events(recent):
    """Cluster activities into discrete EVENTS by start-time gap.

    A backyard / 24h / last-person-standing ultra is logged by Strava as many
    separate loop activities (and can straddle midnight). Bucketing by calendar
    date alone splits one event into several sub-threshold days; clustering by a
    <STITCH_GAP_H start gap stitches them back into one. Activities lacking a
    parseable start time fall back to their date at midnight (still clusters within
    a day). Returns a list of event dicts with summed/maxed load fields.
    """
    items = []
    for a in recent:
        d = a.get("date")
        if not d:
            continue
        start = a.get("start")
        dt = None
        if start:
            try:
                dt = datetime.fromisoformat(str(start).replace("Z", "")[:19])
            except ValueError:
                dt = None
        if dt is None:
            try:
                dt = datetime.strptime(str(d)[:10], "%Y-%m-%d")
            except ValueError:
                continue
        items.append((dt, a))
    if not items:
        return []
    items.sort(key=lambda x: x[0])

    clusters, cur = [], []
    for dt, a in items:
        if cur and (dt - cur[-1][0]).total_seconds() > STITCH_GAP_H * 3600:
            clusters.append(cur)
            cur = []
        cur.append((dt, a))
    if cur:
        clusters.append(cur)

    events = []
    for cl in clusters:
        run_suffer = moving_sum = elapsed_sum = dist_sum = 0.0
        single_max = 0.0   # longest single continuous bout in the cluster
        is_run = False
        name = ""
        for _dt, a in cl:
            if a.get("type") != "Run":
                continue
            is_run = True
            mv = _num(a.get("duration_min"), 0)
            el = _num(a.get("elapsed_min"), mv)   # fall back to moving if no elapsed
            moving_sum += mv
            elapsed_sum += el
            dist_sum += _num(a.get("distance_km"), 0)
            run_suffer += _num(a.get("suffer_score"), 0)   # RUNS ONLY (no cross-modal sum)
            single_max = max(single_max, el, mv)
            if not name and a.get("name"):
                name = a.get("name")
        events.append({
            "start_date": cl[0][0].date(),
            "is_run": is_run, "name": name or "event",
            "run_suffer": run_suffer, "single_max_min": single_max,
            "time_on_feet_min": max(moving_sum, elapsed_sum),
            "distance_km": dist_sum,
        })
    return events


def detect_big_event(recent, today):
    """Return the recovery-window state for the most significant recent big event.

    Window is anchored to days-since-EVENT, not TSB. Detection is duration/distance-
    led (see calibration notes above); suffer is a secondary trigger only.

    Returns {"active": bool, "phase": "full_block"|"reintegration"|None, ...}.
    """
    best = None
    for ev in _stitch_events(recent):
        if not ev["is_run"]:
            continue
        days_since = (today - ev["start_date"]).days
        if days_since < 0 or days_since > RECOVERY_WINDOW_DAYS_ULTRA:
            continue
        single = ev["single_max_min"]
        tof = ev["time_on_feet_min"]
        dist = ev["distance_km"]
        suffer = ev["run_suffer"]
        qualifies = (
            single >= BIG_EVENT_MIN          # a single continuous ≥2h bout
            or dist >= BIG_EVENT_KM          # cumulative event distance
            or tof >= BIG_EVENT_TOF          # cumulative time-on-feet (backyard/24h)
            or suffer >= BIG_EVENT_SUFFER    # secondary HR-based trigger
        )
        if not qualifies:
            continue
        is_ultra = single >= ULTRA_MIN or dist >= ULTRA_KM or tof >= ULTRA_TOF
        window = RECOVERY_WINDOW_DAYS_ULTRA if is_ultra else RECOVERY_WINDOW_DAYS
        if days_since >= window:
            continue   # window already elapsed for this event
        # Tier-aware, recency-aware pick: prefer the higher-tier event, then the
        # one with more window remaining (so a midnight-split remnant can't shrink
        # an ultra to a standard window, and a still-active earlier ultra wins).
        cand = {"date": ev["start_date"].isoformat(), "days_since": days_since,
                "window_days": window, "suffer": round(suffer),
                "duration_min": round(tof), "single_max_min": round(single),
                "distance_km": round(dist, 1), "name": ev["name"],
                "tier": "ultra" if is_ultra else "big_event"}
        if best is None or (
            (cand["tier"] == "ultra", window - days_since)
            > (best["tier"] == "ultra", best["window_days"] - best["days_since"])
        ):
            best = cand

    if not best:
        return {"active": False, "phase": None}

    full_block_days = max(1, round(best["window_days"] * RECOVERY_FULL_BLOCK_FRAC))
    phase = "full_block" if best["days_since"] < full_block_days else "reintegration"
    days_remaining = best["window_days"] - best["days_since"]
    label = "24h/ultra" if best["tier"] == "ultra" else "big effort"
    note = (
        f"post-{label} recovery window — '{best['name']}' {best['days_since']}d ago "
        f"(~{best['duration_min']}min on feet, {best['distance_km']}km). Still in the "
        f"physiological recovery tail (hard work now raises illness/injury/overreach risk). "
        + ("Phase 1: easy Z2 + gym + rest only; hard/long/strides blocked."
           if phase == "full_block" else
           "Phase 2 (reintegration): hard runs blocked, long run discounted; "
           "rebuild gradually — prioritize sleep + carbs.")
        + f" Full clearance in {days_remaining}d."
    )
    return {"active": True, "phase": phase, "full_block_days": full_block_days,
            "days_remaining": days_remaining, "note": note, **best}


# ── HRV Baseline Analysis ─────────────────────────────────
def compute_hrv_status(oura_data):
    """Compute HRV status using RMSSD rolling avg vs individualized baseline.
    Returns dict with: status ('normal','suppressed','elevated'),
    rolling_avg, baseline_mean, baseline_sd, normal_low, normal_high.
    Research: HRV4Training methodology (mean +/- 0.5 SD)."""
    daily = oura_data.get("hrv", {}).get("daily", [])
    vals = [d["hrv"] for d in daily
            if isinstance(d, dict) and d.get("hrv") and d["hrv"] > 0]

    if len(vals) < 7:
        return {"status": "insufficient_data", "rolling_avg": 0,
                "baseline_mean": 0, "baseline_sd": 0,
                "normal_low": 0, "normal_high": 0}

    # Rolling 7-day average (smooths out expected 24-48h post-session dips)
    rolling = vals[-HRV_ROLLING_DAYS:]
    rolling_avg = statistics.mean(rolling)

    # Baseline must LAG the rolling window. If the baseline includes the current
    # 7 days, a genuine sustained suppression drags the mean down with it and
    # self-cancels — the longer the dip persists, the harder it gets to detect
    # (Plews/HRV4Training compute the reference as a window that excludes the
    # current rolling block). Once we have >=2x the rolling window of data,
    # exclude the last 7; below that, fall back to the full window (provisional).
    if len(vals) >= 2 * HRV_ROLLING_DAYS:
        pool = vals[:-HRV_ROLLING_DAYS]
        baseline = pool[-HRV_BASELINE_DAYS:]
        baseline_lagged = True
    else:
        baseline = vals[-HRV_BASELINE_DAYS:]
        baseline_lagged = False
    baseline_mean = statistics.mean(baseline)
    baseline_sd = statistics.stdev(baseline) if len(baseline) > 1 else 0

    # Normal range
    normal_low = baseline_mean - HRV_SD_BAND * baseline_sd
    normal_high = baseline_mean + HRV_SD_BAND * baseline_sd

    if rolling_avg < normal_low:
        status = "suppressed"
    elif rolling_avg > normal_high:
        status = "elevated"
    else:
        status = "normal"

    return {
        "status": status,
        "rolling_avg": round(rolling_avg, 1),
        "baseline_mean": round(baseline_mean, 1),
        "baseline_sd": round(baseline_sd, 1),
        "normal_low": round(normal_low, 1),
        "normal_high": round(normal_high, 1),
        "baseline_lagged": baseline_lagged,
    }


# ── Scoring ────────────────────────────────────────────────
def score_session(key, info, days, tsb, readiness, yest_type, week_hard,
                  hrv_trend, deload_mode, strides_ready,
                  days_since_gym, too_close_to_long, too_close_to_intervals,
                  ramp_too_fast, mid_quota_full, high_quota_full,
                  week_gym_count,
                  hrv_status=None, dow=None,
                  avg_sleep=None, rhr_trend=None,
                  muscle_emergency=False, volume_behind=False,
                  sleep_score=None):
    """Returns (score, block_reason). score < 0 means blocked."""

    # ---- Hard gates ----
    if readiness < READY_RED and key != "rest":
        return -1, f"readiness {readiness} < {READY_RED}"

    if tsb < info["min_tsb"]:
        return -1, f"TSB {round(tsb,1)} < min {info['min_tsb']}"

    if readiness < info["min_readiness"]:
        return -1, f"readiness {readiness} < {info['min_readiness']}"

    if info["type"] == "hard" and yest_type == "hard":
        return -1, "back-to-back hard days blocked"

    if info["type"] in ("hard","easy","moderate") and yest_type == "blocked_run":
        return -1, "day after very-high load — runs blocked"

    if info["type"] == "hard" and week_hard >= 2:
        return -1, f"{week_hard} hard sessions this week (max 2)"

    # HRV red day: falling trend + yellow readiness = no hard sessions
    if info["type"] == "hard" and hrv_trend == "FALLING" and readiness < READY_YELLOW:
        return -1, f"HRV falling + readiness {readiness} < {READY_YELLOW} — hard blocked"

    # RMSSD-based HRV gate (research: HRV4Training methodology)
    # 7-day rolling avg below individual baseline (mean - 0.5 SD) = ANS suppressed
    # Downgrade hard sessions to easy. Uses rolling avg to ignore expected 24-48h dips.
    if hrv_status and hrv_status.get("status") == "suppressed" and info["type"] == "hard":
        return -1, (f"HRV suppressed — 7d avg {hrv_status['rolling_avg']}ms "
                     f"below normal range [{hrv_status['normal_low']}-{hrv_status['normal_high']}ms]")

    # Cold-start fallback: with <7 HRV readings status is "insufficient_data" and the
    # suppression gate above silently can't fire — the HRV safety net disappears in
    # exactly the data-gap conditions (new device, sync hole, post-trip) it's meant for.
    # If the raw Oura HRV trend is FALLING, block hard conservatively (mirrors the LEA
    # cluster's existing insufficient_data+FALLING fallback).
    if (hrv_status and hrv_status.get("status") == "insufficient_data"
            and hrv_trend == "FALLING" and info["type"] == "hard"):
        return -1, "HRV cold-start (insufficient baseline) + falling trend — hard blocked conservatively"

    # Deload mode: hard sessions blocked. Two distinct triggers — name the real one:
    #   scheduled mesocycle deload (cycle_type=DELOAD) can fire with a perfectly fresh TSB,
    #   vs fatigue deload (TSB < danger threshold + poor recovery). Don't claim "TSB < -30"
    #   when TSB is actually positive — that misleads.
    if deload_mode and info["type"] == "hard":
        if tsb >= TSB_FATIGUED:
            return -1, f"scheduled mesocycle deload week (TSB {round(tsb,1)} fresh) — hard blocked"
        return -1, f"fatigue deload (TSB {round(tsb,1)} < {TSB_FATIGUED} + poor recovery) — hard blocked"

    # Intervals require neuromuscular prep: 2+ speed-work sessions in past 21 days.
    if key in ("intervals_vo2",) and not strides_ready:
        return -1, "intervals locked: need 2+ speed-work runs (avg HR >153 or peak >=168, 15-75 min) in past 21 days"

    # 48-72h recovery: gym -> no hard run within 2 days (research: connective tissue)
    if info["type"] == "hard" and days_since_gym < 2:
        return -1, f"gym {days_since_gym}d ago — need 48h before hard run"

    # Long run + intervals must be 3+ days apart
    if key == "long_run" and too_close_to_intervals:
        return -1, "intervals done <3 days ago — long run needs 3d spacing"
    if key in ("intervals_vo2",) and too_close_to_long:
        return -1, "long run done <3 days ago — intervals need 3d spacing"

    # CTL ramp too fast: block hard sessions to cap load accumulation
    if ramp_too_fast and info["type"] == "hard":
        return -1, "CTL ramp rate exceeds beginner limit (5pts/week) — hard blocked"

    # Zone distribution gates (Seiler 3-zone): cap the HIGH band's distribution and
    # the true MIDDLE (tempo). Easy aerobic volume is NOT capped and never blocks quality.
    if high_quota_full and key in ("intervals_vo2",):
        return -1, "high-intensity ceiling reached this week — no more intervals"
    if mid_quota_full and key == "threshold":
        return -1, "middle-zone (tempo) budget spent this week — no more threshold"

    # Full body gym recovery gate: 3 days minimum between sessions
    if key == "full_body_gym" and days_since_gym < 3:
        return -1, f"gym {days_since_gym}d ago — need 3d between full body sessions"

    # Weekly gym frequency cap: 2x/week full body
    if key == "full_body_gym" and week_gym_count >= 2:
        return -1, f"gym done {week_gym_count}x this week (cap 2)"

    # ---- Scoring ----
    overdue = min(days / info["ideal_freq"], 3.0)
    score   = info["base_score"] * overdue

    # TSB modifiers (calibrated to research thresholds)
    if tsb > TSB_PEAK and info["type"] == "hard":
        score *= 1.3    # peak freshness — push hard
    elif tsb < TSB_MODERATE and info["type"] == "hard":
        score *= 0.4    # fatigued — heavily discount
    elif tsb <= TSB_FATIGUED and info["type"] == "easy":
        score *= 1.5    # in danger zone — strongly prefer easy
    elif tsb <= TSB_FATIGUED and info["type"] == "gym":
        score *= 1.6    # gym adds minimal load, ideal when fatigued
    elif tsb <= TSB_FATIGUED and info["type"] == "moderate":
        score *= 1.3    # strides ok when fatigued, low stress

    # Rotation modifier: prefer sessions that match today's slot
    if dow and dow in ROTATION:
        if key in ROTATION[dow]:
            score *= ROTATION_ON_BOOST
        elif info["type"] in ("hard", "moderate"):
            score *= ROTATION_OFF_HARD
        else:
            score *= ROTATION_OFF_EASY

    # Sleep modifier: worse-of duration vs Oura sleep score (doc §5). Poor sleep
    # discounts hard work and boosts easy (VAT stores more on poor sleep).
    s_band = sleep_band(avg_sleep, sleep_score)
    if info["type"] == "hard":
        if s_band == "red":
            score *= 0.3
        elif s_band == "yellow":
            score *= 0.6
    elif s_band in ("yellow", "red") and info["type"] in ("easy", "gym"):
        score *= 1.2

    # RHR trend: rising RHR = classic overtraining signal, discount hard
    if rhr_trend == "RISING" and info["type"] == "hard":
        score *= 0.5

    # Muscle emergency: gym becomes top priority when Renpho shows loss
    if muscle_emergency and key == "full_body_gym":
        score *= 2.5

    # Volume urgency: behind on weekly km → boost easy/long runs
    if volume_behind and key in ("easy_z2", "long_run"):
        score *= 1.4

    return round(score, 1), None


# ── Pace helpers ───────────────────────────────────────────
def fmt_pace(speed):
    if not speed or speed <= 0:
        return "n/a"
    p = 1000 / (speed * 60)
    m, s = int(p), int(round((p % 1) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def pace_to_speed(pace):
    """Convert M:SS per km to m/s."""
    if not pace or not isinstance(pace, str) or ":" not in pace:
        return None
    try:
        minutes, seconds = pace.strip().split(":", 1)
        total = int(minutes) * 60 + int(seconds)
    except (TypeError, ValueError):
        return None
    return 1000 / total if total > 0 else None


def _shrink_speed_update(current, candidate, max_delta=0.03):
    """Limit self-calibrated pace jumps so one curve refresh cannot over-prescribe."""
    if not current or current <= 0:
        return candidate, False
    lo = current * (1 - max_delta)
    hi = current * (1 + max_delta)
    capped = max(lo, min(hi, candidate))
    return capped, abs(capped - candidate) > 1e-6


def _load_vo2_anchor():
    """Demonstrated VO2 rep pace from a completed interval session (data/vo2_anchor.json).
    Fills the gap where VDOT (continuous >=3km blocks only) can't learn from 4×4/5×3 reps:
    the interval pace would otherwise stay pinned to the conservative 5K anchor forever."""
    try:
        return json.loads((DATA_DIR / "vo2_anchor.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None


# Map VO2 section name → the vo2_anchor.json field the nightly loop writes its observed HR floor to.
_VO2_SECTION_HR_KEY = {"warmup": "warmup_hr_floor", "jog": "jog_hr_floor", "cooldown": "cooldown_hr_floor"}

def _vo2_section_hr(section, default):
    """Read-back HR for a VO2 warm-up/jog/cooldown section. These are PACE-mode sections
    (belt-set), so the HR is only shown as what-to-expect. The nightly calibration loop
    writes the observed HR floor into vo2_anchor.json; prefer it so the note self-updates.
    Bounded to a sane range (100-160) so a bad value can't render nonsense."""
    anc = _load_vo2_anchor() or {}
    val = anc.get(_VO2_SECTION_HR_KEY.get(section, ""))
    try:
        v = int(round(float(val)))
        if 100 <= v <= 160:
            return v
    except (TypeError, ValueError):
        pass
    return default


# Map section → the vo2_anchor.json field holding the nightly loop's calibrated BELT PACE
# for that section, plus a sane bound so a bad value can't render a nonsense belt speed.
_VO2_SECTION_PACE_KEY = {"warmup": "warmup_pace", "recovery": "recovery_pace", "cooldown": "cooldown_pace"}
_VO2_SECTION_PACE_BOUND = {"warmup": (420, 660), "recovery": (480, 720), "cooldown": (540, 720)}

def _vo2_section_pace(section, default):
    """Belt PACE for a VO2 warm-up/recovery/cooldown section. The nightly loop tunes each
    section's belt speed toward its target HR band and writes it to vo2_anchor.json; prefer
    that so the whole session self-calibrates. Falls back to `default` (the easy/recovery/walk
    pace) when uncalibrated. Bounded per section so a stray value can't render a bad belt."""
    anc = _load_vo2_anchor() or {}
    val = anc.get(_VO2_SECTION_PACE_KEY.get(section, ""))
    sec = _pace_seconds(val)
    if sec is not None:
        lo, hi = _VO2_SECTION_PACE_BOUND.get(section, (0, 10 ** 9))
        if lo <= sec <= hi:
            return val
    return default


def apply_calibrated_paces(paces_raw, calibration):
    """Use the passive CS curve for quality pace guidance when confidence is real.

    Easy/long runs stay HR-led from observed Z2 pace. The CS curve only updates
    hard-session pace targets, and only once outdoor evidence is thick enough.
    """
    merged = dict(paces_raw or {})
    meta = {
        "applied": False,
        "source": (paces_raw or {}).get("source", "paces_json"),
        "reason": "no calibration file",
    }
    if not isinstance(calibration, dict):
        return merged, meta

    # ── Quality-band paces come from the Daniels VDOT, NOT the treadmill anchor ─────
    # A treadmill threshold anchor can read optimistic vs demonstrated race fitness.
    # Prefer Daniels VDOT from a recent time-trial over a treadmill-only anchor.
    # An outdoor critical-speed curve can under-read the other way.
    # vdot_estimates.{tempo,interval}_pace are synced into paces.json as
    # {threshold,interval}_base — undamped, fitness-true, and self-tracking off the best
    # effort. Prescribe THOSE for quality reps; keep the treadmill anchor as context only.
    # Reps stay HR-led (90-95% HRmax) — pace is a treadmill-referenced guardrail, not a
    # target. (Replaces the old "treadmill anchor overrides everything, no smoothing" block
    # that produced the too-fast reps.)
    tm = calibration.get("treadmill_anchor")
    vdot_applied = {}
    for base_key, speed_key in (("threshold_base", "threshold_speed"),
                                ("interval_base", "interval_speed")):
        sp = pace_to_speed((paces_raw or {}).get(base_key))
        if sp:
            merged[speed_key] = round(sp, 4)
            vdot_applied[speed_key] = fmt_pace(sp)
    _vo2_src = None
    if vdot_applied:
        # ── Demonstrated VO2 anchor ─────────────────────────────────────────────
        # VDOT only reads continuous >=3km blocks, so a completed 4×4/5×3 session can NEVER
        # move the interval pace through the normal pipeline — it stays pinned to the
        # conservative 5K anchor. When the athlete runs the reps FASTER than that estimate
        # (HR confirming the VO2 band), capture it in data/vo2_anchor.json and prefer it here,
        # capped so a bad/typo value can't over-prescribe (never faster than 4:30/km). Only
        # active on the VDOT-base path (guarded by vdot_applied) so it can't leak into the
        # CS-curve fallback or the promotion-gate/shrink logic below.
        _vo2 = _load_vo2_anchor()
        if _vo2:
            _sp = pace_to_speed(_vo2.get("interval_pace"))
            if _sp and _sp <= pace_to_speed("4:30"):
                merged["interval_speed"] = round(_sp, 4)
                vdot_applied["interval_speed"] = fmt_pace(_sp)
                _vo2_src = _vo2.get("interval_pace")
        merged["source"] = "daniels_vdot_base" + (" + vo2_anchor" if _vo2_src else "")
        meta.update({
            "applied": True,
            "source": "daniels_vdot_base" + (" + vo2_anchor" if _vo2_src else ""),
            "reason": "quality bands from Daniels VDOT (paces.json *_base)"
                      + (f"; interval from demonstrated VO2 anchor {_vo2_src}" if _vo2_src else "")
                      + "; treadmill anchor kept as context only",
            "treadmill_anchor_interval": (tm or {}).get("interval") if isinstance(tm, dict) else None,
            "vo2_anchor_interval": _vo2_src,
            "paces": vdot_applied,
        })
        return merged, meta

    confidence = calibration.get("confidence")
    # Curve contributors (treadmill-inclusive when calibrate runs with INCLUDE_TREADMILL);
    # falls back to the legacy outdoor-only key for older calibration files.
    outdoor = calibration.get("n_curve_contributing")
    if outdoor is None:
        outdoor = calibration.get("n_outdoor_contributing") or 0
    bands = calibration.get("bands_provisional") or {}
    gate_missing = _nested(calibration, ["promotion_gate", "missing"], [])
    if confidence not in ("moderate", "high") or outdoor < 6:
        meta.update({
            "reason": (
                f"calibration observed only: confidence={confidence or 'unknown'}, "
                f"outdoor_contributors={outdoor}"
            ),
            "confidence": confidence,
            "outdoor_contributors": outdoor,
            "promotion_missing": gate_missing,
        })
        return merged, meta

    applied = {}
    capped_updates = {}
    for band_key, speed_key in (
        ("tenk", "tenk_pace_speed"),
        ("threshold", "threshold_speed"),
        ("interval", "interval_speed"),
    ):
        speed = pace_to_speed(bands.get(band_key))
        if speed:
            speed, capped = _shrink_speed_update(_num(merged.get(speed_key), 0), speed)
            merged[speed_key] = round(speed, 4)
            applied[speed_key] = fmt_pace(speed)
            if capped:
                capped_updates[speed_key] = "capped to +/-3% from prior pace"

    if not applied:
        meta.update({
            "reason": "calibration has no usable pace bands",
            "confidence": confidence,
            "outdoor_contributors": outdoor,
        })
        return merged, meta

    merged["source"] = "self_calibration_cs"
    meta.update({
        "applied": True,
        "source": "self_calibration_cs",
        "reason": "moderate-confidence outdoor CS curve",
        "confidence": confidence,
        "outdoor_contributors": outdoor,
        "paces": applied,
        "capped_updates": capped_updates,
    })
    return merged, meta


def adjusted_paces(paces_raw, readiness, tsb):
    if readiness < 65 or tsb < -40:
        mult = 0.92
    elif tsb < -25:
        mult = 0.96
    else:
        mult = 1.0

    def _get(key, fallback=2.15):
        return paces_raw.get(key, fallback)

    # Strides = short neuromuscular pickups, run FASTER than interval pace and NOT
    # damped by the detraining/fatigue `mult` (like walk). Derive from the interval
    # BASE pace (undamped) so it always stays faster than interval and auto-tracks
    # fitness instead of going stale. Fallbacks: live interval speed, then 4:40.
    # (was hardcoded to a stale interval-adjacent pace
    # — too slow for strides; his executed strides peak ~4:42/km.)
    STRIDES_FASTER_S = 30  # seconds/km faster than the interval base pace
    _iv_base_s = _pace_seconds(paces_raw.get("interval_base"))
    if _iv_base_s is None:
        _iv_speed = _get("interval_speed", 3.64)
        _iv_base_s = (1000.0 / _iv_speed) if _iv_speed else None
    strides_pace = (
        fmt_pace(1000.0 / max(_iv_base_s - STRIDES_FASTER_S, 1.0))
        if _iv_base_s else "4:40"
    )

    return {
        "easy":      fmt_pace(_get("easy_speed")      * mult),
        "z2":        fmt_pace(_get("z2_speed")         * mult),
        "threshold": fmt_pace(_get("threshold_speed", 2.78) * mult),
        "long":      fmt_pace(_get("long_speed")       * mult),
        "interval":  fmt_pace(_get("interval_speed", 3.64)  * mult),
        "recovery":  fmt_pace(_get("easy_speed")       * 0.87),
        # Single committed targets — one pace, never a range.
        # strides = short ~3-5K-effort accelerations; derived above as ~30s/km
        #           faster than interval BASE pace, undamped (see strides_pace).
        # walk    = the recovery-walk pace between reps/strides; fixed — it doesn't
        #           track running fitness or scale with the fatigue mult.
        "strides":   strides_pace,
        "walk":      "11:00",
        "z2_normal": fmt_pace(_get("z2_speed")),
        "mult":      mult,
    }


# ── Session builder ────────────────────────────────────────
def _pace_seconds(pace):
    if not pace or not isinstance(pace, str) or ":" not in pace:
        return None
    try:
        minutes, seconds = pace.split(":", 1)
        return int(minutes) * 60 + int(seconds)
    except (TypeError, ValueError):
        return None


# Quality-rep paces may come from treadmill efforts (VDOT off a recent 5K).
# Outdoors, treadmill-trained athletes often run slower at the same HR.
# Use a single offset for the outdoor guardrail. Reps are HR-led regardless of surface.
OUTDOOR_QUALITY_OFFSET_S = 25

def _pace_plus(pace, secs):
    """Add secs/km to a M:SS pace string (surface adjustment). Returns 'M:SS' or the input."""
    base = _pace_seconds(pace)
    if base is None:
        return pace
    total = base + int(secs)
    return f"{total // 60}:{total % 60:02d}"


DELOAD_LONG_RUN_FRAC = 0.7   # deload week: cut long-run volume ~30% (standard deload reduction)

def _long_run_target(cap_km, decoupling, deload=False):
    """Distance target + durability cap for the long run prescription."""
    target = _num(cap_km, 0)
    if target <= 0:
        target = 10.0
    target = round(target, 1)
    cap = target
    durability = "normal"
    stop_rule = "stop if HR drift/decoupling is clearly rising or legs get heavy"
    if decoupling and decoupling > 7:
        durability = "capped"
        cap = round(target * 0.85, 1)
        stop_rule = "recent decoupling >7% — keep lower Z2 and stop at first drift"
    elif decoupling and decoupling >= 5:
        durability = "watch"
        cap = round(target * 0.92, 1)
        stop_rule = "recent decoupling 5-7% — stay conservative; stop if HR drifts >5%"
    else:
        stop_rule = "recent decoupling <5% — normal long-run cap; still stop if HR drifts >5%"
    # Deload week: reduce volume ~30% so the long run actually deloads (recovery enables the
    # supercompensation that drives CTL/Z2 goals). Applied AFTER decoupling so it always wins
    # the lower of the two caps. The week-plan blocks hard work; this completes the deload.
    if deload:
        cap = round(min(cap, target * DELOAD_LONG_RUN_FRAC), 1)
        durability = "deload"
        stop_rule = (f"DELOAD week — long run cut to ~{int(DELOAD_LONG_RUN_FRAC*100)}% volume "
                     "(absorb training, supercompensate). Strictly easy Z2; stop if HR drifts >5%")
    return target, cap, durability, stop_rule


def _economy_cues():
    """Current cadence target + rotating form cue from the weekly_focus overlay
    (research 2026-06-19: HIS ~165 baseline → 173 → opt 178, easy-runs only; form cues).
    Robust fallback so the selector never breaks if the overlay is missing/stale."""
    cad, cue = 173, ""
    try:
        wf = json.loads((DATA_DIR / "weekly_focus.json").read_text())
        cad = int(wf.get("cadence_target_spm") or cad)
        cue = str(wf.get("form_cue") or "")
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, OSError):
        pass
    return cad, cue


def build_session(key, p, readiness, tsb, long_run_cap_km=None, decoupling=None, deload=False):
    """Returns (label, type_str, total_time, steps)."""
    # Research-backed economy layer (cadence/form) from the weekly_focus overlay.
    cad, cue = _economy_cues()
    cue_suffix = f" Form cue: {cue}" if cue else ""
    # Cadence retraining is EASY-RUNS ONLY (research): never prescribe a cadence number
    # on quality reps — turnover should rise naturally with pace there.
    REP_CADENCE = "Don't chase a cadence number on the reps — turnover rises naturally with pace (cadence work is easy-days only)."

    if key == "intervals_vo2":
        # Protocol rotation: 5×3min @ ~vVO2max is the weekly backbone; 4×4min
        # Helgerud rotates in ~every 3rd ISO week for variety. Both maximise
        # time≥90% VO2max far better than 400/800m repeats at lower injury cost
        # (research 2026-06-03). On a fatigued-but-eligible day, drop one rep.
        # Per-section belt paces: default to easy/recovery/walk, but the nightly loop
        # (calibrate_next_session) tunes each toward its target HR band and overrides here.
        wu_pace = _vo2_section_pace("warmup", p["easy"])
        rec_pace = _vo2_section_pace("recovery", p["recovery"])
        cd_pace = _vo2_section_pace("cooldown", p["walk"])
        rotate_4x4 = (selector_today().isocalendar()[1] % 3 == 0)
        if rotate_4x4:
            reps = 4 if tsb > TSB_MODERATE else 3
            return (
                f"HARD: {reps}×4min VO2max (Norwegian 4×4)", "hard", "50-60 min",
                [
                    # PACE-MODE session: every section is belt-pace + duration; HR shown as read-back only.
                    {"step":1,"name":"Warm-up","duration":"15 min","pace":wu_pace,"mode":"pace","hr":f"expect HR building to ~{_vo2_section_hr('warmup', 130)}",
                     "notes":f"Set belt to {wu_pace}/km. Easy jog building in. Last 4 min: 4×20s strides to prime legs (belt/manual, HR will spike — fine). Cadence: 165 spm."},
                    {"step":2,"name":f"{reps}×4min","duration":f"{reps} reps","pace":p["interval"],"rest_pace":rec_pace,"mode":"pace","hr":f"expect ~175 bpm by last min (mid 90-95% band, HRmax {HRMAX}) · rest ~138",
                     "notes":f"Set the belt to {p['interval']}/km and hold it EVEN across all {reps} reps (the belt IS the target; watch treadmill pace often under-reads). If end-of-rep HR sits on the low edge of VO2 with no drift, the next session should be a step faster. Target ~175 bpm by the last min (mid 90-95% HRmax). Outdoors ~{_pace_plus(p['interval'], OUTDOOR_QUALITY_OFFSET_S)}/km. Rest = 3 min easy jog @ {rec_pace}/km. Unable to hold a full sentence by the last min. {REP_CADENCE}"},
                    {"step":3,"name":"Easy jog","duration":"5 min","pace":rec_pace,"mode":"pace","hr":f"expect HR ~{_vo2_section_hr('jog', 130)} (post-VO2 floor)",
                     "notes":f"Set belt to {rec_pace}/km. Post-VO2 jog HR floors ~130-133 — it will NOT drop lower in 5 min unless you slow further; the loop tunes this. Cadence: 160 spm."},
                    {"step":4,"name":"Cooldown","duration":"10 min","pace":cd_pace,"mode":"pace","hr":f"expect HR down to ~{_vo2_section_hr('cooldown', 118)} — WALK it",
                     "notes":f"Set the belt to a WALK (~{cd_pace}/km). A jog near easy-Z2 pace will not drop HR. Only walking brings HR into the 110s-120s. Walk the full 10 min (or jog 2-3 min then walk the rest). Light calf + hip mobility (not aggressive stretching)."},
                ]
            )
        reps = 5 if tsb > TSB_MODERATE else 4
        return (
            f"HARD: {reps}×3min VO2max Intervals", "hard", "50-60 min",
            [
                # PACE-MODE session: every section is belt-pace + duration; HR shown as read-back only.
                {"step":1,"name":"Warm-up","duration":"15 min","pace":wu_pace,"mode":"pace","hr":f"expect HR building to ~{_vo2_section_hr('warmup', 130)}",
                 "notes":f"Set belt to {wu_pace}/km. Easy jog building in. Last 4 min: 3-4×20s strides at 5K pace, full recovery (HR will spike — fine). Cadence: 165 spm."},
                {"step":2,"name":f"{reps}×3min","duration":f"{reps} reps","pace":p["interval"],"rest_pace":rec_pace,"mode":"pace","hr":f"expect ~175 bpm by last min (of HRmax {HRMAX}) · rest ~138",
                 "notes":f"Set the belt to {p['interval']}/km and hold each 3-min rep EVEN (the belt IS the target; watch treadmill pace often under-reads). HR should reach ~175 by the last minute (~92% of HRmax {HRMAX}, mid VO2 band). ~3-5K race effort. Outdoors ~{_pace_plus(p['interval'], OUTDOOR_QUALITY_OFFSET_S)}/km. Rest = 3 min easy jog @ {rec_pace}/km. Stop if pace fades >5s or form collapses. ~{reps*3} min total in Z4-Z5. {REP_CADENCE}"},
                {"step":3,"name":"Easy jog","duration":"5 min","pace":rec_pace,"mode":"pace","hr":f"expect HR ~{_vo2_section_hr('jog', 130)} (post-VO2 floor)",
                 "notes":f"Set belt to {rec_pace}/km. Post-VO2 jog HR floors ~130-133 unless you slow further; the loop tunes this. Cadence: 160 spm."},
                {"step":4,"name":"Cooldown","duration":"10 min","pace":cd_pace,"mode":"pace","hr":f"expect HR down to ~{_vo2_section_hr('cooldown', 118)} — WALK it",
                 "notes":f"Set the belt to a WALK (~{cd_pace}/km). Jogging the cooldown often holds HR near Z2. Only walking brings HR into the 110s-120s. Walk the full 10 min (or jog 2-3 min then walk). Light calf + hip mobility."},
            ]
        )

    if key == "threshold":
        # Alternates by ISO week (research 2026-06-03 Week A / Week B):
        #   Week A = threshold tempo (raises sustainable speed / lactate clearance)
        #   Week B = continuous 10K-pace distance work (race-specific)
        # Both are the #1/#2 ranked 10K-transfer-per-fatigue sessions for this goal.
        # Interface contract: distance-mode workouts accept only
        # a pace and a distance. Do not emit distance reps, recoveries, or a rest pace;
        # use time-based sessions when recoveries are required.
        if selector_today().isocalendar()[1] % 2 == 0:
            return (
                "HARD: 5 km at current 10K pace", "hard", "45-55 min",
                [
                    # PACE-MODE session: every section is belt-pace + duration; HR shown as read-back only.
                    {"step":1,"name":"Warm-up","duration":"12 min","pace":p["easy"],"mode":"pace","hr":"expect HR building to ~140",
                     "notes":f"Set belt to {p['easy']}/km. Easy jog. 4×15s strides in last 3 min. Cadence: 165 spm."},
                    {"step":2,"name":"10K-pace run","duration":"5 km","pace":p["threshold"],"mode":"pace",
                     "notes":f"Distance mode: enter 5 km at {p['threshold']}/km. Continuous, even running at current 10K pace. Do not add recoveries or a second pace."},
                    {"step":3,"name":"Cooldown","duration":"10 min","pace":p["easy"],"mode":"pace","hr":"expect HR settling to ~130s · walk last 3 min",
                     "notes":f"Set belt to {p['easy']}/km. Easy jog, walk last 3 min. Calf + hip mobility. Cadence: natural."},
                ]
            )
        return (
            "HARD: Threshold Tempo", "hard", "45-55 min",
            [
                # PACE-MODE session: every section is belt-pace + duration; HR shown as read-back only.
                {"step":1,"name":"Warm-up","duration":"10 min","pace":p["easy"],"mode":"pace","hr":"expect HR building to ~140",
                 "notes":f"Set belt to {p['easy']}/km. Easy jog. 4×15s pickups in last 3 min. Cadence: 165 spm."},
                {"step":2,"name":"3×10min @ threshold","duration":"3 × 10 min","pace":p["threshold"],"rest_pace":p["recovery"],"mode":"pace","hr":"expect ~168-174 bpm (ceiling ~175)",
                 "notes":f"Set belt to {p['threshold']}/km. Comfortably hard, short phrases only. True threshold sits ~90% HRmax, not mid-Z3. 3 min jog @ {p['recovery']}/km between blocks. Hold pace steady. If HR sits well below threshold at this pace, move the belt up. {REP_CADENCE}"},
                {"step":3,"name":"Cooldown","duration":"10 min","pace":p["easy"],"mode":"pace","hr":"expect HR settling to ~130s · walk last 3 min",
                 "notes":f"Set belt to {p['easy']}/km. Walk or very easy jog. Calf stretch. Cadence: natural."},
            ]
        )

    if key == "long_run":
        t, ceil, _ = karvonen_z2_target(tsb)
        t_low = t - 3  # start conservative
        t_high = min(t + 5, ceil)  # progress in back half
        target_km, cap_km, durability, stop_rule = _long_run_target(long_run_cap_km, decoupling, deload=deload)
        pace_sec = _pace_seconds(p.get("long"))
        est_min = round((cap_km * pace_sec) / 60) if pace_sec else None
        duration = f"~{est_min} min" if est_min else "75-120 min"
        return (
            "EASY: Long Z2", "easy", f"{cap_km:g} km cap ({duration})",
            [
                # Long Z2 = pure aerobic durability block. No strides appended:
                # strides are neuromuscular speed work with their own session type; bolting them on
                # contradicts the easy-aerobic intent and is doubly wrong on a deload week where
                # standalone strides are blocked. Want pickups → run the dedicated strides session.
                {"step":1,"name":"Z2 Block","duration":f"{cap_km:g} km max","pace":p["long"],"mode":"hr","hr":f"target HR {t_low}-{t} bpm · ceiling {ceil} bpm",
                 "notes":f"HR-LED — let pace float to hold the HR (guide pace ~{p['long']}/km). Week-plan target {target_km:g}km; today's durability cap {cap_km:g}km ({durability}). One continuous block. First 10 min naturally serves as warm-up. Start conservative at {t_low}, progress to {t} in back half. Push toward {t_high} only if smooth. {stop_rule}. Hard stop if HR exceeds {ceil} for >2 min. Cadence target ~{cad} spm (easy-run cadence — build from your ~165 base, don't force 180).{cue_suffix}"},
            ]
        )

    if key == "easy_z2":
        t, ceil, lbl = karvonen_z2_target(tsb)
        return (
            "EASY: Z2 60min", "easy", "60 min",
            [
                {"step":1,"name":"Z2 Block","duration":"60 min","pace":p["z2"],"mode":"hr","hr":f"{lbl} · ceiling {ceil} bpm",
                 "notes":f"One continuous block. No separate warm-up — first 5-10 min naturally serves as warm-up. HR-led, let pace float (guide pace ~{p['z2']}/km). Chest strap. Cadence target ~{cad} spm (easy-run cadence work — build from your ~165 base; don't force 180). Optional 1-2×/wk: 4-6×1min at +5-10% cadence, 2min normal between — cuts impact loading ~18% (back off if HR/RPE jumps).{cue_suffix}"},
            ]
        )

    if key == "strides":
        t, ceil, lbl = karvonen_z2_target(tsb)
        return (
            "Z2 60min + Strides", "easy", "~70 min",
            [
                {"step":1,"name":"Z2 Block","duration":"60 min","pace":p["z2"],"mode":"hr","hr":f"target HR {t} bpm · ceiling {ceil} bpm",
                 "notes":f"One continuous block. No separate warm-up — first 5-10 min naturally serves as warm-up. HR-led, let pace float (guide pace ~{p['z2']}/km). Chest strap. Cadence target ~{cad} spm (easy-run cadence work — build from your ~165 base; don't force 180). Optional 1-2×/wk: 4-6×1min at +5-10% cadence, 2min normal between — cuts impact loading ~18% (back off if HR/RPE jumps).{cue_suffix}"},
                # Pickups are run by EFFORT (~3-5K), not a strict pace or HR target — mode:"effort"
                # exempts this sub-block from the single-mode gate (the one justified exception).
                {"step":2,"name":"Strides","duration":f"6 × (20s @ {p['strides']} → 90s walk @ {p['walk']})","pace":p['strides'],"mode":"effort","hr":"n/a",
                 "notes":"Relaxed accelerations at ~3-5K effort. NOT sprints. Walk is full recovery — let HR settle. Focus on form, quick turnover. Cadence: 180+ spm."},
            ]
        )

    if key == "full_body_gym":
        return build_gym_session(key)

    if key == "rest":
        return (
            "REST: Recovery Day", "rest", "0 min",
            [
                {"step":1,"name":"Rest","duration":"All day","pace":"n/a","mode":None,"hr":"n/a",
                 "notes":"Full rest. Walk, stretch, foam roll if desired. No structured training."},
            ]
        )

    return (
        "EASY: Z2 60min", "easy", "60 min",
        [
            {"step":1,"name":"Z2 Block","duration":"60 min","pace":p.get("z2","7:45"),"mode":"hr","hr":"Z2",
             "notes":f"Fallback session for unknown key '{key}'. HR-led, let pace float."},
        ]
    )

def _last_gym_variant(recent_activities):
    """Derive last A/B variant from Strava activity names.
    Activities are named 'Full Body - A', 'Full Body - B', etc.
    Returns 'a' or 'b' (or 'b' as default so first pick is 'a')."""
    for act in recent_activities:
        if act.get("type") != "WeightTraining":
            continue
        name = (act.get("name") or "").lower()
        if "- b" in name or "b" == name.split("- ")[-1].strip():
            return "b"
        if "- a" in name or "a" == name.split("- ")[-1].strip():
            return "a"
    return "b"


# Module-level cache for recent activities (set in main before build_session)
_recent_activities = []


def build_gym_session(key):
    """Load full body gym program, alternate A/B based on last session."""
    programs_path = DATA_DIR / "gym_programs.json"
    try:
        programs = json.loads(programs_path.read_text())
    except Exception:
        return ("GYM: Full Body", "gym", "35-40 min", [
            {"step":1,"name":"Full Body Session","duration":"35-40 min","pace":"n/a","hr":"n/a",
             "notes":"2x5 compounds at 75-80% 1RM. See gym_programs.json."},
        ])

    # Alternate A/B: always derive from Strava history (last_session field was stale)
    last = _last_gym_variant(_recent_activities)

    variant = "a" if last == "b" else "b"
    program_key = f"full_body_{variant}"

    program = programs.get(program_key, {})
    if not program or "exercises" not in program:
        return ("GYM: Full Body", "gym", "35-40 min", [
            {"step":1,"name":"Full Body","duration":"35-40 min","pace":"n/a","hr":"n/a",
             "notes":"See gym_programs.json"},
        ])

    # Build steps from exercises
    steps = []
    for ex in program["exercises"]:
        rpe_note = f" RPE {ex['rpe']}" if ex.get("rpe") else ""
        rest_note = f" Rest {ex['rest']} between sets." if ex.get("rest") else ""
        steps.append({
            "step":  ex["step"],
            "name":  f"{ex['name']} — {ex['sets']}x{ex['reps']}{rpe_note}",
            "duration": f"{ex['sets']} sets",
            "rest":  ex.get("rest", ""),
            "pace": "n/a",
            "hr":   "n/a",
            "notes": f"{ex['notes']}{rest_note}",
        })

    label = f"GYM: {program['name']}"
    return (label, "gym", program["total_time"], steps)


# ── Main ───────────────────────────────────────────────────
def main():
    apply_runtime_env()
    today = selector_today()
    dow = today.strftime("%A")  # "Monday", "Tuesday", etc.

    # Load data (with defensive defaults for missing/corrupt files)
    def _load_json(filename, default=None):
        path = DATA_DIR / filename
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"WARNING: {filename}: {e} — using defaults", file=sys.stderr)
            return default if default is not None else {}

    # Fail-safe data integrity: a missing/stale CRITICAL input must NOT default to a
    # permissive (rested/fresh) state — that prescribes a hard session on a bad-data day.
    # We record each issue, then below (a) block hard runs and (b) flag the trust badge RED.
    data_issues = []
    readiness_data = _load_json("readiness.json", None)
    if not readiness_data:
        data_issues.append("readiness.json missing")
        readiness_data = {"score": 50, "stale": True, "stale_reason": "readiness.json missing"}
    paces_raw      = _load_json("paces.json", None)
    if not paces_raw:
        # Missing/corrupt → generic fallback speeds. NOT permissive: record as a data
        # issue so hard/long pace-led work is blocked + the trust badge goes RED.
        data_issues.append("paces.json missing/corrupt — pace-led targets would use generic fallback speeds, not your data")
        paces_raw = {
            "easy_speed": 2.15, "z2_speed": 2.15, "threshold_speed": 2.78,
            "long_speed": 2.15, "interval_speed": 3.64,
        }
    else:
        # Staleness guard: a dead refresh pipeline must not silently serve old paces.
        _p_updated = paces_raw.get("updated")
        try:
            _p_age = (today - datetime.strptime(str(_p_updated), "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            _p_age = None
        if _p_age is None:
            data_issues.append("paces.json has no valid 'updated' date — cannot confirm paces reflect current fitness")
        elif _p_age > PACES_STALE_DAYS:
            data_issues.append(f"paces.json stale ({_p_age}d old — refresh pipeline likely down) — pace-led targets not tracking current data")
    calibration    = _load_json("calibration.json", None)
    paces_raw, calibration_pace_source = apply_calibrated_paces(paces_raw, calibration)
    _fitness_raw   = _load_json("fitness_freshness.json", None)
    fitness_data   = (_fitness_raw or {}).get("current", {})
    if not fitness_data:
        data_issues.append("fitness_freshness.json missing/empty (TSB/CTL gates would be blind)")
    oura_data      = _load_json("oura_trends.json", None)
    if not oura_data:
        data_issues.append("oura_trends.json missing (HRV/sleep/RHR gates would be blind)")
        oura_data = {}
    briefing_ctx   = _load_json("briefing_context.json", {})
    recovery_signals = _load_json("recovery_signals.json", {})
    if recovery_signals:
        briefing_ctx["recovery_signals"] = recovery_signals

    # VO2max: bank the latest reading and derive a trend once enough accumulate.
    vo2_history = update_vo2max_history(recovery_signals)
    vo2max = vo2max_status(vo2_history)

    # Eight Sleep: bank RR/HRV history and compute the personal-baseline RR illness
    # watch. Stash back into recovery_signals so compute_decision_models can read it
    # (same dict reference already placed in briefing_ctx above).
    if recovery_signals:
        es_history = update_eight_sleep_history(recovery_signals)
        es_rr = eight_sleep_rr_status(es_history)
        if isinstance(recovery_signals.get("eight_sleep"), dict):
            recovery_signals["eight_sleep"]["rr_status"] = es_rr

    # Acute sleep-debt recovery gate (computed once; consumed in decision models).
    sleep_debt = compute_sleep_debt(oura_data, today)

    readiness = readiness_data.get("score", 50)
    if readiness_data.get("stale"):
        data_issues.append(("readiness stale — " + (readiness_data.get("stale_reason") or "Oura not synced today")))
        readiness = min(readiness, 55)  # can't trust a high score on stale data → cap to block hard
    injury    = readiness_data.get("components", {}).get("injury_flag", 0)
    # Manual illness override — read directly from illness.json so
    # it takes effect on the next run without re-running readiness. Fever/sick =
    # full REST (not even gym), and the cut/LEA reasoning is replaced with illness
    # guidance (HRV/RHR/ACWR are confounded while ill, so they must not drive it).
    _illness_override = _load_json("illness.json", {})
    illness_expires = _illness_override.get("expires", "")
    manual_illness = bool(_illness_override) and (
        not illness_expires or today.isoformat() <= illness_expires)
    tsb       = fitness_data.get("form_tsb", 0)
    ctl       = fitness_data.get("fitness_ctl", 0)
    atl       = fitness_data.get("fatigue_atl", 0)
    _acwr_raw = _nested(briefing_ctx, ["acwr", "acwr"])
    acwr      = _num(_acwr_raw, 0)
    if _acwr_raw is None:
        # acwr field absent (briefing_context missing/stale) → acwr coerces to 0, which is
        # FALSY and silently bypasses the >1.5/>1.8 ACWR injury gates. Flag it so the block
        # isn't blind. (A genuine computed 0 = no acute load = correctly not gated.)
        data_issues.append("acwr unavailable (briefing_context missing/stale) — ACWR injury gates blind")

    # Renpho body comp — adjust volume floor based on VAT/body fat trends
    body_comp = {}
    try:
        body_comp = json.loads((DATA_DIR / "health_snapshot.json").read_text())
    except Exception:
        pass
    vat = body_comp.get("visceral_fat_7d_avg") or body_comp.get("visceral_fat")
    bf  = body_comp.get("body_fat_7d_avg") or body_comp.get("body_fat")
    muscle = body_comp.get("muscle_mass")
    volume_floor = VOLUME_FLOOR_MIN
    body_comp_note = ""
    if vat and vat >= 8.0:
        volume_floor = 180  # VAT still high — more Z2 volume needed
        body_comp_note = f"VAT {vat} ≥ 8.0 → volume floor raised to 180 min/week"
    elif bf and bf > 17.0:
        volume_floor = 165  # body fat above target range
        body_comp_note = f"BF {bf}% > 17% → volume floor raised to 165 min/week"

    # Muscle protection: set MUSCLE_BASELINE to lean mass. Losing muscle = cutting too hard
    # or gym adherence failure. Boost gym urgency when muscle is at risk.
    MUSCLE_BASELINE = 50.0  # set to your lean-mass baseline (kg)
    muscle_alert = ""
    muscle_emergency = False
    if muscle and muscle < MUSCLE_BASELINE - 0.5:
        muscle_alert = f"MUSCLE LOSS: {muscle}kg < baseline {MUSCLE_BASELINE}kg — gym is critical"
        muscle_emergency = True
    elif muscle and muscle < MUSCLE_BASELINE:
        muscle_alert = f"Muscle {muscle}kg below baseline {MUSCLE_BASELINE}kg — protect gym adherence"

    # Load week plan (periodization + progression)
    week_plan_path = DATA_DIR / "week_plan.json"
    week_plan = None
    if week_plan_path.exists():
        try:
            week_plan = json.loads(week_plan_path.read_text())
        except (json.JSONDecodeError, KeyError):
            week_plan = None

    # Week boundary used by both Strava fallback and weekly stats.
    dow_int = today.weekday()                          # 0=Mon
    monday  = today - timedelta(days=dow_int)

    # Recent activities: JSON file, or SESSION_SELECTOR_RECENT_CMD (prints JSON).
    # Lookbacks span up to 35 days (volume ramp, speed-work gate, big-event window).
    recent = load_recent_activities()

    # Normalize: drop items without a parseable date, coerce date to YYYY-MM-DD
    # and guarantee a "type" key. Downstream uses a["date"]/a["type"] with bracket
    # access, so one malformed Strava item would otherwise crash the whole run.
    def _clean_act(a):
        if not isinstance(a, dict):
            return None
        day = a.get("date")
        if not isinstance(day, str) or len(day) < 10:
            return None
        day = day[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return None
        out = {**a, "date": day, "type": a.get("type", "")}
        # Heat-correct avg HR at ingestion so EVERY downstream HR gate (classify,
        # week_hard, strides_ready, rolling-density) sees the same de-rated value.
        # Raw preserved as avg_hr_raw; no-op for treadmill / missing-temp runs.
        raw_hr = out.get("avg_hr")
        adj_hr, sub = heat_hr_correction(
            raw_hr, out.get("average_temp"), bool(out.get("trainer")))
        if sub > 0:
            out["avg_hr_raw"] = raw_hr
            out["avg_hr"] = adj_hr
            out["heat_corr_bpm"] = sub
        return out

    recent = [c for c in (_clean_act(a) for a in recent) if c]

    if not recent:
        # Strava 60-run fetch returned nothing (subprocess failed OR no runs in 60d) → fell
        # back to the ~7-day briefing set. Flag it: the 21d/35d speed-work & density gates will
        # under-count, so a resulting intervals/hard block is a data artifact (trust badge RED),
        # not a genuine training-state decision.
        data_issues.append("no recent runs from Strava (fetch failed or none in 60d) — "
                            "using ~7-day briefing fallback; 21d/35d lookback gates degraded")
        recent = recent_from_briefing(briefing_ctx)
        gym_done_from_plan = _num(_nested(week_plan or {}, ["budget", "full_body_gym", "done"]), 0)
        if gym_done_from_plan > 0 and not any(a.get("type") == "WeightTraining" for a in recent):
            recent.append({
                "name": "Full Body Gym",
                "type": "WeightTraining",
                "date": str(monday),
                "distance_km": 0,
                "duration_min": 40,
                "avg_hr": None,
                "max_hr": None,
            })

    # Week stats (since Monday)
    week_acts = [a for a in recent
                 if datetime.strptime(a["date"],"%Y-%m-%d").date() >= monday]

    week_km    = sum(a.get("distance_km",0) for a in week_acts if a["type"]=="Run")
    runs_done  = sum(1 for a in week_acts if a["type"]=="Run")
    week_gym_count = sum(1 for a in week_acts if a["type"]=="WeightTraining")
    def _is_hard_run(a):
        # Mirror classify()'s 'hard' rule so the density caps can't be defeated by a
        # threshold run whose warm-up/recoveries drag avg_hr <=158 (e.g. avg 155/max 180):
        # such a run counts toward the weekly QUALITY quota but, under the old avg_hr>158
        # rule, NOT toward the hard-density caps — letting 3 hard sessions slip through.
        if a.get("type") != "Run":
            return False
        hr = a.get("avg_hr") or 0
        mx = a.get("max_hr") or 0
        dur = a.get("duration_min") or 0
        return (hr > 158 or (mx >= 173 and hr > 149)) and dur <= 75

    week_hard  = sum(1 for a in week_acts if _is_hard_run(a))
    # Rolling 7-day hard-density (boundary-robust). The Mon-anchored week_hard cap
    # resets every Monday, so Sun-hard + Mon-hard + Wed-hard reads as "1 last week,
    # 2 this week" and sails through — yet that's 3 hard in 5 days, exactly the kind
    # of acute density ATL smooths over. Count hard runs in the trailing 7 days
    # (incl. any already completed today) regardless of week boundary.
    rolling_hard_7d = sum(
        1 for a in recent
        if _is_hard_run(a)
        and 0 <= (today - datetime.strptime(a["date"], "%Y-%m-%d").date()).days <= 6)

    # Live weekly category completion (drives quota floors + scoreboard).
    # Threshold quota is satisfied by any quality/hard run (threshold or intervals).
    week_threshold_done = sum(1 for a in week_acts
                              if classify(a) in ("threshold","intervals_vo2"))
    week_long_done = sum(1 for a in week_acts if classify(a) == "long_run")
    # Recovery days = elapsed dates (Mon..today) whose hardest stimulus was easy/rest:
    # no gym, no long run, no quality run, no run with avg HR above the Z2 ceiling (~147).
    # An empty elapsed day = rest taken. Today only counts once an easy/rest day is logged
    # (the day isn't over, so don't credit an unworked rest prematurely).
    _acts_by_date = {}
    for a in week_acts:
        _acts_by_date.setdefault(a["date"], []).append(a)
    week_recovery_days = 0
    _d = monday
    while _d <= today:
        day_acts = _acts_by_date.get(str(_d), [])
        day_hard = any(
            a["type"] == "WeightTraining"
            or classify(a) in ("threshold","intervals_vo2","long_run")
            or (a["type"] == "Run" and (a.get("avg_hr") or 0) > 147)
            for a in day_acts
        )
        if _d == today:
            is_recovery = bool(day_acts) and not day_hard
        else:
            is_recovery = not day_hard
        if is_recovery:
            week_recovery_days += 1
        _d += timedelta(days=1)

    # Yesterday context
    yesterday  = today - timedelta(days=1)
    yest_acts  = [a for a in recent
                  if datetime.strptime(a["date"],"%Y-%m-%d").date() == yesterday]
    yest_types = [classify(a) for a in yest_acts if classify(a)]
    yest_run_min = sum(a.get("duration_min",0) for a in yest_acts if a["type"]=="Run")
    yest_run_km  = sum(a.get("distance_km",0) for a in yest_acts if a["type"]=="Run")
    yest_gym     = any(a["type"]=="WeightTraining" for a in yest_acts)

    # Combined load label — intensity-aware
    yest_hard_run = any(a["type"]=="Run" and (a.get("avg_hr") or 0) > 150
                        for a in yest_acts)
    if (yest_hard_run and yest_run_min > 50 and yest_gym) or \
       (yest_hard_run and yest_run_min > 60) or yest_run_min > 90:
        yest_load = "very_high"
    elif (yest_hard_run and yest_run_min > 30) or yest_run_min > 60:
        yest_load = "high"
    elif yest_run_min > 0:
        yest_load = "moderate"
    else:
        yest_load = "none"

    # Yesterday session type (for gates)
    if any(t in ("intervals_vo2","threshold") for t in yest_types):
        yest_type = "hard"
    elif "long_run" in yest_types:
        yest_type = "long_run"
    elif yest_load == "very_high":
        yest_type = "blocked_run"   # blocks all runs today
    else:
        yest_type = "easy"

    # Days since each session type
    ds = days_since_each(recent, today)

    # Big-event recovery window (post-ultra recovery guard). Anchored to the event
    # date, NOT TSB — TSB decays back to neutral within days and would otherwise
    # wave through hard work while the athlete is still in the recovery tail.
    big_event = detect_big_event(recent, today)

    # Illness return-to-play ramp: 0-N days AFTER the sick flag's expiry, block
    # hard/long/strides. No new persistence — keyed off the existing illness.json
    # expiry. Inactive while the illness itself is still active (manual_illness).
    # TSB looks fresh post-illness because load decayed while resting; ignore it.
    illness_ramp = {"active": False}
    if _illness_override and illness_expires and not manual_illness:
        try:
            _exp = datetime.strptime(str(illness_expires)[:10], "%Y-%m-%d").date()
            _dsc = (today - _exp).days
            if 0 <= _dsc <= ILLNESS_RAMP_DAYS:
                illness_ramp = {
                    "active": True, "days_since_clear": _dsc,
                    "note": (f"illness return-to-play ramp — sick flag cleared {_dsc}d ago. "
                             f"Easy Z2 / gym / rest only for the first {ILLNESS_RAMP_DAYS} days "
                             f"back (no hard/long/strides; post-viral hard efforts risk "
                             f"myocarditis). TSB looks fresh because load decayed while ill — "
                             f"ignore it.")}
        except ValueError:
            pass

    # Heat summary (advisory — no blocking gate). Surfaces which recent runs had their
    # HR de-rated for heat, and nudges hydration/recovery after a hot outdoor session.
    heat_corrected = [
        {"date": a["date"], "name": a.get("name"), "temp_c": a.get("average_temp"),
         "raw_hr": a.get("avg_hr_raw"), "adj_hr": a.get("avg_hr"), "bpm": a.get("heat_corr_bpm")}
        for a in recent if a.get("heat_corr_bpm")]
    _recent_hot = False
    for a in recent:
        if a.get("type") != "Run" or a.get("trainer") or _num(a.get("average_temp"), -99) < HEAT_FLAG_C:
            continue
        try:
            if 0 <= (today - datetime.strptime(a["date"], "%Y-%m-%d").date()).days <= 1:
                _recent_hot = True
                break
        except (ValueError, TypeError):
            continue
    heat_summary = {
        "corrected_runs": heat_corrected[:5],
        "recent_hot": _recent_hot,
        "note": ("recent outdoor run in notable heat (≥28°C device temp) — prioritize "
                 "rehydration + electrolytes before the next quality day; an HRV dip "
                 "tonight is expected heat recovery, not illness." if _recent_hot else ""),
    }

    # HRV trend from oura
    hrv_trend = oura_data.get("hrv", {}).get("trend", "")

    # RMSSD-based HRV status (research-validated gate)
    hrv_status = compute_hrv_status(oura_data)

    # Deload mode: composite gate (TSB alone is a lagging calc — confirm with body signals)
    # Research-backed: TSB < -30 is a training-load risk marker, not a recovery state.
    # Require at least one objective recovery signal to also be poor before locking deload.
    poor_recovery = (
        readiness < READY_YELLOW
        or hrv_status.get("status") == "suppressed"
        or hrv_trend == "FALLING"
    )
    deload_mode = tsb < TSB_FATIGUED and poor_recovery

    # ── Single-session distance-spike guard (executed acute load) ──────────
    # A single run far above recent long-run distance is an injury-risk spike
    # (PMID 25155475, ≥30% distance jumps). Neither the duration-based yest_load gate
    # (>90min) nor the big-event guard (≥25km) catches a FAST, sub-25km, sub-90min
    # overshoot (e.g. 17km in 80min when the long-run cap is 11km, +55%). Flag it and,
    # if it landed yesterday/today, hold hard+long for a day. Distinct from the
    # volume-RAMP guard, which caps the prescribed *target*, not what was actually run.
    # Baseline = long runs OLDER than the spike window (>2d), so the spike run can't
    # inflate the very median used to judge it (else it under-fires on sparse history).
    _baseline_longs = []
    for a in recent:
        if classify(a) != "long_run":
            continue
        try:
            _bd = (today - datetime.strptime(a["date"], "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            continue
        if _bd > 2:
            _baseline_longs.append(_num(a.get("distance_km"), 0))
    _baseline_longs.sort(reverse=True)
    typical_long_km = statistics.median(_baseline_longs[:6]) if _baseline_longs else 0
    SPIKE_FACTOR = 1.5
    volume_spike = {"active": False}
    if typical_long_km > 0:
        for a in recent:
            if a.get("type") != "Run":
                continue
            try:
                _dsv = (today - datetime.strptime(a["date"], "%Y-%m-%d").date()).days
            except (ValueError, TypeError):
                continue
            _dkm = _num(a.get("distance_km"), 0)
            if 0 <= _dsv <= 2 and _dkm >= SPIKE_FACTOR * typical_long_km and _dkm < BIG_EVENT_KM:
                volume_spike = {
                    "active": True, "days_since": _dsv, "km": round(_dkm, 1),
                    "typical_long_km": round(typical_long_km, 1),
                    "note": (f"single-run distance spike — {_dkm:.1f}km vs typical long "
                             f"{typical_long_km:.1f}km ({_dsv}d ago, "
                             f"+{round((_dkm / typical_long_km - 1) * 100)}% acute jump). "
                             f"Injury-risk; hold hard/long for a day, keep it easy.")}
                break

    # Neuromuscular readiness for VO2max intervals: 2+ speed-work sessions in past 21 days.
    # Strava summary HR can't detect in-run strides (pickups spike max HR but avg HR stays
    # Z2), so avg HR > 153 (above Z2 upper 149) OR a hard peak (max >= 168 rep-band floor
    # on avg > 145), + duration 15-75min, is the proxy for threshold/interval/moderate
    # efforts. The peak clause matches classify(): without it a real interval session
    # (Jul-08 5x3: avg 146.7/max 171) never counted, so interval sessions could never
    # unlock intervals. v2.1 plan has 1 hard run/week, so 2 in 21 days = normal adherence.
    recent_21 = [a for a in recent
                 if (today - datetime.strptime(a["date"],"%Y-%m-%d").date()).days <= 21]
    speed_sessions = sum(
        1 for a in recent_21
        if a["type"] == "Run"
        and ((a.get("avg_hr") or 0) > 153
             or ((a.get("max_hr") or 0) >= 168 and (a.get("avg_hr") or 0) > 145))
        and 15 <= (a.get("duration_min") or 0) < 75
    )
    strides_ready = speed_sessions >= 2

    # 48-72h recovery gate: gym -> no hard run within 2 days
    days_since_gym = ds.get("full_body_gym", 999)

    # Long run progression: cap at 10% increase over last long run, max 11km for 10K
    # Long run cap: use week_plan progression target if available (#6)
    if week_plan and week_plan.get("long_run_target_km"):
        long_run_cap_km = week_plan["long_run_target_km"]
    else:
        last_long_km = max(
            (a.get("distance_km", 0) for a in recent if classify(a) == "long_run"),
            default=8.0
        )
        long_run_cap_km = min(round(last_long_km * 1.10, 1), 11.0)

    # Long run + intervals spacing: must be 3+ days apart
    days_since_intervals = min(
        ds.get("intervals_vo2", 999), 999
    )
    days_since_long = ds.get("long_run", 999)
    too_close_to_long   = days_since_long < 3       # long run within 3 days
    too_close_to_intervals = days_since_intervals < 3  # intervals within 3 days

    # Weekly zone distribution (Seiler 3-zone — see sync_session_data.py 2026-05-26).
    # LOW (easy aerobic, ≤149) is MAXIMIZED and never caps anything; only the true
    # MIDDLE (LT1-LT2 tempo, 149-164) is capped, and HIGH (>=165) is the interval target.
    MID_CAP_MIN  = 38     # weekly threshold budget — research 10-15% of ~5h ≈ 30-45min
                          # (allows one full threshold session ~30min, blocks a second)
    HIGH_CAP_MIN = 30     # weekly Z4-Z5 (VO2) ceiling. Research: 5-10% of ~5h ≈ 15-30
                          # min/wk. 30 lets one VO2 session through, blocks a 2nd.
    zones = _load_json("weekly_zones.json", {
        "mid_quota_used": 0, "high_min": 0,
        "mid_headroom_min": 999,
        "classification": "base"})

    mid_quota_full  = zones.get("mid_quota_used", 0) >= MID_CAP_MIN   # tempo budget spent
    high_quota_full = zones.get("high_min", 0) >= HIGH_CAP_MIN        # interval ceiling

    # CTL ramp rate: compute weekly CTL change from fitness trend
    fitness_trend = _load_json("fitness_freshness.json", {}).get("trend", [])
    # Anchor the lookback to the trend point CLOSEST to exactly 7 days ago, not
    # merely the first point >=6 days old (which drifts to 10-15d on a sparse
    # trend and distorts the weekly ramp this injury gate keys on).
    def _trend_day(t):
        try:
            return datetime.strptime(t["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            return None
    seven_ago = today - timedelta(days=7)
    _ramp_cands = [(abs((_trend_day(t) - seven_ago).days), t.get("ctl"))
                   for t in fitness_trend if _trend_day(t) is not None and t.get("ctl") is not None]
    ctl_7d_ago = min(_ramp_cands, key=lambda x: x[0])[1] if _ramp_cands else ctl
    ctl_ramp_rate = round(ctl - ctl_7d_ago, 1)   # points gained this week
    ramp_too_fast = ctl_ramp_rate > 5             # research: >3-5 = high risk for beginners

    # #4/#5: Deload enforcement from mesocycle
    if week_plan and week_plan.get("cycle_type") == "DELOAD":
        deload_mode = True  # Force deload even if TSB is fine

    # #4: Weekly periodization — check budget remaining
    budget_block = {}
    if week_plan and week_plan.get("budget"):
        budget = week_plan["budget"]
        # If the weekly quality quota is 0 (deload) or already done, block all hard runs
        if budget.get("quality", {}).get("remaining", 1) <= 0:
            budget_block["threshold"] = "weekly budget exhausted"
            budget_block["intervals_vo2"] = "weekly budget exhausted"
        if budget.get("long_run", {}).get("remaining", 1) <= 0:
            budget_block["long_run"] = "weekly budget exhausted"

    # #7: Return-from-break gate — block hard sessions until 2+ easy runs done
    # Connective tissue needs gradual reintroduction after 7+ days off
    # Find the most recent run date, then check if there's a 7+ day gap before it
    run_dates = sorted(
        [datetime.strptime(a["date"], "%Y-%m-%d").date()
         for a in recent if a.get("type") == "Run"],
        reverse=True
    )
    return_from_break = False
    if run_dates:
        # Check gap between today and most recent run
        days_since_last_run = (today - run_dates[0]).days
        if days_since_last_run >= 7:
            # Currently in a break — no runs for 7+ days
            return_from_break = True
        else:
            # Check for a recent break: 7+ day gap between consecutive runs
            for i in range(len(run_dates) - 1):
                gap = (run_dates[i] - run_dates[i + 1]).days
                if gap >= 7:
                    # Found a break. Count easy runs AFTER the break
                    break_ended = run_dates[i]
                    easy_after_break = sum(
                        1 for a in recent
                        if a.get("type") == "Run"
                        and (a.get("avg_hr") or 0) < 150
                        and datetime.strptime(a["date"], "%Y-%m-%d").date() >= break_ended
                    )
                    if easy_after_break < 2:
                        return_from_break = True
                    break
    else:
        return_from_break = True

    # Extract Oura signals for scoring
    avg_sleep = oura_data.get("sleep", {}).get("avg_duration", 7.5)
    rhr_trend = oura_data.get("resting_hr", {}).get("trend", "")
    # Oura sleep quality/score (0-100), computed upstream in readiness.json.
    # Used worse-of with duration so a fragmented-but-long night still downgrades.
    sleep_score = _nested(readiness_data, ["components", "sleep_quality"])

    # Volume urgency: behind on weekly km target by mid-week
    weekly_km_target_scoring = week_plan.get("target_km", 35) if week_plan else 35

    # ── Raw weekly-volume ramp guard (research lever #1) ──────────────────
    # week_plan target is spike-robust (median-phase), but still gate the
    # EFFECTIVE volume used for scoring against ACTUAL trailing km so a target
    # that outruns recent training history can't push volume-behind boosts into
    # an over-ramp. Caps weekly_km_target → tempers km_remaining/volume_behind.
    def _km_for_week(wk_mon):
        wk_end = wk_mon + timedelta(days=7)
        return sum(a.get("distance_km", 0) for a in recent
                   if a.get("type") == "Run"
                   and wk_mon <= datetime.strptime(a["date"], "%Y-%m-%d").date() < wk_end)
    prior_weeks_km = [_km_for_week(monday - timedelta(days=7 * n)) for n in range(1, 5)]
    _wks = [k for k in prior_weeks_km if k > 0]
    trailing_avg_km = round(sum(_wks) / len(_wks), 1) if _wks else 0
    volume_ramp_note = ""
    volume_ramp_hot = False
    # Only a target ABOVE the trailing average is an upward ramp worth gating;
    # a target at/below recent volume (e.g. crash-recovery week) is not a spike.
    if trailing_avg_km > 0 and weekly_km_target_scoring > trailing_avg_km:
        ramp_ratio = weekly_km_target_scoring / trailing_avg_km
        prev2 = sum(prior_weeks_km[1:3])
        two_wk_ratio = (weekly_km_target_scoring + prior_weeks_km[0]) / prev2 if prev2 else 0
        if ramp_ratio > VOL_RAMP_CAP:
            capped = round(trailing_avg_km * VOL_RAMP_CAP, 1)
            volume_ramp_hot = True
            volume_ramp_note = (
                f"volume-ramp guard: target {weekly_km_target_scoring}km is "
                f"+{round((ramp_ratio - 1) * 100)}% over trailing {trailing_avg_km}km avg "
                f"→ effective target capped to {capped}km (≤{round((VOL_RAMP_CAP - 1) * 100)}%/wk)")
            weekly_km_target_scoring = capped
        elif two_wk_ratio > VOL_RAMP_2WK_CAP:
            volume_ramp_hot = True
            volume_ramp_note = (
                f"volume-ramp guard: 2-week volume +{round((two_wk_ratio - 1) * 100)}% "
                f"→ hold volume steady (≤{round((VOL_RAMP_2WK_CAP - 1) * 100)}%/2wk)")

    # ── Niggle AMBER volume cut (research lever #4) ───────────────────────
    # Quality/long already blocked in apply_layered_decision; halve the volume
    # target so volume-behind boosts don't pile easy mileage onto a sore tissue.
    niggle_note = ""
    if injury == 1:
        weekly_km_target_scoring = round(weekly_km_target_scoring * 0.5, 1)
        niggle_note = ("niggle AMBER — volume cut ~50%, quality/long blocked; easy Z2 only "
                       "(skip strides if any pain), prehab the area (eccentric calf raises / "
                       "hip control), reassess in 3-5 days")
    elif injury >= 2:
        niggle_note = ("niggle RED — no running; cross-train pain-free (bike/elliptical), "
                       "rehab the area, return via easy Z2 once pain-free")

    volume_behind = (dow_int >= 2 and week_km < weekly_km_target_scoring * 0.4)  # Wed+ and <40% done
    weekly_km_target = weekly_km_target_scoring

    models = compute_decision_models(
        briefing_ctx, readiness, tsb, ctl, atl, acwr, hrv_status,
        avg_sleep, rhr_trend, injury, week_hard, week_gym_count,
        days_since_gym, zones, week_km, weekly_km_target, sleep_score=sleep_score,
        vo2max=vo2max, sleep_debt=sleep_debt, hrv_trend=hrv_trend
    )

    # Score every session
    scores, blocked = {}, {}
    for key, info in CATALOGUE.items():
        s, reason = score_session(
            key, info, ds[key], tsb, readiness, yest_type, week_hard,
            hrv_trend, deload_mode, strides_ready,
            days_since_gym, too_close_to_long, too_close_to_intervals,
            ramp_too_fast, mid_quota_full, high_quota_full,
            week_gym_count,
            hrv_status=hrv_status, dow=dow,
            avg_sleep=avg_sleep, rhr_trend=rhr_trend,
            muscle_emergency=muscle_emergency, volume_behind=volume_behind,
            sleep_score=sleep_score
        )
        # Apply return-from-break gate (#7)
        if return_from_break and info["type"] == "hard" and s >= 0:
            s = -1
            reason = "return from break — need 2+ easy runs before hard sessions (calf/tendon safety)"
        # Apply weekly budget blocks (#4)
        if key in budget_block and s >= 0:
            s = -1
            reason = budget_block[key]
        # Big-event recovery window (post-ultra recovery guard)
        if big_event.get("active") and s >= 0:
            if big_event["phase"] == "full_block":
                # Phase 1: only easy Z2 / gym / rest. Block all hard, long, strides.
                if info["type"] in ("hard", "moderate") or key == "long_run":
                    s = -1
                    reason = big_event["note"]
            elif info["type"] == "hard":
                # Phase 2: ALL hard runs stay blocked (threshold is a hard run too —
                # blocking, not discounting, so the weekly quality floor can't re-boost
                # it past the guard). long_run is allowed but discounted below.
                s = -1
                reason = big_event["note"]
        # Rolling 7-day hard-density cap (boundary-robust complement to week_hard).
        # No more than 2 hard runs in any rolling 7-day window — catches the spike
        # that the Mon-anchored weekly cap and the EWMA (ATL) both smooth over.
        if rolling_hard_7d >= 2 and info["type"] == "hard" and s >= 0:
            s = -1
            reason = f"{rolling_hard_7d} hard runs in the last 7 days (rolling cap 2) — density guard"
        # FAIL-SAFE: a missing/stale CORE recovery input (readiness / Oura / fitness)
        # means the safety gates are running blind — never prescribe hard/long on bad
        # data. Falls through to easy/rest, and the trust badge is flagged RED below.
        if data_issues and s >= 0 and (info["type"] == "hard" or key == "long_run"):
            s = -1
            reason = "core recovery data missing/stale — easy/rest only (fail-safe): " + "; ".join(data_issues)
        # Single-session distance-spike: 1-day hard/long hold after an acute overshoot.
        if (volume_spike.get("active") and volume_spike["days_since"] <= 1
                and s >= 0 and (info["type"] == "hard" or key == "long_run")):
            s = -1
            reason = volume_spike["note"]
        # Illness return-to-play ramp: first ~3 days after the sick flag clears,
        # block hard/long/strides. TSB recovered DURING the illness (ATL decayed),
        # so without this the engine waves through intervals the first day back —
        # exactly the post-viral hard-effort / myocarditis-risk window the code warns
        # about. Reuses the recovery scaffolding; easy Z2 + gym + rest stay available.
        if illness_ramp.get("active") and s >= 0 and (
            info["type"] in ("hard", "moderate") or key == "long_run"):
            s = -1
            reason = illness_ramp["note"]
        scores[key] = s
        if reason:
            blocked[key] = reason

    scores, blocked, score_adjustments = apply_layered_decision(
        scores, blocked, models, CATALOGUE, ds, yest_type,
        week_hard, week_gym_count, dow, injury=injury
    )

    # ── Muscle protection: gym overdue boost ──────────────────────────
    # Gym is non-negotiable for muscle preservation during a cut.
    # 7+ days without gym = emergency. Muscle_emergency (Renpho signal) = double down.
    # BUT: lifting on acute sleep debt or Orange readiness carries elevated injury risk
    # + blunted adaptation. The force-boosts (300/350/200) otherwise dwarf the recovery
    # cluster's rest/easy boosts (+40/+15), forcing a strength session on a day the
    # athlete should recover. One day's delay won't lose muscle — so on a clearly
    # under-recovered day, hold the force-boost and let rest/easy win (gym still scores
    # on its own merits; it just isn't floored to priority).
    gym_fatigue_suppress = (
        models["readiness"].get("sleep_debt")
        or models["readiness"]["color"] == "Orange")
    if "full_body_gym" in scores and scores["full_body_gym"] >= 0 and not gym_fatigue_suppress:
        if days_since_gym >= 7:
            scores["full_body_gym"] = max(scores["full_body_gym"], 300)
            if "full_body_gym" not in blocked:
                blocked["_gym_boost"] = f"gym overdue ({days_since_gym}d) — muscle protection priority"
        if muscle_emergency:
            scores["full_body_gym"] = max(scores["full_body_gym"], 350)
            if "full_body_gym" not in blocked:
                blocked["_muscle_alert"] = muscle_alert
    elif gym_fatigue_suppress and (days_since_gym >= 7 or muscle_emergency):
        score_adjustments.setdefault("full_body_gym", []).append(
            "gym priority-boost HELD — under-recovered (sleep debt / Orange); lift tomorrow")

    # ── Big-event recovery: steer the winner toward recovery in the window ──
    # Phase 1 boosts rest/easy so the day resolves to genuine recovery (gym stays
    # available for muscle protection via its own floors). Phase 2 keeps surviving
    # hard/long sessions on the board but discounts them so they don't out-compete
    # easy work while still rebuilding.
    if big_event.get("active"):
        if big_event["phase"] == "full_block":
            if scores.get("rest", -1) >= 0:
                scores["rest"] += 50
                score_adjustments.setdefault("rest", []).append(
                    "+50 post-big-event recovery window (Phase 1)")
            if scores.get("easy_z2", -1) >= 0:
                scores["easy_z2"] += 15
                score_adjustments.setdefault("easy_z2", []).append(
                    "+15 post-big-event: easy shake-out only")
        else:  # reintegration
            # hard runs are now hard-blocked in the scoring loop; only long_run
            # survives here and gets discounted so easy work wins on most days.
            if scores.get("long_run", -1) >= 0:
                scores["long_run"] = round(scores["long_run"] * 0.6, 1)
                score_adjustments.setdefault("long_run", []).append(
                    "-40% post-big-event reintegration (rebuild gradually)")
            if scores.get("easy_z2", -1) >= 0:
                scores["easy_z2"] += 10
                score_adjustments.setdefault("easy_z2", []).append(
                    "+10 post-big-event: favor easy aerobic")

    # Illness return-to-play ramp: bias toward easy/rest on the days back.
    if illness_ramp.get("active"):
        if scores.get("rest", -1) >= 0:
            scores["rest"] += 40
            score_adjustments.setdefault("rest", []).append(
                "+40 illness return-to-play ramp")
        if scores.get("easy_z2", -1) >= 0:
            scores["easy_z2"] += 15
            score_adjustments.setdefault("easy_z2", []).append(
                "+15 illness return-to-play: easy only")

    # Set recent activities for gym A/B derivation (no side effects)
    global _recent_activities
    _recent_activities = recent

    p = adjusted_paces(paces_raw, readiness, tsb)

    # Volume urgency: if behind on km target, boost easy run scores
    km_remaining = weekly_km_target - week_km
    if km_remaining > 0 and week_km < weekly_km_target * 0.5:
        # Behind on volume — boost easy/long runs
        for k in ("easy_z2", "long_run"):
            if k in scores and scores[k] >= 0:
                scores[k] = scores[k]  # placeholder, actual boost applied below
    pace_note = (f"Paces slowed {round((1-p['mult'])*100)}% (fatigue)"
                 if p["mult"] < 1 else "Normal paces")

    # ── Muscle maintenance floor: 2 gym/week is non-negotiable ──────
    # If it's late in the week and gym isn't done, boost gym scores aggressively
    if week_plan:
        budget = week_plan.get("budget", {})
        days_left_wp = week_plan.get("days_left", 7)
        # Full body gym: 2x/week non-negotiable (muscle maintenance)
        gym_sessions_remaining = budget.get("full_body_gym", {}).get("remaining", 0)

        # If gym is at risk of being missed (remaining gym >= days left), boost it —
        # unless under-recovered (sleep debt / Orange), where recovery wins over the
        # weekly floor (same rationale as the muscle-protection boost above).
        if (gym_sessions_remaining > 0 and days_left_wp <= gym_sessions_remaining + 1
                and not gym_fatigue_suppress):
            if "full_body_gym" in scores and scores["full_body_gym"] >= 0:
                scores["full_body_gym"] = max(scores["full_body_gym"], 200)

            # Re-pick winner after boost
            if "full_body_gym" in score_adjustments:
                score_adjustments["full_body_gym"].append("+weekly strength floor at risk")
            else:
                score_adjustments["full_body_gym"] = ["+weekly strength floor at risk"]

    # ── Weekly quota floors: front-load any behind required session ──────
    # Escalates by deadline (slack = days_left - remaining). Each boost raises a
    # POSITIVE score only, so readiness/TSB/HRV gates (score < 0) always win — a
    # behind hard session can never be forced; it is reported at_risk/missed instead.
    # (Gym floor is handled separately above for muscle-protection priority.)
    floor_status = {}
    if week_plan:
        _fb = week_plan.get("budget", {})
        days_left_wp = week_plan.get("days_left", 7 - dow_int)
        # keys are tried in PRIORITY order — the first placeable one gets the boost.
        # Quality slot prefers VO2/economy intervals; threshold is the fallback only
        # when intervals are gate-blocked (readiness<75 / not strides_ready). A fallback
        # threshold run feeds strides_ready, which unlocks intervals next time.
        for cat, keys, done_live in (
            ("quality", ("intervals_vo2", "threshold"), week_threshold_done),
            ("long_run", ("long_run",), week_long_done),
        ):
            target = _fb.get(cat, {}).get("target", 0)
            remaining = max(0, target - done_live)
            if target <= 0:
                floor_status[cat] = "off"
                continue
            if remaining <= 0:
                floor_status[cat] = "done"
                continue
            slack = days_left_wp - remaining
            if slack >= 2:
                # Plenty of slack remains: report the quota as on track, but
                # do not override the weekly rotation or recovery/load logic.
                floor_status[cat] = "on_track"
                continue
            elif slack == 1:
                floor_val, tag, st = 220, "+weekly floor at risk", "behind"
            else:
                floor_val, tag, st = 320, "+weekly floor MUST DO today", "behind"
            placeable = False
            for k in keys:
                if k in scores and scores[k] >= 0:
                    scores[k] = max(scores[k], floor_val)
                    score_adjustments.setdefault(k, []).append(tag)
                    placeable = True
                    break   # boost only the top-priority placeable option
            # Behind, can't be safely placed today, and not enough days remain → at risk
            if not placeable and remaining >= days_left_wp:
                st = "at_risk"
            floor_status[cat] = st

        # Recovery-day floor: guarantee at least one genuine easy/rest day per week.
        # Driven by the budgeted rest target but never below 1 (easy days count as
        # recovery). Only nudges when actually under-rested (post-hard or fatigued).
        rec_target = max(1, _fb.get("rest", {}).get("target", 0) or 1)
        if week_recovery_days < rec_target and (
            yest_type in ("hard", "long_run") or tsb <= TSB_MODERATE
        ):
            for k in ("easy_z2", "rest"):
                if k in scores and scores[k] >= 0:
                    scores[k] = max(scores[k], 150)
                    score_adjustments.setdefault(k, []).append("+recovery floor (under-rested)")

    # ── Under-training / detraining-drift guard (added 2026-06-19) ──────────
    # The selector's stated objective is to MAXIMISE CTL while holding TSB in the
    # productive −5..−20 band. It had strong OVER-training defenses but no symmetric
    # guard against the opposite failure: parked too fresh while fitness bleeds.
    # REAL FAILURE (2026-06-19): a 7-day no-run break (Jun 9-15) pushed TSB to +27.6
    # and dropped CTL 77.5→63.1 (−14 in 12d, −19%); the engine kept force-boosting
    # strides (388) and easy work, steering toward MORE freshness exactly when
    # re-loading was the goal. TSB/ATL/ACWR can't catch this — they read "Fresh/green"
    # while fitness is actively bleeding. Fires ONLY when genuinely too-fresh AND
    # losing fitness AND recovered AND no competing recovery mandate (big-event /
    # illness / deload / niggle own those days). It only ever RAISES positive scores on
    # load-bearing sessions and damps the easy/strides over-boost — it can NEVER
    # resurrect a hard-blocked (−1) session, so Session Lock and every safety gate still
    # win. The priority walk stops at the first UNLOCKED load session, so a gate-locked
    # flagship correctly defers to its fallback (intervals locked → threshold, which
    # also re-earns strides_ready to unlock intervals next time). Floor (240) sits BELOW
    # the gym muscle-protection floors (200/300/350) so the non-negotiable strength
    # floor still wins when a lift is genuinely owed.
    undertrain_drift = (
        tsb > 10
        and ctl_ramp_rate < -2
        and models["readiness"]["color"] in ("Green", "Yellow")
        and not big_event.get("active")
        and not illness_ramp.get("active")
        and not deload_mode
        and injury == 0
    )
    undertrain_note = ""
    if undertrain_drift:
        for k in ("strides", "easy_z2"):
            if scores.get(k, -1) > 0:
                scores[k] = round(scores[k] * 0.5, 1)
                score_adjustments.setdefault(k, []).append(
                    "−50% under-training drift (too fresh + CTL falling — re-load, don't coast)")
        for k in ("intervals_vo2", "threshold", "long_run"):
            if scores.get(k, -1) >= 0:
                scores[k] = max(scores[k], 240)
                score_adjustments.setdefault(k, []).append(
                    f"+load floor: TSB {round(tsb,1)} too fresh (target −5..−20), "
                    f"CTL {ctl_ramp_rate}/wk falling — re-load to resume CTL growth")
                undertrain_note = (
                    f"under-training drift: TSB {round(tsb,1)} above productive band + "
                    f"CTL falling {ctl_ramp_rate}/wk → prioritised {k} to re-load")
                break
        if not undertrain_note:
            undertrain_note = (
                f"under-training drift: TSB {round(tsb,1)} + CTL {ctl_ramp_rate}/wk falling, "
                f"but all load sessions gated today — a real easy run still beats coasting")

    valid = [(k, v) for k, v in scores.items() if v >= 0]
    winner = max(valid, key=lambda x: x[1])[0] if valid else "rest"
    if manual_illness:
        winner = "rest"   # fever/sick override — full rest, highest priority
    label, sess_type, total_time, steps = build_session(
        winner,
        p,
        readiness,
        tsb,
        long_run_cap_km=long_run_cap_km,
        decoupling=models["performance"].get("latest_decoupling"),
        deload=deload_mode,
    )
    final_decision = build_final_decision(
        winner, scores, blocked, models, score_adjustments,
        readiness, tsb, ctl, week_km, runs_done, week_gym_count,
        muscle_alert, days_since_gym
    )
    reason = final_decision["reason"]

    # Lead the reasoning with the recovery-window / illness-ramp note so it can't be missed.
    if not manual_illness:
        for _g in (big_event, illness_ramp, volume_spike):
            if _g.get("active"):
                final_decision["reason"] = f"⚠ {_g['note']} || " + final_decision["reason"]
                final_decision["why_won"] = [f"⚠ {_g['note']}"] + final_decision["why_won"]
        if undertrain_drift and undertrain_note:
            final_decision["reason"] = f"⚠ {undertrain_note} || " + final_decision["reason"]
            final_decision["why_won"] = [f"⚠ {undertrain_note}"] + final_decision["why_won"]
        reason = final_decision["reason"]

    if manual_illness:
        # Replace cut/LEA/load reasoning (all confounded by illness) with a clear,
        # correct illness message. The break gate handles easing back after clear.
        label = "REST: Illness — recover (sick flag active)"
        final_decision["confidence"] = 99
        final_decision["why_won"] = [
            f"ILLNESS flagged via illness.json"
            f"{f' (auto-clears {illness_expires})' if illness_expires else ''} — full REST "
            "until afebrile ≥24h and symptoms clearing (exercising with a fever risks myocarditis)",
            "Eat to MAINTENANCE, not a deficit, while ill — protein + fluids aid recovery",
            "Return-to-play: easy/short Z2 only for the first 2-3 sessions once cleared",
            "HRV / readiness / RHR / ACWR are confounded by illness right now — don't let them drive the call",
        ]
        final_decision["nutrition_flags"] = [
            "ILLNESS: calories to maintenance — do NOT run a deficit while sick",
            "protein 1.6-1.8 g/kg + aggressive hydration",
        ]
        reason = "Illness override active (illness.json) — full rest + return-to-play; cut/LEA signals suppressed as confounded."

    # ── Decision log + adherence (recommended vs actual) ───────────────
    # Banks today's recommendation and scores how well past recommendations
    # matched what the athlete actually did. Persistent mismatch = rotation/gates may
    # be mis-tuned; also the accumulating record that future calibration needs.
    last_analysis = _load_json("last_analysis.json", {})
    decision_log = log_decision(
        today, winner, label, sess_type, final_decision["confidence"],
        {"tsb": round(tsb, 1), "ctl": round(ctl, 1), "atl": round(atl, 1),
         "acwr": round(acwr, 2) if acwr else None, "readiness": readiness,
         "dow": dow},
    )
    adherence = compute_adherence(decision_log, recent, today, last_analysis)

    # ── Secondary session recommendation ──────────────────────────────
    # Check if a second session should be added to hit volume/aerobic targets
    secondary = None
    secondary_reason = None

    # Fastest-sustainable guard: never suggest extra volume (double day / extend)
    # when under-recovered — quality + recovery beat junk miles. Volume chasing only
    # when genuinely fresh.
    allow_extra_volume = (
        not sleep_debt[0]
        and models["load"]["status"] not in ("Overreached", "Caution")
        and models["readiness"]["color"] in ("Green", "Yellow")
    )

    if week_plan and allow_extra_volume:
        aerobic_done = week_plan.get("aerobic_min_done", 0)
        aerobic_remaining = max(0, volume_floor - aerobic_done)
        km_done = week_plan.get("actual_km", 0)
        km_target = week_plan.get("target_km", 0)
        days_left_wp = week_plan.get("days_left", 7)
        km_per_day_needed = (km_target - km_done) / max(1, days_left_wp) if km_target > km_done else 0

        # Rule 1: If primary is gym, recommend an easy run too (double day)
        if winner == "full_body_gym" and readiness >= 65:
            if aerobic_remaining > 60:
                secondary = "easy_z2"
                secondary_reason = (
                    f"Add 30-40 min easy Z2 run (PM). "
                    f"Only {aerobic_done} of {volume_floor} aerobic min done. "
                    f"{'VAT ' + str(vat) + ' needs extra volume. ' if body_comp_note else ''}"
                    f"Target {volume_floor}+ min/week."
                )
            elif km_per_day_needed > 8:
                secondary = "easy_z2"
                secondary_reason = (
                    f"Add 30-40 min easy Z2 run (PM). "
                    f"{round(km_target - km_done, 1)}km left in {days_left_wp} days — need to build volume."
                )

        # Rule 2: If primary is easy run but it's short, suggest extending or adding PM run
        elif winner == "easy_z2" and km_per_day_needed > 6:
            secondary = "extend"
            secondary_reason = (
                f"Extend to 50-60 min if feeling good. "
                f"{round(km_target - km_done, 1)}km left in {days_left_wp} days."
            )

        # Rule 3: If primary is a short run and volume is way behind, suggest double
        elif winner in ("easy_z2", "strides") and readiness >= 70:
            if aerobic_remaining > 90 and km_per_day_needed > 8:
                secondary = "easy_z2"
                secondary_reason = (
                    f"Add a second easy Z2 run later (PM). "
                    f"{round(km_target - km_done, 1)}km left in {days_left_wp} days, "
                    f"only {aerobic_done} of {volume_floor} aerobic min. "
                    f"Volume drives Z2 pace improvement."
                )

        # Rule 4: If rest day but way behind on aerobic minutes
        elif winner == "rest" and aerobic_remaining > 90 and readiness >= 70:
            secondary = "easy_z2"
            secondary_reason = (
                f"Consider a light 20-30 min Z2 run instead of full rest. "
                f"Only {aerobic_done} of {volume_floor} aerobic min — "
                f"VAT mobilization needs volume."
            )

    # Build secondary session details if recommended
    secondary_session = None
    if secondary == "easy_z2":
        # Same-day easy runs must agree: derive the HR target from karvonen_z2_target(tsb)
        # exactly like the primary easy/long builders, instead of a hardcoded band.
        _sec_t, _sec_ceil, _sec_lbl = karvonen_z2_target(tsb)
        secondary_session = {
            "label": "ADDITIONAL: Easy Z2 Run",
            "type": "easy",
            "total_time": "30-40 min",
            "when": "PM (separate from primary by 4+ hours)",
            "pace": p.get("z2", "7:44") + "/km",
            "hr_target": f"{_sec_lbl} · ceiling {_sec_ceil} bpm",
            "reason": secondary_reason,
        }
    elif secondary == "extend":
        secondary_session = {
            "label": "EXTEND primary run",
            "type": "extend",
            "reason": secondary_reason,
        }

    # ── Weekly scoreboard (Today page render + Friday nudge source) ──────
    _sb_budget = (week_plan or {}).get("budget", {})
    _sb_days_left = (week_plan or {}).get("days_left", 7 - dow_int)

    def _sb_status(target, done):
        if target <= 0:
            return "off"
        if done >= target:
            return "done"
        rem = target - done
        if rem > _sb_days_left:
            return "at_risk"
        slack = _sb_days_left - rem
        return "on_track" if slack >= 2 else "behind"

    sb_items = []
    for cat, done_live in (
        ("quality", week_threshold_done),
        ("long_run", week_long_done),
        ("full_body_gym", week_gym_count),
    ):
        tgt = _sb_budget.get(cat, {}).get("target", 0)
        # Prefer the deadline-aware floor verdict (accounts for today's gate-ability);
        # fall back to a pure count-vs-target status (e.g. gym, which floors elsewhere).
        st = floor_status.get(cat)
        if st in (None, "off"):
            st = _sb_status(tgt, done_live)
        sb_items.append({
            "category": cat,
            "target": tgt,
            "done": done_live,
            "remaining": max(0, tgt - done_live),
            "status": st,
        })
    _sb_rec_target = max(1, _sb_budget.get("rest", {}).get("target", 0) or 1)
    weekly_scoreboard = {
        "days_left": _sb_days_left,
        "recovery_days": {"done": week_recovery_days, "target": _sb_rec_target},
        "items": sb_items,
        "summary": _scoreboard_summary(sb_items, week_recovery_days, _sb_rec_target),
    }
    selector_health = {
        "calibration_ready": bool(calibration_pace_source.get("applied")),
        "calibration_blocker": calibration_pace_source.get("reason"),
        "promotion_missing": calibration_pace_source.get("promotion_missing", []),
        "submax_hr_pace": (calibration or {}).get("submax_hr_pace"),
        "adherence_14d": adherence.get("last_14d", {}).get("rate"),
        "adherence_sample_low": (adherence.get("last_14d", {}).get("n") or 0) < 7,
        "blocked_count": len([k for k in blocked if k in CATALOGUE]),
        "hard_blocked_count": len([
            k for k in blocked
            if k in CATALOGUE and CATALOGUE[k]["type"] == "hard"
        ]),
        "load_status": models["load"]["status"],
        "readiness_color": models["readiness"]["color"],
    }

    output = {
        "generated": today.isoformat(),
        "data_issues": data_issues,   # non-empty → trust badge goes RED; hard/long were blocked
        "decision": f"SMART: {winner} (score {scores[winner]})",
        "recommendation": {
            "workout": winner,
            "label": label,
            "confidence": final_decision["confidence"],
            "score_gap_to_next": final_decision["score_gap_to_next"],
            "runner_up": final_decision["runner_up"],
            "acceptable_alternatives": final_decision["acceptable_alternatives"],
            "top3": final_decision["top3"],
            "why_won": final_decision["why_won"],
            "blocked_hard": final_decision["blocked_hard"],
            "nutrition_flags": final_decision["nutrition_flags"],
        },
        "reason": reason,
        "weekly_scoreboard": weekly_scoreboard,
        "adherence": adherence,
        "selector_health": selector_health,
        "scores": {k: v for k, v in sorted(scores.items(), key=lambda x: -x[1])},
        "blocked": blocked,
        "adjustments": {
            "pace_multiplier": p["mult"],
            "pace_note": pace_note,
            "pace_source": calibration_pace_source,
            "duration_factor": 1.0,
            "duration_note": "Duration per session spec",
            "missing_sessions": [k for k in ("intervals_vo2","threshold","long_run")
                                  if ds.get(k,999) > CATALOGUE[k]["ideal_freq"] * 1.5],
            "score_adjustments": score_adjustments,
        },
        "models": models,
        "session": {
            "label": label,
            "type": sess_type,
            "total_time": total_time,
            "steps": steps,
        },
        "context": {
            "readiness": readiness,
            "readiness_adjusted": readiness,
            "tsb": round(tsb, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "acwr": round(acwr, 2) if acwr else None,
            "week_km": round(week_km, 1),
            "week_target": weekly_km_target,
            "runs_done": runs_done,
            "yesterday_load": yest_load,
            "yesterday_km": round(yest_run_km, 1),
            "days_left": 7 - dow_int,
            "threshold_done": any(classify(a)=="threshold" for a in week_acts),
            "long_run_done": any(classify(a)=="long_run" for a in week_acts),
            "gym_done": week_gym_count,
            "injury_flag": injury,
            "niggle_note": niggle_note,
            "volume_ramp_note": volume_ramp_note,
            "volume_ramp_hot": volume_ramp_hot,
            "trailing_avg_km": trailing_avg_km,
            "days_since": ds,
            "deload_mode": deload_mode,
            "strides_ready": strides_ready,
            "hrv_trend": hrv_trend,
            "hrv_status": hrv_status,
            "ctl_ramp_rate": ctl_ramp_rate,
            "ramp_too_fast": ramp_too_fast,
            "undertrain_drift": {"active": undertrain_drift, "note": undertrain_note,
                                 "tsb": round(tsb, 1), "ctl_ramp_rate": ctl_ramp_rate},
            "long_run_cap_km": long_run_cap_km,
            "days_since_gym": days_since_gym,
            "week_gym_count": week_gym_count,
            "rolling_hard_7d": rolling_hard_7d,
            "zones": {
                "low_pct": zones.get("low_pct",0),
                "mid_pct": zones.get("mid_pct",0),
                "high_pct": zones.get("high_pct",0),
                "classification": zones.get("classification","base"),
                "mid_quota_used": zones.get("mid_quota_used",0),
                "high_min": zones.get("high_min",0),
                "mid_headroom_min": zones.get("mid_headroom_min",999),
            },
            "recovery_alerts": recovery_signals.get("alerts", []) if isinstance(recovery_signals, dict) else [],
            "recovery_window": big_event,
            "illness_ramp": illness_ramp,
            "volume_spike": volume_spike,
            "heat": heat_summary,
        },
        "secondary_session": secondary_session,
        "calibration": calibration,
        "oura_trends": {
            "rhr_trend": oura_data.get("resting_hr",{}).get("trend",""),
            "hrv_trend": oura_data.get("hrv",{}).get("trend",""),
            "avg_sleep": oura_data.get("sleep",{}).get("avg_duration", 7),
            "avg_bedtime": oura_data.get("sleep",{}).get("avg_bedtime",""),
            "bedtime_consistency": oura_data.get("sleep",{}).get("bedtime_consistency",""),
            "alerts": oura_data.get("alerts",[]),
            "recommendations": oura_data.get("recommendations",[]),
        },
        "body_comp": {
            "weight_7d": body_comp.get("weight_7d_avg"),
            "body_fat_7d": bf,
            "visceral_fat_7d": vat,
            "muscle_mass": muscle,
            "muscle_baseline": MUSCLE_BASELINE,
            "muscle_alert": muscle_alert or None,
            "volume_floor": volume_floor,
            "volume_floor_note": body_comp_note or "standard 150 min/week",
        },
    }

    out_path = DATA_DIR / "dynamic_session.json"

    def _archive_session(session):
        # Archive a dated copy so downstream tools (post-run-analysis, calibrate_next_session)
        # can grade a run against the prescription FROM ITS OWN DAY. dynamic_session.json is
        # overwritten on every run; without this archive a run analyzed the next day was
        # compared to the wrong day's plan (2026-06-23 bug: yesterday's Z2-only run graded vs
        # today's Z2+Strides).
        # WRITE-ONCE-PER-DAY: only archive the FIRST generation of a day
        # (the authoritative 08:45 prescription). Later same-day re-runs — on-demand tests, or
        # the selector correctly switching to recovery AFTER a completed hard session — must
        # NOT clobber it, else the nightly calibrator reads the wrong prescribed pace.
        try:
            hist_dir = DATA_DIR / "session-history"
            hist_dir.mkdir(exist_ok=True)
            gen_day = session.get("generated") or today.isoformat()
            dated = hist_dir / f"{gen_day}.json"
            if not dated.exists():
                atomic_write_text(dated, dump_json(session))
            for old in sorted(hist_dir.glob("*.json"))[:-120]:  # keep ~120 days
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # Protect manual overrides: if existing file has manual_override=True and is
    # from today, do NOT clobber the user's choice. Test runs or out-of-band
    # re-triggers must not silently erase pinned sessions.
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if (existing.get("manual_override") is True
                    and existing.get("generated") == today.isoformat()):
                print(
                    f"⏭  manual_override active for {today.isoformat()} "
                    f"(session: {existing.get('session', {}).get('label', '?')}). "
                    f"Skipping write. Delete the override field or the file "
                    f"to let the selector re-choose.",
                    file=sys.stderr,
                )
                # Still write the auto-computed output to a draft file so the
                # reasoning is available for inspection without overwriting the pin.
                draft_path = DATA_DIR / "dynamic_session_draft.json"
                atomic_write_text(draft_path, dump_json(output))
                # Archive the PINNED session (what's actually prescribed today) so a
                # next-day run analysis grades against it, not "prescription unavailable".
                _archive_session(existing)
                return
        except (json.JSONDecodeError, OSError):
            pass  # corrupt existing file — safe to overwrite

    atomic_write_text(out_path, dump_json(output))
    _archive_session(output)
    return output


if __name__ == "__main__":
    result = main()
    if result is not None:
        print(dump_json(result), end="")
