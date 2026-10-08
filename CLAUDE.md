# Working in this repo

- **Iterating:** the "dev branch" is whichever branch the session is on, any branch except `main`. Keep using it between iterations: commit and push each change to it, so the user can pull it into their test environment. Never reset or recreate it.
- **Checks before pushing:** `python -m pytest -q` and `python scripts/eval_prompts.py` (see `DEVELOPMENT.md`).
