# Contributing

## Setup

```bash
git clone https://github.com/henrychentp/runmaxxing.git
cd runmaxxing
python3 -m pip install -e ".[dev]"
```

## Check

```bash
runmaxxing-check
pytest tests/ -q
```

The frozen-week test must still pick the same session. Do not change `tests/fixtures/week_build/expected.json` unless the pick change is the point of the pull request.

## Changes

- Open a pull request against `main`.
- Keep live health files and tokens off the tree.
- Put your personal numbers in `data/athlete.json` (gitignored). Do not edit `athlete.example.json` unless you are changing the public example athlete.
- Keep JSON output stable (`dump_json`): sorted keys, 2-space indent, trailing newline.
- Load `SKILL.md` before changing input shapes.
