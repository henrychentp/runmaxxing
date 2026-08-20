import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_selector as selector  # noqa: E402
import calibrate  # noqa: E402


def test_calibrated_paces_wait_for_promotion_gate():
    paces = {"source": "manual", "threshold_speed": 3.0}
    cal = {
        "confidence": "low",
        "n_outdoor_contributing": 5,
        "promotion_gate": {"missing": ["outdoor_contributors_ok"]},
        "bands_provisional": {"threshold": "5:30"},
    }

    merged, meta = selector.apply_calibrated_paces(paces, cal)

    assert merged["threshold_speed"] == 3.0
    assert meta["applied"] is False
    assert meta["promotion_missing"] == ["outdoor_contributors_ok"]


def test_calibrated_paces_shrink_large_speed_jump():
    paces = {
        "source": "manual",
        "tenk_pace_speed": 3.0,
        "threshold_speed": 3.0,
        "interval_speed": 3.0,
    }
    cal = {
        "confidence": "moderate",
        "n_outdoor_contributing": 6,
        "promotion_gate": {"missing": []},
        "bands_provisional": {
            "tenk": "4:00",
            "threshold": "4:00",
            "interval": "4:00",
        },
    }

    merged, meta = selector.apply_calibrated_paces(paces, cal)

    assert meta["applied"] is True
    assert merged["threshold_speed"] == 3.09
    assert meta["capped_updates"]["threshold_speed"] == "capped to +/-3% from prior pace"


def test_long_run_target_caps_on_high_decoupling():
    target, cap, durability, stop_rule = selector._long_run_target(15, 8.5)

    assert target == 15
    assert cap == 12.8
    assert durability == "capped"
    assert "decoupling >7%" in stop_rule


def test_final_decision_surfaces_close_alternative():
    scores = {"easy_z2": 100, "long_run": 82, "rest": 0}
    models = {
        "readiness": {"color": "Green", "score": 80},
        "load": {"status": "Productive", "reasons": ["TSB -8"]},
        "body_comp": {"status": "OnTarget", "reasons": ["OK"]},
        "performance": {"status": "Stable", "reasons": ["OK"]},
    }

    decision = selector.build_final_decision(
        "easy_z2",
        scores,
        {},
        models,
        {},
        readiness_raw=82,
        tsb=-8,
        ctl=70,
        week_km=20,
        runs_done=3,
        week_gym_count=1,
        muscle_alert=None,
        days_since_gym=2,
    )

    assert decision["acceptable_alternatives"] == [{"key": "long_run", "score": 82}]


def test_submax_hr_pace_model_tracks_easy_run_trend():
    pool = []
    for i, pace_sec in enumerate([510, 505, 500, 495, 480, 475, 470, 465], start=1):
        pool.append({
            "date": f"2026-05-{i:02d}",
            "treadmill": False,
            "duration_min": 60,
            "avg_hr": 142,
            "avg_pace_sec_per_km": pace_sec,
        })

    model = calibrate.submax_hr_pace_model(pool)

    assert model["status"] == "trend"
    assert model["trend"] == "improving"
    assert model["delta_sec_per_km"] == 30.0
    assert model["confidence"] == "moderate"
    assert model["caveats"] == []


# ── days_since_each: latest-occurrence + unsorted/missing-date robustness ──
def test_days_since_each_uses_latest_occurrence_when_input_unsorted():
    today = date(2026, 6, 13)
    # Same easy run on two days, listed OLDEST-first. The old logic kept the
    # first item in iteration order (06-09 -> 4 days); correct is latest (06-12 -> 1).
    acts = [
        {"type": "Run", "date": "2026-06-09", "duration_min": 45, "avg_hr": 140},
        {"type": "Run", "date": "2026-06-12", "duration_min": 45, "avg_hr": 140},
    ]
    key = selector.classify(acts[0])
    out = selector.days_since_each(acts, today)
    assert out[key] == 1


def test_days_since_each_skips_items_without_date():
    today = date(2026, 6, 13)
    acts = [
        {"type": "Run", "duration_min": 45, "avg_hr": 140},          # no date
        {"type": "Run", "date": "2026-06-11", "duration_min": 45, "avg_hr": 140},
    ]
    key = selector.classify(acts[1])
    out = selector.days_since_each(acts, today)
    assert out[key] == 2


# ── compute_hrv_status: bands + robustness to malformed entries ──
def _hrv(values):
    return {"hrv": {"daily": [{"hrv": v} for v in values]}}


def test_hrv_status_insufficient_data_under_seven_samples():
    out = selector.compute_hrv_status(_hrv([60, 61, 62, 63, 64, 65]))
    assert out["status"] == "insufficient_data"


def test_hrv_status_suppressed_when_rolling_below_band():
    # 23 stable days at ~65 establish the baseline; last 7 crash to ~45.
    vals = [65, 64, 66, 65, 64, 66, 65, 64, 66, 65, 64, 66, 65, 64, 66, 65]
    vals += [45, 44, 46, 45, 44, 46, 45]
    out = selector.compute_hrv_status(_hrv(vals))
    assert out["status"] == "suppressed"


def test_hrv_status_normal_when_stable():
    out = selector.compute_hrv_status(_hrv([65, 64, 66, 65, 64, 66, 65, 64, 66, 65]))
    assert out["status"] == "normal"


def test_hrv_status_ignores_non_dict_and_zero_entries():
    daily = [{"hrv": 65}, 999, None, {"hrv": 0}, {"hrv": 64}, {"hrv": 66},
             {"hrv": 65}, {"hrv": 64}, {"hrv": 66}, {"hrv": 65}]
    out = selector.compute_hrv_status({"hrv": {"daily": daily}})
    # 7 valid numeric readings remain -> not insufficient, no crash on 999/None/0
    assert out["status"] in ("normal", "suppressed", "elevated")


def test_hrv_baseline_is_lagged_and_detects_sustained_suppression():
    # 20 stable days then 7 suppressed. A contaminated baseline would fold the
    # dip into its own mean; the lagged baseline keeps the reference at ~65.
    out = selector.compute_hrv_status(_hrv([65] * 20 + [45] * 7))
    assert out["baseline_lagged"] is True
    assert out["status"] == "suppressed"


def test_hrv_baseline_provisional_below_two_windows():
    out = selector.compute_hrv_status(_hrv([65, 64, 66, 65, 64, 66, 65, 64, 66, 65]))
    assert out["baseline_lagged"] is False


# ── LEA / under-fuelling cluster (2-of-3 + cut-sensitive) ──
def _models(ctx=None, **kw):
    base = dict(
        readiness=80, tsb=0, ctl=70, atl=50, acwr=1.0,
        hrv_status={"status": "normal"}, avg_sleep=8.0, rhr_trend="STABLE",
        injury=0, week_hard=0, week_gym_count=2, days_since_gym=1,
        zones={}, week_km=20, weekly_km_target=40,
    )
    base.update(kw)
    return selector.compute_decision_models(ctx if ctx is not None else {}, **base)


def test_lea_fires_on_two_of_three_signals():
    # readiness drops to Red via RHR penalty (50-7=43) + RHR rising = 2 signals.
    m = _models(readiness=50, rhr_trend="RISING")
    assert m["body_comp"]["status"] == "LEA_Risk"


def test_lea_single_signal_no_cut_does_not_fire():
    # HRV suppressed only (readiness stays Yellow), no active cut -> no LEA.
    m = _models(readiness=80, hrv_status={"status": "suppressed"})
    assert m["body_comp"]["status"] != "LEA_Risk"


def test_lea_cut_sensitive_single_hrv_signal_fires():
    ctx = {"body_comp_goal": {"cutting_safety": {"status": "OK"}}}
    m = _models(ctx=ctx, readiness=80, hrv_status={"status": "suppressed"})
    assert m["body_comp"]["status"] == "LEA_Risk"


def test_lea_cold_start_fallback_during_cut():
    # <14 HRV readings -> status insufficient_data, but Oura trend FALLING during
    # a cut must still trip the gate (no silent pass).
    ctx = {"body_comp_goal": {"cutting_safety": {"status": "OK"}}}
    m = _models(ctx=ctx, readiness=80,
                hrv_status={"status": "insufficient_data"}, hrv_trend="FALLING")
    assert m["body_comp"]["status"] == "LEA_Risk"


# ── threshold demoted to fallback (training-rules L8) ──
def test_threshold_is_fallback_not_coflagship():
    cat = selector.CATALOGUE
    assert cat["intervals_vo2"]["base_score"] > cat["threshold"]["base_score"]
    assert cat["long_run"]["base_score"] > cat["threshold"]["base_score"]
    assert cat["threshold"]["base_score"] > cat["easy_z2"]["base_score"]
    assert "threshold" not in selector.ROTATION["Friday"]


# ── Phase C: robustness + noise-floor ──
def test_vo2max_needs_two_point_move_for_trend():
    # +1.0 over 24 days is within Oura noise -> stable (old band would say improving)
    assert selector.vo2max_status([("2026-05-01", 45.0), ("2026-05-25", 46.0)])["status"] == "stable"
    assert selector.vo2max_status([("2026-05-01", 45.0), ("2026-05-25", 47.5)])["status"] == "improving"


def test_sleep_debt_coerces_strings_and_skips_non_dict():
    # Calendar-night-indexed contract: each night needs a date key. Still verifies
    # string coercion of sleep_h ("4.5") and skipping non-dict entries (999, None).
    is_debt, _ = selector.compute_sleep_debt(
        {"nightly": [{"day": "2026-06-13", "sleep_h": "4.5"}, 999, None]})
    assert is_debt is True


