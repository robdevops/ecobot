# Developing

How to test the bot and what the GitHub checks do. (Running it is in `README.md`.)

## Tests
```
pip install -r requirements-dev.txt
python -m pytest                       # runs in parallel (pytest.ini); `-n0` for one process
python scripts/eval_prompts.py         # the saved questions: decisions made in code, no key needed
```
The suite takes about 25 s on four cores. The slowest fixture, the fake station's 18-month archive (`archived_cache` in `tests/conftest.py`),
is built once an hour and shared by the workers and by later runs: it lives under pytest's own temp folder (`/tmp/pytest-of-<user>/ecobot-archive-*.sqlite`),
named for the hour and for the code that shapes it, so a change to `lib/storage`, `lib/ecowitt` or `tests/fakes.py` rebuilds it. The first run after
that pays about 15 s more. Tests of the archive itself use a station a few weeks old (`ecowitt_transport(history_days=...)`); the storage's reader
and writer threads, and `asyncio.to_thread`, run inline in tests (the `threaded_storage` fixture puts the threads back for the tests of them).

The code targets Python 3.13.5 (`.python-version`). Claude Code sessions build the same Python into `.venv` with uv (`.claude/hooks/session-start.sh`).

## GitHub Actions (`.github/workflows/ci.yml`, and `docker.yml` for `image`)
| Job | When | What |
|---|---|---|
| `test` | every push to `main` and every pull request | the exact versions in `requirements.txt`: pytest (chart pictures included) and the offline evals. The gate. |
| `newest` | Sundays 21:17 UTC, or the Run workflow button | the tests with the newest Pillow. Advisory. |
| `live-evals` | Sundays 21:17 UTC, or the Run workflow button | asks the real model the six questions in `tests/evals/weekly.txt` (`python scripts/eval_prompts.py --live`) and checks which tool it calls; a failure is asked once more before it is reported. Advisory. The button's "all cases" box asks all 87. Skipped until the secrets below exist. |
| `image` | pushes that change `Dockerfile`, `.dockerignore` or `requirements.txt` (any branch), or the Run workflow button | builds the Docker image, checks the bot starts in it and names the commit, draws the sample charts in it (they must be in Inter and match `tests/charts`); the image's size goes in the run's summary. |

Repository secrets (Settings, Secrets and variables, Actions, New repository secret):
- `OPENROUTER_API_KEY`: for `live-evals`. `OPENROUTER_MODEL` is optional (default `deepseek/deepseek-v4.1-flash-1017`): add it only if production runs another model.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: optional; failures of `newest` and `live-evals`, and of `test` on `main`, are then sent to Telegram.

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for the Python packages and the actions. `Pillow` comes in a group of its own, because the chart pictures depend on it.

## Chart pictures
Ten fixed sample charts and dashboards (`tests/chart_samples.py`, made-up data, no clock) are drawn and compared with reference images in `tests/charts/` (`tests/test_chart_images.py`, marker `golden`). They are drawn with Pillow's own font (so they match on any machine; it lacks a few characters, such as ₂ and µ, shown as boxes there only), shrunk to 40% and cut to 64 colours, so each is 7 to 46 KB (about 220 KB in all). A chart passes when under 0.1% of its pixels differ noticeably; a failure leaves the new picture and a red-on-grey picture of the difference in `chart-diffs/` (CI uploads it as the `chart-diffs` artifact). After an intended change to how charts look, run `python scripts/update_charts.py` (or name charts: `... wind air`) and commit the changed images: GitHub shows old and new side by side in the pull request. `Pillow` is pinned in `requirements.txt` because the pictures depend on it (charts and the dashboard are drawn with it, `lib/draw.py`); a Dependabot update whose chart test fails is the cue to redo the images.
