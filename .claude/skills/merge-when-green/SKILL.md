---
name: merge-when-green
description: Merge a pull request once its CI has passed. This is the default for every pull request opened in this repo (a plain "pr" means open it and merge it when green); also use when the user says "merge" or runs /merge-when-green.
argument-hint: "[pull request number; default: the open pull request for the current branch]"
---

# Merge when green

The user has made this the default for this repo: after opening a pull request for them ("pr"), follow this skill and merge it with a merge commit (`merge_method: "merge"`, not squash or rebase) as soon as CI is green, without asking again. Invoking it by hand, or saying "merge", covers any open pull request they name. The authorisation is for pull requests I opened or they named; it does not cover Dependabot or anyone else's pull requests unless they ask. If the user says "pr only" or "don't merge", open it and stop.

## 1. Find the pull request
- With an argument, that number. Without one, the open pull request whose head is the current branch (`mcp__github__list_pull_requests`, state `open`, match `head.ref`). None, or more than one: say so and stop.
- Repository: `robdevops/ecobot` (check `git remote get-url origin` if in doubt). Read the pull request with `mcp__github__pull_request_read`. If it is already merged or closed, say so and stop.

## 2. Is it green?
Read the check runs on the **current head commit** (`get_check_runs`), not an earlier one.
- **Green** = the `test` check is `success`, and no other check is `failure`, `cancelled` or `timed_out`. `skipped` is fine (`newest` and `live-evals` only run on the schedule and the Run workflow button, never on pull requests).
- **Pending** (`queued` or `in_progress`, or no checks yet because a push just landed): do not poll in a loop and do not `sleep`. Call `mcp__github__subscribe_pr_activity` for the pull request and end your turn saying you will merge when CI finishes; if a CI result arrives, start again at step 2. (Also schedule a check-in with `mcp__Claude_Code_Remote__send_later` about 10 minutes out if it is available, since events can be missed.)
- **Red**: do not merge. Read the failing job (`get_job_logs`), say what failed. If it is a pull request you opened and the fix is yours to make, fix and push; otherwise report and stop. Never skip, disable or loosen a test to get green, and never push an empty commit to re-run CI.

## 3. Is it mergeable?
- `mergeable_state` of `dirty` (merge conflict): merge the base branch in and resolve it if the pull request is yours; for a Dependabot pull request comment `@dependabot rebase` and wait for its new CI run (back to step 2). Never rewrite someone else's branch.
- Review state: stop if a review has requested changes or a thread is open that names a red-circle (blocking) finding. Say what is open.
- Base moved since CI ran and it was not rebased: fine for GitHub's own check; the `test` run on the head commit is what counts.

## 4. Merge
`mcp__github__merge_pull_request` with `merge_method: "merge"`. Then confirm it merged (`state: closed`, `merged: true`) and report the merge commit.

## 5. After merging
- Bring the dev branch (the head branch, any branch but `main`) up to the merge: `git fetch origin main && git merge --ff-only origin/main && git push origin HEAD`. Never reset or recreate it; it persists between iterations. Delete no branch.
- If the user has other open pull requests that touch the same files (typically several Dependabot ones edit `.github/workflows/ci.yml`), list them: each may now be conflicted and need `@dependabot rebase` and a fresh CI run before its own merge.
- Unsubscribe from the pull request (`mcp__github__unsubscribe_pr_activity`).

## Rules
- One pull request per invocation. "Merge them all" is a separate instruction, and each is still checked as above.
- Never force-merge past a failing or missing required check, never `force` anything, and never merge a pull request you only watched unless the user named it.
- Report outcomes plainly: what was green, what was skipped, what was merged, and anything left open.
- Keep the final message to a couple of words ("Merged #32.", "CI red: test."). Say nothing about each notification along the way.
