# Developing

How to test the servers and what the GitHub checks do. (Running it is in `README.md`.)

## Tests
```
pip install -r requirements-dev.txt     # requirements.txt (requirements-mcp.txt and requirements-irrigation.txt), requirements-charts.txt (Pillow) and pytest
python -m pytest                       # runs in parallel (pytest.ini); `-n0` for one process; the slow tests left out
python -m pytest -m ""                 # every test, the slow ones too (as CI runs them): before pushing
python -m pytest -m slow               # the slow ones alone
```
Plain `pytest` takes about 19 s on four cores; everything (`-m ""`) about 51 s, and must stay under a minute: the browser tests are split
into parts that run side by side, and wait on the page (`calm` in `tests/web_dashboard_browser.js`) rather than for fixed times.
The weather's tests count its own frames and time, never the machine's: each of its frames is a fixed step of its time however long it
took to draw (`window.__skyTest`, see `weatherPage`), and most of its pages are driven (`driven`: the test makes the frames, one after
another, from the same start whatever the load, its randomness seeded: the same frames every run; `SKY_SEED=n` seeds it otherwise, to
see how near a check is to failing). A frame's cost is the page script's own CPU time (`frameCost`), not the clock's. The slow tests, 0.5 s or more each run alone (the browser
tests of the web page, the service evals over every tool, the tests that start servers, the archive's own), are listed in
`tests/slow_tests.txt` and marked `slow` by `tests/conftest.py`; `python scripts/slow_tests.py` measures every test again and rewrites the
list (about two and a half minutes). The slowest fixture, the fake station's 18-month archive (`archived_cache` in `tests/conftest.py`),
is built once an hour and shared by the workers and by later runs: it lives under pytest's own temp folder (`/tmp/pytest-of-<user>/ecobot-archive-*.sqlite`),
named for the hour and for the code that shapes it, so a change to `lib/storage`, `lib/ecowitt` or `tests/fakes.py` rebuilds it. The first run after
that pays about 15 s more. Tests of the archive itself use a station a few weeks old (`ecowitt_transport(history_days=...)`); the storage's reader
and writer threads, and `asyncio.to_thread`, run inline in tests (the `threaded_storage` fixture puts the threads back for the tests of them).

The clock: most tests run at the real time, so some answers (just after midnight, late in the evening, on the 1st of a month or of the year)
are only tested when the suite happens to run then. To run the whole suite at such a time on purpose, set `ECOBOT_CLOCK` to a Melbourne
wall-clock time: the clock starts there and runs on, for lib/ and the tests, the archive included (`tests/clocks.py`):
```
ECOBOT_CLOCK="2026-11-11 00:05" python -m pytest -q -m ""   # also 23:55 or 23:59, and 00:05 on the 1st of a month and on 1 Jan
```
A test whose answer depends on the time of day or the date pins its own clock instead: `daytime_archive` (2:05pm on Tue 10 Nov 2026) or
`pinned_archive(when)` with `clocks.pin(monkeypatch, when)`, the fake station's archive as it was then, built once for the code that shapes
it and kept (`ecobot-archive-pinned-*.sqlite`); a tool's answer to words alone can take `Context(clock=...)` (`clocks.yesterday_at`).

The engines the service's tools wrap are tested directly where that is simpler: `tests/engines.py` drives the station's
`HistoryQuery`, `link()` and the air sensor's history and reading now. The old tools' handlers, the oracle the
service was ported against, are gone: `tests/test_history.py` and `tests/test_days.py` hold the service's `readings` and `days`
tools to the stored rows and the fake station's own figures, and current's words are pinned in `tests/current_words.json`.

The code targets Python 3.13.5 (`.python-version`). Claude Code sessions build the same Python into `.venv` with uv (`.claude/hooks/session-start.sh`).

