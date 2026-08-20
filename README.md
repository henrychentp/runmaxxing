# Runmaxxing

Daily workout picker for endurance runners who also lift.

It scores candidate sessions (easy Z2, long run, VO2 intervals, threshold, strides, gym, rest). Safety gates can block a session even when its score is high. The highest valid score wins. There is no fixed weekly calendar. Body state and load still override the suggested rotation.

This public copy is the decision engine plus example JSON. It does not include live health exports, API tokens, or a private Strava helper.

## Install

```bash
git clone https://github.com/henrychentp/runmaxxing.git
cd runmaxxing
python3 -m pip install -e ".[dev]"
```

Copy `athlete.example.json` to `data/athlete.json` and edit your numbers. That live file is gitignored.

## What it reads

Put files in `data/` (examples ship in this repo):

| File | Role |
| --- | --- |
| `readiness.json` | Composite readiness, injury flag, stale flag |
| `fitness_freshness.json` | CTL / ATL / TSB |
| `paces.json` | Easy, Z2, threshold, interval speeds |
| `oura_trends.json` | HRV series, sleep, RHR trend |
| `briefing_context.json` | ACWR and optional weather |
| `recovery_signals.json` | Optional stress, SpO2, VO2 |
| `health_snapshot.json` | Optional VAT, body fat, muscle mass |
| `week_plan.json` | Weekly budget (quality / long / gym) |
| `recent_activities.json` | Recent runs and lifts (or set `SESSION_SELECTOR_RECENT_CMD`) |
| `illness.json` | Optional manual illness override |

Wire your own sync (Oura, Strava, scale) to write the same shapes. Optional DuckDB: set `HEALTH_DB_PATH` for rolling RHR and max HR. If it is unset, the engine uses the fallbacks in `athlete.example.json`.

Public knobs live in `athlete.example.json` at the repo root. Copy that file to `data/athlete.json` for your numbers.

## Run

```bash
runmaxxing
```

Or:

```bash
python3 session_selector.py
```

Writes `data/dynamic_session.json`.

### Replay a week

Pin the selector to a fixed calendar day and an alternate data directory (for tests or regression checks):

```bash
export SESSION_SELECTOR_TODAY=2026-08-20
export SESSION_SELECTOR_DATA_DIR=/tmp/runmaxxing-replay
unset HEALTH_DB_PATH   # optional: use fallback RHR/HRmax instead of DuckDB
python3 session_selector.py
```

Copy `data/*.json` into `SESSION_SELECTOR_DATA_DIR` first. Omit `dynamic_session.json` and any `session-history/` archive. Strip same-day activities from `recent_activities.json` if you want a morning-before-the-run snapshot.

```bash
python3 -m pytest tests/ -q
```

## Gates (short)

- Low readiness: rest or easy only
- TSB too low for the session type
- Back-to-back hard days
- HRV suppressed plus falling
- Weekly hard-density cap
- Volume ramp vs trailing weeks
- Post-ultra recovery window from event duration, not TSB
- Illness file forces rest

Edit `data/athlete.json` (copy from `athlete.example.json`): `goal_priority` is `fat_loss`, `speed`, or `balanced`. Set `muscle_baseline_kg` to your lean mass.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT
