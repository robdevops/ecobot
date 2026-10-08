# Working in this repo

- **Pull requests:** "pr" means open a pull request to `main` and then merge it with a merge commit once CI is green (the `merge-when-green` skill, `.claude/skills/merge-when-green/SKILL.md`). "pr only" or "don't merge" means open it and stop. Other people's and Dependabot's pull requests are merged only when asked.
- **Branch:** work on `claude/envirobot-next`; after a merge, restart it from `main` (`git fetch origin main && git checkout -B claude/envirobot-next origin/main`).
- **Checks before pushing:** `python -m pytest -q` and `python scripts/eval_prompts.py` (see `DEVELOPMENT.md`).
