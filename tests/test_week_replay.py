import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_selector  # noqa: E402
import validate_session as vsess  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "week_build"
IGNORED_DATA = {"session-history", "dynamic_session.json", "README.md"}


def _copy_data_to(tmp_path: Path) -> None:
    src = ROOT / "data"
    for item in src.iterdir():
        if item.name in IGNORED_DATA:
            continue
        if item.is_dir():
            continue
        if item.suffix != ".json":
            continue
        dest = tmp_path / item.name
        dest.write_text(item.read_text())


def _strip_today_activities(tmp_path: Path, day: str) -> None:
    ra_path = tmp_path / "recent_activities.json"
    activities = json.loads(ra_path.read_text())
    activities = [a for a in activities if a.get("date") != day]
    ra_path.write_text(json.dumps(activities, indent=2) + "\n")


@pytest.fixture
def expected():
    return json.loads((FIXTURE_DIR / "expected.json").read_text())


def test_week_replay_frozen_2026_08_20(tmp_path, monkeypatch, expected):
    _copy_data_to(tmp_path)
    _strip_today_activities(tmp_path, expected["today"])

    monkeypatch.setenv("SESSION_SELECTOR_TODAY", expected["today"])
    monkeypatch.setenv("SESSION_SELECTOR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HEALTH_DB_PATH", raising=False)

    previous_data_dir = session_selector.DATA_DIR
    try:
        session_selector.apply_runtime_env()
        out = session_selector.main()
    finally:
        session_selector.DATA_DIR = previous_data_dir

    winner = out["recommendation"]["workout"]
    blocked = out.get("blocked", {})
    scores = out.get("scores", {})

    assert out.get("generated") == expected["today"]
    assert winner == expected["winner"]
    assert winner not in blocked
    assert sorted(blocked.keys()) == expected["blocked"]

    badge = vsess.validate(out, today=date.fromisoformat(expected["today"]))
    assert badge["status"] == expected["badge"]
    assert scores.get(winner, -1) >= 0

    written = (tmp_path / "dynamic_session.json").read_text()
    parsed = json.loads(written)
    assert written == session_selector.dump_json(parsed)
    assert parsed["recommendation"]["workout"] == winner
