# Runmaxxing

Daily workout picker for endurance runners who also lift.

It scores candidate sessions (easy Z2, long run, VO2 intervals, threshold, strides, gym, rest). Safety gates can block a session even when its score is high. The highest valid score wins. There is no fixed weekly calendar. Body state and load still override the suggested rotation.

This public copy is the decision engine plus example JSON. It does not include live health exports, API tokens, or a private Strava helper.

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

Wire your own sync (Oura, Strava, scale) to write the same shapes. Optional DuckDB: set `HEALTH_DB_PATH` for rolling RHR and max HR. If it is unset, the engine uses fallback RHR 60 and HRmax 190.

## Run

```bash
python3 session_selector.py
```

Writes `data/dynamic_session.json`.

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

Edit `GOAL_PRIORITY` (`fat_loss` / `speed` / `balanced`) and `MUSCLE_BASELINE` in `session_selector.py` for your block.

## License

MIT
