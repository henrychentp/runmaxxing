#!/usr/bin/env python3
"""Resting-HR baseline for Karvonen zone math.

Optional DuckDB: set HEALTH_DB_PATH to a read-only health database that has
table `oura_sleep` with column `resting_heart_rate`. Otherwise use FALLBACK_RHR.
"""
import os
from pathlib import Path

DB_PATH = Path(os.environ["HEALTH_DB_PATH"]) if os.environ.get("HEALTH_DB_PATH") else None
RHR_SOURCE = "oura_sleep"
WINDOW_DAYS = 30
AGG = "avg"
MIN_SAMPLES = 7
FALLBACK_RHR = 60


def rhr_baseline():
    if DB_PATH is None or not DB_PATH.exists():
        return FALLBACK_RHR
    try:
        import duckdb

        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            n, val = con.execute(
                f"select count(*), {AGG}(resting_heart_rate) "
                f"from {RHR_SOURCE} "
                f"where resting_heart_rate is not null "
                f"  and day >= current_date - ?",
                [WINDOW_DAYS],
            ).fetchone()
        finally:
            con.close()
        if n and n >= MIN_SAMPLES and val is not None:
            return int(round(val))
    except Exception:
        pass
    return FALLBACK_RHR


if __name__ == "__main__":
    print(rhr_baseline())
