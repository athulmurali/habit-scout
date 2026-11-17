# Contributing

Thanks for your interest! habit-scout is small on purpose, so please keep changes in that spirit.

## Ground rules

- **Standard library only.** No `pip install` step; it runs on the system `python3`.
- **Zero tokens by default.** New logic belongs in the local mining/gate layers. Anything that
  increases how often or how much the LLM is called needs a strong reason.
- **Privacy.** Nothing new should leave the machine without passing through `redact()`, and it
  should be visible in `scout run --dry-run`.
- **New settings go in `DEFAULTS`** in `scout/config.py` with a one-line comment, and in the
  README's config table.

## Workflow

```sh
git clone https://github.com/athulmurali/habit-scout.git && cd habit-scout
python3 -m unittest discover -s tests
HABIT_SCOUT_HOME=/tmp/hs-dev PYTHONPATH=. python3 -m scout candidates
```

Using a throwaway `HABIT_SCOUT_HOME` keeps your real data and decisions untouched.

1. Open an issue first for anything beyond a small fix.
2. Add or update a test in `tests/` for behavior changes.
3. Keep PRs focused; describe what you ran to verify it.

## Good first contributions

- A new collector (e.g. bash/fish history, JetBrains local history)
- More entries for `command_synonyms` / secret patterns in `redact.py`
- Linux scheduling (systemd user timer) as an alternative to launchd
