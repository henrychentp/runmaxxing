"""Aerobic trend helper used by the selector tests.

The live Critical Speed / treadmill-anchor pipeline is not in this public
snapshot. This module keeps the HR-normalized easy-run trend function.
"""


def pace(sec_per_km):
    m, s = int(sec_per_km // 60), int(round(sec_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def submax_hr_pace_model(pool):
    """Estimate pace at a fixed 142 bpm from steady easy runs."""
    samples = []
    for row in pool:
        avg_hr = row.get("avg_hr")
        pace_sec = row.get("avg_pace_sec_per_km")
        dur = row.get("duration_min") or 0
        if not avg_hr or not pace_sec:
            continue
        if not (30 <= dur <= 100 and 128 <= avg_hr <= 152):
            continue
        pace_at_142 = pace_sec * (avg_hr / 142.0)
        samples.append({
            "date": row.get("date"),
            "surface": "treadmill" if row.get("treadmill") else "outdoor",
            "avg_hr": round(avg_hr, 1),
            "pace_sec_per_km": round(pace_sec, 1),
            "pace_at_142_sec": round(pace_at_142, 1),
        })
    samples = [s for s in samples if s.get("date")]
    samples.sort(key=lambda s: s["date"])
    if len(samples) < 4:
        return {
            "status": "insufficient",
            "n_samples": len(samples),
            "note": "Need at least 4 steady easy runs with HR and pace.",
        }

    recent = samples[-4:]
    prior = samples[-8:-4]
    recent_avg = sum(s["pace_at_142_sec"] for s in recent) / len(recent)
    recent_mix = {
        "outdoor": sum(1 for s in recent if s["surface"] == "outdoor"),
        "treadmill": sum(1 for s in recent if s["surface"] == "treadmill"),
    }
    out = {
        "status": "baseline" if not prior else "trend",
        "n_samples": len(samples),
        "recent_pace_at_142": pace(recent_avg),
        "recent_pace_at_142_sec": round(recent_avg, 1),
        "surface_mix_recent": recent_mix,
        "note": "HR-normalized easy-run trend; used as a cross-check, not hard-session prescription.",
    }
    if prior:
        prior_avg = sum(s["pace_at_142_sec"] for s in prior) / len(prior)
        delta = prior_avg - recent_avg
        prior_mix = {
            "outdoor": sum(1 for s in prior if s["surface"] == "outdoor"),
            "treadmill": sum(1 for s in prior if s["surface"] == "treadmill"),
        }
        caveats = []
        if recent_mix["outdoor"] == 0:
            caveats.append("recent window is treadmill-only")
        if prior_mix["outdoor"] != recent_mix["outdoor"]:
            caveats.append("surface mix changed between prior and recent windows")
        out.update({
            "prior_pace_at_142": pace(prior_avg),
            "surface_mix_prior": prior_mix,
            "delta_sec_per_km": round(delta, 1),
            "trend": "improving" if delta >= 5 else ("regressing" if delta <= -5 else "stable"),
            "confidence": "low" if caveats else "moderate",
            "caveats": caveats,
        })
    return out
