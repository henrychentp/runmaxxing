#!/usr/bin/env python3
"""Observed max HR with spike guards.

Optional DuckDB: set HEALTH_DB_PATH to a database with `strava_activities`
(max_heartrate, has_heartrate, average_heartrate, start_date). Otherwise use
FALLBACK_HRMAX.
"""
import os
from pathlib import Path

DB_PATH = Path(os.environ["HEALTH_DB_PATH"]) if os.environ.get("HEALTH_DB_PATH") else None
WINDOW_DAYS = 365
EFFORT_AVG_HR = 140
OUTLIER_GAP = 6
FALLBACK_HRMAX = 190


def select_hrmax(vals):
    """Pick max HR from a DESC-sorted list of per-effort max_heartrate readings."""
    vals = [v for v in vals if v]
    if not vals:
        return None
    top = vals[0]
    second = vals[1] if len(vals) > 1 else top
    if top - second > OUTLIER_GAP:
        top = second
    return int(round(top))


def hrmax_baseline():
    if DB_PATH is None or not DB_PATH.exists():
        return FALLBACK_HRMAX
    try:
        import duckdb

        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            rows = con.execute(
                "select max_heartrate from strava_activities "
                "where max_heartrate is not null and has_heartrate "
                "  and average_heartrate >= ? "
                "  and start_date >= current_date - ? "
                "order by max_heartrate desc limit 5",
                [EFFORT_AVG_HR, WINDOW_DAYS],
            ).fetchall()
        finally:
            con.close()
        picked = select_hrmax([r[0] for r in rows])
        if picked is not None:
            return picked
    except Exception:
        pass
    return FALLBACK_HRMAX


if __name__ == "__main__":
    print(hrmax_baseline())
