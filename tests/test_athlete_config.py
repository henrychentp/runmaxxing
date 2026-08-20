import json

import session_selector

EXAMPLE = session_selector.REPO_ROOT / "athlete.example.json"


def test_example_athlete_is_speed_block(monkeypatch):
    monkeypatch.setenv("SESSION_SELECTOR_ATHLETE", str(EXAMPLE))
    monkeypatch.delenv("HEALTH_DB_PATH", raising=False)
    session_selector.apply_athlete_config()
    assert session_selector.GOAL_PRIORITY == "speed"
    assert session_selector.MUSCLE_BASELINE == 50.0
    assert session_selector.HRMAX == 190
    assert session_selector.RHR_DEFAULT == 60


def test_live_athlete_file_overrides_example(tmp_path, monkeypatch):
    cfg = {
        "goal_priority": "fat_loss",
        "muscle_baseline_kg": 48.0,
        "volume_floor_min": 150,
        "vol_ramp_cap": 1.12,
        "vol_ramp_2wk_cap": 1.3,
        "fallback_hrmax": 185,
        "fallback_rhr": 58,
        "z2_hr_ceiling": 145,
    }
    path = tmp_path / "athlete.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("SESSION_SELECTOR_ATHLETE", str(path))
    monkeypatch.delenv("HEALTH_DB_PATH", raising=False)
    try:
        session_selector.apply_athlete_config()
        assert session_selector.GOAL_PRIORITY == "fat_loss"
        assert session_selector.MUSCLE_BASELINE == 48.0
        assert session_selector.HRMAX == 185
        assert session_selector.Z2_HR_CEILING == 145
    finally:
        monkeypatch.setenv("SESSION_SELECTOR_ATHLETE", str(EXAMPLE))
        session_selector.apply_athlete_config()
        assert session_selector.GOAL_PRIORITY == "speed"
