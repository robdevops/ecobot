---
name: ship
description: Start the pipeline that merges the dev branch into main (tests, pull request, merge). Use when the user says "ship", "pr", "merge" or runs /ship.
---

# Ship

"ship" (or "pr", or "merge": all the same) starts the **Ship** workflow (`.github/workflows/ship.yml`) on the dev branch (the branch the session is on: any branch except `main`). The workflow runs the CI gate, opens the pull request to `main`, merges it with a merge commit, and fast-forwards the dev branch to `main`. Nothing here waits or merges by hand.

1. Everything must be committed and pushed to the dev branch; push first if not. Run `python -m pytest -q` and `python scripts/eval_prompts.py` before pushing.
2. Start it: `mcp__github__actions_run_trigger` (`run_workflow`, owner `robdevops`, repo `ecobot`, workflow `ship.yml`, ref the dev branch).
3. Reply with one line: "Shipping." and the run link if known. Do not subscribe, poll or recap. GitHub tells the user if it fails; if they ask, read the run with `mcp__github__actions_get` and `get_job_logs`.
4. Do not reset or recreate the dev branch afterwards: the workflow fast-forwards it. Before the next change, `git fetch origin && git merge --ff-only origin/<dev branch>` if the remote moved.

If the workflow cannot start (it is not on `main` yet, so the Run workflow button and the API do not know it), say so: the first ship has to be a pull request merged by hand.
