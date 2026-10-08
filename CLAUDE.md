# Working in this repo

- **Iterating:** the "dev branch" is whichever branch the session is on, any branch except `main`. Keep using it between iterations: commit and push each change to it, with no pull request, so the user can pull it into their test environment. Never reset or recreate it.
- **Shipping:** "ship" (also "pr" or "merge": all the same) means start the Ship workflow on the dev branch and stop (see the `ship` skill). It tests, opens the pull request, merges it, and fast-forwards the branch to `main`. Do not open or merge the pull request by hand, and do not subscribe or wait. Other people's and Dependabot's pull requests are merged only when asked.
- **Checks before pushing:** `python -m pytest -q` and `python scripts/eval_prompts.py` (see `DEVELOPMENT.md`).
