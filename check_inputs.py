#!/usr/bin/env python3
"""Loud input check for Runmaxxing.

Prints FIX lines an LLM can patch. Does not pick a workout.
Exit 1 only when required files are missing or the wrong shape.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schemas"

REQUIRED_DATA = (
    ("readiness.json", "readiness.schema.json"),
    ("paces.json", "paces.schema.json"),
    ("fitness_freshness.json", "fitness_freshness.schema.json"),
    ("recent_activities.json", "recent_activities.schema.json"),
)

OPTIONAL_DATA = (
    ("oura_trends.json", "oura_trends.schema.json"),
    ("briefing_context.json", "briefing_context.schema.json"),
    ("recovery_signals.json", "recovery_signals.schema.json"),
    ("health_snapshot.json", "health_snapshot.schema.json"),
    ("week_plan.json", "week_plan.schema.json"),
    ("illness.json", "illness.schema.json"),
)


def data_dir() -> Path:
    override = os.environ.get("SESSION_SELECTOR_DATA_DIR")
    if override:
        return Path(override)
    return ROOT / "data"


def athlete_path() -> Path:
    override = os.environ.get("SESSION_SELECTOR_ATHLETE")
    if override:
        return Path(override)
    live = data_dir() / "athlete.json"
    if live.exists():
        return live
    return ROOT / "athlete.example.json"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate(value, schema: dict, path: str, issues: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_ok(value, t) for t in expected):
            issues.append(f"FIX {path}: want type {expected}, got {type(value).__name__}")
            return
    elif expected and not _type_ok(value, expected):
        issues.append(f"FIX {path}: want type {expected}, got {type(value).__name__}")
        return
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        issues.append(f"FIX {path}: want one of {enum}, got {value!r}")
    if expected == "object" or isinstance(value, dict):
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        if not isinstance(value, dict):
            return
        for key in required:
            if key not in value:
                issues.append(f"FIX {path}: missing required field {key}")
        for key, sub in props.items():
            if key in value:
                child = f"{path}.{key}" if path else key
                _validate(value[key], sub, child, issues)
    if (expected == "array" or isinstance(value, list)) and "items" in schema:
        if not isinstance(value, list):
            return
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]", issues)


def _check_file(path: Path, schema_name: str, required: bool) -> list[str]:
    issues: list[str] = []
    rel = path.name
    if not path.exists():
        if required:
            issues.append(f"FIX {rel}: file missing. Copy data/{rel} from the repo examples, then map your export.")
        else:
            issues.append(f"WARN {rel}: missing (optional). Gates that use it will be weaker.")
        return issues
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        issues.append(f"FIX {rel}: invalid JSON ({e})")
        return issues
    _validate(data, _load_schema(schema_name), rel, issues)
    return issues


def collect_issues(base: Path | None = None) -> list[str]:
    base = base or data_dir()
    issues: list[str] = []
    issues.extend(_check_file(athlete_path(), "athlete.schema.json", True))
    for name, schema in REQUIRED_DATA:
        issues.extend(_check_file(base / name, schema, True))
    for name, schema in OPTIONAL_DATA:
        issues.extend(_check_file(base / name, schema, False))
    return issues


def report(base: Path | None = None, stream=None) -> list[str]:
    stream = stream or sys.stderr
    issues = collect_issues(base)
    errors = [i for i in issues if i.startswith("FIX ")]
    warns = [i for i in issues if i.startswith("WARN ")]
    for line in issues:
        print(line, file=stream)
    if not errors:
        print("OK inputs match schemas. Do not pick the workout. Run runmaxxing.", file=stream)
    return errors


def cli() -> None:
    errors = report()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    cli()
