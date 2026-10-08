# Working in this repo

- **Pull requests:** "pr" means: create the pull request to `main`, wait for CI (subscribe to its events; no polling), merge it with a merge commit once it is green, restart `claude/envirobot-next` from `main`, and unsubscribe. Do all of it without asking for permission or confirmation (the tools are pre-approved in `.claude/settings.json`). Finish with a couple of words, e.g. "Merged #32." or, if CI failed, "CI red: <check>." Do not narrate notifications or recap. See the `merge-when-green` skill. "pr only" or "don't merge" means open it and stop. Other people's and Dependabot's pull requests are merged only when asked.
- **Branch:** work on `claude/envirobot-next`; after a merge, restart it from `main` (`git fetch origin main && git checkout -B claude/envirobot-next origin/main`).
- **Checks before pushing:** `python -m pytest -q` and `python scripts/eval_prompts.py` (see `DEVELOPMENT.md`).
