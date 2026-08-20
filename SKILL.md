# Runmaxxing (for coding agents)

You wire data. You do not pick the workout.

## Goal

Map the user's Oura, Strava, scale, or CSV exports into `data/*.json`. Then run the picker. The engine's output is the session. Do not override it with a chat recommendation.

## Install

```bash
python3 -m pip install -e ".[dev]"
cp athlete.example.json data/athlete.json
```

Edit `data/athlete.json` with the user's numbers. Do not commit that file. Do not invent live health values.

## Contract

JSON Schema files live in `schemas/`. Example payloads live in `data/` and `athlete.example.json`.

Required:

- `data/readiness.json`
- `data/paces.json`
- `data/fitness_freshness.json`
- `data/recent_activities.json`

Optional but useful: `oura_trends.json`, `briefing_context.json`, `recovery_signals.json`, `health_snapshot.json`, `week_plan.json`, `illness.json`.

## Loop

1. Read `SKILL.md` and the matching schema before writing a file.
2. Write adapters. Do not copy the user's home paths into the repo.
3. Run `runmaxxing-check`. Fix every `FIX` line. `WARN` means a weaker gate, not a crash.
4. Run `runmaxxing`. Read `data/dynamic_session.json`.
5. Do not change `recommendation.workout` by hand.

## Hard rules

- Do not pick easy vs intervals vs rest in prose if it disagrees with the JSON out.
- Do not commit tokens, `.env`, or live exports.
- Keep output JSON stable if you touch writers: sorted keys, 2-space indent, trailing newline.