## ecobot-mcp
ecobot-mcp, the MCP server (`python -m ecobot_mcp`, the package `ecobot_mcp/`; [docs/ecobot-mcp.md](docs/ecobot-mcp.md) 3.8): the
sources and their syncers, the alert rules writing the alert log, and the MCP server (`lib/mcp_server.py`) at
`http://MCP_BIND/mcp` (Streamable HTTP, protocol 2026-07-28 and the older one). It reads the `MCP_` settings and the shared ones
([docs/configuration.md](docs/configuration.md)); its packages are `requirements-mcp.txt` and Pillow (`requirements-charts.txt`);
`requirements.txt` adds irrigation-mcp's (`requirements-irrigation.txt`), for a systemd install's one venv:
```
set -a; . <(sudo cat /etc/ecobot.env); set +a          # the settings
DATA_DIR=/tmp/ecobot python -m ecobot_mcp              # http://127.0.0.1:8765/mcp
sudo docker compose up -d ecobot-mcp                   # in Docker (compose.yaml), on the host's port 8765
```
Every request needs a bearer token: one of `AUTH_TOKENS`, or, with that unset, the one it generates at its first start and keeps in
the data folder (`lib/auth_token.py`: `DATA_DIR/auth_token`, mode 0600, made whole and then linked into place; its path is logged,
never its value). Only one ecobot-mcp runs on a data folder: it holds `mcp.lock` there, so a second refuses to start.
Claude Code connects over HTTP: `claude mcp add --transport http ecobot http://127.0.0.1:8765/mcp --header "Authorization: Bearer $(cat /tmp/ecobot/auth_token)"`.
A client that shows pictures can have the charts as images: `http://127.0.0.1:8765/mcp?charts=png` (or, per call, request `_meta`
`chart_image: "png"`; docs 3.7). Pillow (`requirements-charts.txt`) is imported only then: installed from `requirements-mcp.txt` alone
ecobot-mcp answers with the charts' JSON and a `chart_image_note`, and warns once.
To look at it by hand: `npx @modelcontextprotocol/inspector` against the URL with the header `Authorization: Bearer <token>`.

