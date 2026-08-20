import json
from io import StringIO

import check_inputs


def test_example_tree_has_no_fix_errors():
    errors = [i for i in check_inputs.collect_issues() if i.startswith("FIX ")]
    assert errors == []


def test_missing_required_file_prints_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SELECTOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "SESSION_SELECTOR_ATHLETE",
        str(check_inputs.ROOT / "athlete.example.json"),
    )
    issues = check_inputs.collect_issues(tmp_path)
    assert any("FIX readiness.json: file missing" in i for i in issues)


def test_wrong_type_prints_field(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SESSION_SELECTOR_ATHLETE",
        str(check_inputs.ROOT / "athlete.example.json"),
    )
    (tmp_path / "readiness.json").write_text(json.dumps({"score": "hot"}))
    issues = check_inputs._check_file(
        tmp_path / "readiness.json",
        "readiness.schema.json",
        True,
    )
    assert any("score" in i and "FIX" in i for i in issues)


def test_report_ok_on_examples():
    buf = StringIO()
    errors = check_inputs.report(stream=buf)
    assert errors == []
    assert "OK inputs" in buf.getvalue()
