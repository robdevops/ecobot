# Developing

How to test the bot and what the GitHub checks do. (Running it is in `README.md`.)

## Tests
```
pip install -r requirements-dev.txt
python -m pytest                       # runs in parallel (pytest.ini); `-n0` for one process
python scripts/eval_prompts.py         # the saved questions: decisions made in code, no key needed
```
The code targets Python 3.13.5 (`.python-version`). Claude Code sessions build the same Python into `.venv` with uv (`.claude/hooks/session-start.sh`).

## GitHub Actions (`.github/workflows/ci.yml`)
| Job | When | What |
|---|---|---|
| `test` | every push to `main` and every pull request | the exact versions in `requirements.txt`: pytest (chart pictures included) and the offline evals. The gate. |
| `newest` | Sundays 21:17 UTC, or the Run workflow button | the tests with the newest matplotlib, numpy and Pillow. Advisory. |
| `live-evals` | Sundays 21:17 UTC, or the Run workflow button | asks the real model the six questions in `tests/evals/weekly.txt` (`python scripts/eval_prompts.py --live`) and checks which tool it calls; a failure is asked once more before it is reported. Advisory. The button's "all cases" box asks all 87. Skipped until the secrets below exist. |

Repository secrets (Settings, Secrets and variables, Actions, New repository secret):
- `XAI_API_KEY`, `XAI_BASE_URL`, `XAI_MODEL`: for `live-evals`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: optional; failures of any job, and of `test` on `main`, are then sent to Telegram.

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for the Python packages and the actions. `matplotlib`, `numpy` and `Pillow` come as one pull request, because the chart pictures depend on them.

## Chart pictures
Seven fixed sample charts (`tests/chart_samples.py`, made-up data, no clock) are drawn and compared with reference images in `tests/charts/` (`tests/test_chart_images.py`, marker `golden`). They are drawn with matplotlib's own font, shrunk to 40% and cut to 64 colours, so each is 7 to 35 KB (about 140 KB in all). A chart passes when under 0.1% of its pixels differ noticeably; a failure leaves the new picture and a red-on-grey picture of the difference in `chart-diffs/` (CI uploads it as the `chart-diffs` artifact). After an intended change to how charts look, run `python scripts/update_charts.py` (or name charts: `... wind air`) and commit the changed images: GitHub shows old and new side by side in the pull request. `matplotlib`, `numpy` and `Pillow` are pinned in `requirements.txt` because the pictures depend on them; Dependabot (`.github/dependabot.yml`) proposes updates to the three as one pull request, whose failing chart test is the cue to redo the images.