def test_compute_adherence_survives_bad_log_date_keys():
    # A non-date key in the log must be skipped, not crash the window pass.
    log = {"not-a-date": {"category": "easy"}, "2026-06-10": {"category": "rest"}}
    out = selector.compute_adherence(log, [], date(2026, 6, 13))
    assert out is not None


# ── hrmax_baseline.select_hrmax (12-mo observed max HR with guards) ──────────
import hrmax_baseline as hrm  # noqa: E402


def test_hrmax_corroborated_peak_kept():
    # Corroborated peak hit multiple times → kept as-is.
    assert hrm.select_hrmax([190, 190, 189, 189, 186]) == 190


def test_hrmax_lone_spike_dropped():
    # A single 210 with the next reading 15 bpm lower = strap artifact → step down.
    assert hrm.select_hrmax([210, 191, 190, 189]) == 191


def test_hrmax_small_gap_kept():
    # Within OUTLIER_GAP (<=6) the top is genuine → keep it.
    assert hrm.select_hrmax([190, 185, 184]) == 190


def test_hrmax_empty_returns_none():
    assert hrm.select_hrmax([]) is None
    assert hrm.select_hrmax([None, None]) is None


# ── Single-mode session invariant (Part A, 2026-07-01) ──────────────────────
import validate_session as vsess  # noqa: E402


def _build(key, tsb=5.3):
    """Build a session with the live paces.json/calibration for invariant checks."""
    import json as _json
    ddir = ROOT / "data"
    pr = _json.loads((ddir / "paces.json").read_text())
    cal = _json.loads((ddir / "calibration.json").read_text()) if (ddir / "calibration.json").exists() else {}
    merged, _ = selector.apply_calibrated_paces(pr, cal)
    p = selector.adjusted_paces(merged, 90, tsb)
    return selector.build_session(key, p, 90, tsb)


def test_every_running_session_is_single_mode():
    # tsb sweep so both rep-count branches (4×4 vs 5×3, threshold A/B) are exercised.
    for key in ("intervals_vo2", "threshold", "long_run", "easy_z2", "strides"):
        for tsb in (5.3, -10.0):
            _, _, _, steps = _build(key, tsb)
            issues = vsess.check_single_mode({"steps": steps})
            assert issues == [], f"{key} (tsb={tsb}) not single-mode: {issues}"


def test_pace_sessions_have_pace_on_every_section():
    for key in ("intervals_vo2", "threshold"):
        _, _, _, steps = _build(key)
        ctrl = [s for s in steps if s.get("mode") in ("pace", "hr")]
        assert ctrl and all(s.get("mode") == "pace" for s in ctrl), f"{key} not all-pace"
        for s in ctrl:
            assert s.get("pace") and s["pace"] != "n/a", f"{key} '{s['name']}' missing pace"
            assert (s.get("duration") or "").strip(), f"{key} '{s['name']}' missing duration"


def test_distance_threshold_session_uses_only_pace_and_distance(monkeypatch):
    class EvenWeekDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 3)  # ISO week 32, the distance-work branch

    monkeypatch.setattr(selector, "date", EvenWeekDate)
    _, _, _, steps = _build("threshold")
    distance_step = next(step for step in steps if step["name"] == "10K-pace run")
    assert distance_step["duration"] == "5 km"
    assert distance_step["pace"] and distance_step["pace"] != "n/a"
    assert "rest_pace" not in distance_step
    assert "hr" not in distance_step


def test_easy_sessions_have_hr_on_every_section():
    for key in ("long_run", "easy_z2"):
        _, _, _, steps = _build(key)
        ctrl = [s for s in steps if s.get("mode") in ("pace", "hr")]
        assert ctrl and all(s.get("mode") == "hr" for s in ctrl), f"{key} not all-hr"
        for s in ctrl:
            assert "n/a" not in (s.get("hr") or "n/a").lower(), f"{key} '{s['name']}' missing hr"


def test_check_single_mode_flags_mixed_and_missing():
    mixed = {"steps": [{"name": "W", "mode": "hr", "hr": "target 130", "duration": "15 min"},
                       {"name": "R", "mode": "pace", "pace": "5:05", "duration": "4 reps"}]}
    assert vsess.check_single_mode(mixed)
    missing = {"steps": [{"name": "R", "mode": "pace", "pace": "n/a", "duration": "4 reps"}]}
    assert vsess.check_single_mode(missing)
    rest = {"steps": [{"name": "Rest", "mode": None, "pace": "n/a", "hr": "n/a", "duration": "All day"}]}
    assert vsess.check_single_mode(rest) == []