Its clients are other programs: signalbot reads `ecobot://feed` (the alerts, `lib/feed.py`), a model client calls the tools. In Docker
(compose.yaml) each server runs from its own image (the Dockerfile's targets `ecobot-mcp` and `irrigation-mcp`): `ecobot-mcp` always,
irrigation-mcp under the `irrigation` profile. Both mount the data folder, so a client on the host reads the token there. ecobot-mcp
answers its own hostname, so a client on the compose network needs no `MCP_ALLOWED_HOSTS`. Under systemd, `irrigation-mcp.service`
starts after `ecobot-mcp.service` when it is installed.
```
set -a; . <(sudo cat /etc/ecobot.env); set +a
export DATA_DIR=/tmp/ecobot
python -m ecobot_mcp &                                   # ecobot-mcp, with the sources' settings (it writes the token)
python -m irrigation_mcp &                               # irrigation-mcp, with the IRRIGATION_TUYA_ settings (optional)
```

Its tests: `tests/test_mcp_server.py` (the SDK's in-memory transport: every tool listed and answering as the ServiceTool does, each resource,
the feed, the prompts, the publishing, the tokens; the chart images asked per call and, under uvicorn, by the URL's
`?charts=png`, without Pillow, and that importing the server imports no Pillow), `tests/test_mcp_listen.py` (the real app under uvicorn
on a free localhost port: a client that listens loses its listen when ecobot-mcp stops and reads what it missed when it is back; the
token and Host checks over HTTP; `python -m ecobot_mcp` with the fake sources, a tool call through a proxy's Host name),
`tests/test_settings.py` (each setting's owner prefix, the old names, the generated token) and
`tests/test_packaging.py` (the two images, compose, the systemd units). The servers' tests talk to them with the SDK's client
(`tests/mcp_client.py`: `McpClient`, the `serve_mcp` and `serve_irrigation_mcp` fixtures in `tests/conftest.py`, in memory or over HTTP),
and `tests/test_server_imports.py` checks that neither server's import graph reaches a module it must not (no chat or model client).

## irrigation-mcp
`python -m irrigation_mcp` (`irrigation_mcp/__main__.py`) is the irrigation controller's MCP server (docs/ecobot-mcp.md 3.9, step 8):
`lib/irrigation/server.py` (its four tools and `irrigation://feed`, the low-level SDK as `lib/mcp_server.py`), `watchdog.py` and
`check.py` (the watchdog and the daily check it runs), `ecobot` (its client of ecobot-mcp, `lib/upstream.Upstream`), `state.py`
(`irrigation_state.json`), `tuya.py`, and `common.py` (what the server and its tests share, without httpx: `lib/irrigation` loads nothing
until asked). Under systemd it is `irrigation-mcp.service`, in Docker the
`irrigation-mcp` container. What it shares with ecobot-mcp over HTTP (the token and Host checks, the request log, uvicorn, the
generated token, the data folder's lock) is `lib/mcp_http.py`. It imports none of ecobot-mcp's sources or tools
(`tests/test_irrigation_mcp.py` checks it in a subprocess, as the Docker build does). Its tests run its server over the SDK's in-memory
transport with a fake controller (`tests.fakes.FakeController`, or the fake Tuya cloud `FakeTuya`), and its daily check against
ecobot-mcp in memory (`tests/mcp_client.py`); the controller's own code (Tuya, the check, the watchdog) is tested in
`tests/test_irrigation_controller.py`.

### Settings
Each is a field of `lib/config.Config`, named for the program that reads it (`MCP_`, `IRRIGATION_`, or none when both do;
docs/configuration.md), with its old name or names, newest first (`_setting(..., old=...)`, `old=("NEWER", "OLDER")`), still read when
the new one is unset and named in one warning at start. Scripts that read one directly use `lib.config.env(name)`, which knows the old names too. The test suite unsets every setting's
variable, new and old, before each test (`tests/conftest.py`).

## GitHub Actions (`.github/workflows/ci.yml`, and `docker.yml` for `image`)
| Job | When | What |
|---|---|---|
| `test` | every push to `main` and every pull request | the exact versions in `requirements.txt` and `requirements-charts.txt`: pytest, every test, chart pictures included. The gate. |
| `newest` | Sundays 21:17 UTC, or the Run workflow button | the tests with the newest Pillow. Advisory. |
| `image` | pushes that change `Dockerfile`, `.dockerignore`, `requirements.txt`, `requirements-mcp.txt`, `requirements-irrigation.txt` or `requirements-charts.txt` (any branch), or the Run workflow button | builds the two Docker images (`ecobot-mcp` and `irrigation-mcp`; each build imports its program, irrigation-mcp's with no Pillow or source module in it), checks each program starts in its image and names the commit, draws the sample charts in ecobot-mcp's (they must be in Inter and match `tests/charts`); the images' sizes go in the run's summary. |

The workflows need no repository secrets. (Failure notices to Telegram, with their `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets, and the `live-evals` job, with `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`, went with the Telegram bot: those secrets can be deleted in the repository's settings.)

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for the Python packages and the actions. `Pillow` comes in a group of its own, because the chart pictures depend on it.

## Chart pictures
Ten fixed sample charts and the Status picture (`tests/chart_samples.py`, made-up data, no clock) are drawn and compared with reference images in `tests/charts/` (`tests/test_chart_images.py`, marker `golden`). They are drawn with Pillow's own font (so they match on any machine; it lacks a few characters, such as ₂ and µ, shown as boxes there only), shrunk to 40% and cut to 64 colours, so each is 7 to 46 KB (about 220 KB in all). A chart passes when under 0.1% of its pixels differ noticeably; a failure leaves the new picture and a red-on-grey picture of the difference in `chart-diffs/` (CI uploads it as the `chart-diffs` artifact). After an intended change to how charts look, run `python scripts/update_charts.py` (or name charts: `... wind air`) and commit the changed images: GitHub shows old and new side by side in the pull request. `Pillow` is pinned in `requirements-charts.txt` because the pictures depend on it (charts and the Status picture are drawn with it, `lib/draw.py`; ecobot-mcp needs it only for its pictures, irrigation-mcp not at all, so it is a file of its own); a Dependabot update whose chart test fails is the cue to redo the images.
