# Developing

How to test the bot and what the GitHub checks do. (Running it is in `README.md`.)

## Tests
```
pip install -r requirements-dev.txt     # requirements.txt, requirements-charts.txt (Pillow) and pytest
python -m pytest                       # runs in parallel (pytest.ini); `-n0` for one process
python scripts/eval_prompts.py         # the saved questions: decisions made in code and the service expectations, no key needed
```
The suite takes about 25 s on four cores. The slowest fixture, the fake station's 18-month archive (`archived_cache` in `tests/conftest.py`),
is built once an hour and shared by the workers and by later runs: it lives under pytest's own temp folder (`/tmp/pytest-of-<user>/ecobot-archive-*.sqlite`),
named for the hour and for the code that shapes it, so a change to `lib/storage`, `lib/ecowitt` or `tests/fakes.py` rebuilds it. The first run after
that pays about 15 s more. Tests of the archive itself use a station a few weeks old (`ecowitt_transport(history_days=...)`); the storage's reader
and writer threads, and `asyncio.to_thread`, run inline in tests (the `threaded_storage` fixture puts the threads back for the tests of them).

The clock: most tests run at the real time, so some answers (just after midnight, late in the evening, on the 1st of a month or of the year)
are only tested when the suite happens to run then. To run the whole suite at such a time on purpose, set `ECOBOT_CLOCK` to a Melbourne
wall-clock time: the clock starts there and runs on, for lib/ and the tests, the archive included (`tests/clocks.py`):
```
ECOBOT_CLOCK="2026-11-11 00:05" python -m pytest -q    # also 23:55 or 23:59, and 00:05 on the 1st of a month and on 1 Jan
```
A test whose answer depends on the time of day or the date pins its own clock instead: `daytime_archive` (2:05pm on Tue 10 Nov 2026) or
`pinned_archive(when)` with `clocks.pin(monkeypatch, when)`, the fake station's archive as it was then, built once for the code that shapes
it and kept (`ecobot-archive-pinned-*.sqlite`); a tool's answer to words alone can take `Context(clock=...)` (`clocks.yesterday_at`).

The code targets Python 3.13.5 (`.python-version`). Claude Code sessions build the same Python into `.venv` with uv (`.claude/hooks/session-start.sh`).

## The MCP service
The service on its own ([docs/mcp-service.md](docs/mcp-service.md) 3.8, step 5): the sources and their syncers, the alert rules writing the
alert log, and the MCP server (`lib/mcp_server.py`) at `http://MCP_SERVICE_BIND/mcp` (Streamable HTTP, protocol 2026-07-28 and the older one).
It reads the bot's settings ([docs/configuration.md](docs/configuration.md)) less the bot's own, and `MCP_SERVICE_TOKENS`, `MCP_SERVICE_BIND`, `MCP_SERVICE_ALLOWED_HOSTS`:
```
set -a; . <(sudo cat /etc/ecobot.env); set +a          # the settings, as the bot reads them
MCP_SERVICE_TOKENS=$(openssl rand -hex 16) python -m ecobot_service      # http://127.0.0.1:8765/mcp
sudo docker compose --profile service up -d ecobot-service           # in Docker (compose.yaml), on the host's port 8765
```
Only one process runs the service on a data folder: it holds `service.lock` there, so `python -m ecobot_service` refuses to start beside
a bot that runs the service itself (`WEATHER_MCP_URL` unset), and the other way round. Make the bot its client first (below): set
`WEATHER_MCP_URL` and restart the bot, then start the service; the bot waits for it and connects when it is up.
Claude Code connects over HTTP: `claude mcp add --transport http ecobot http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"`.
A client that shows pictures can have the charts as images: `http://127.0.0.1:8765/mcp?charts=png` (or, per call, request `_meta`
`chart_image: "png"`; docs 3.7). Pillow (`requirements-charts.txt`) is imported only then: a service installed from `requirements.txt` alone
answers with the charts' JSON and a `chart_image_note`, and warns once.
To look at it by hand: `npx @modelcontextprotocol/inspector` against the URL with the header `Authorization: Bearer <token>`.

Its tests: `tests/test_mcp_server.py` (the SDK's in-memory transport: every tool listed and answering as the ServiceTool does, each resource,
the alerts' template, the prompts, the publishing, the tokens; the chart images asked per call and, under uvicorn, by the URL's
`?charts=png`, without Pillow, and that importing the server imports no Pillow) and `tests/test_mcp_listen.py` (the real app under uvicorn
on a free localhost port: a client that listens loses its listen when the service stops and reads what it missed when it is back; the
token and Host checks over HTTP; `python -m ecobot_service` with the fake sources, a tool call through
a proxy's Host name).

### The bot as the service's client
With `WEATHER_MCP_URL` (and `WEATHER_MCP_TOKEN`, one of the service's `MCP_SERVICE_TOKENS`) set, the bot runs no sources, syncers or alert rules of
its own and reaches the service over MCP (`lib/weather.RemoteWeather`, [docs/mcp-service.md](docs/mcp-service.md) 3.8): one client pinned
to protocol 2026-07-28, kept by one supervisor task (`lib/upstream.Upstream`), which discovers by hand for the instructions and listens to
`ecobot://alerts`, `ecobot://station` and `ecobot://forecast`; when the connection fails or the listen is lost it builds a new client after
a backoff, listens again and reads the alerts after the last id it handled. Unset, the bot runs the service in-process as before
(`lib/weather.LocalWeather`). Both are one interface, `lib/weather.py`, which everything the bot asks of the service goes through:
the model's tools and instructions, its shortcuts, report, gauges, charts drawn ahead and period buttons, Help and `/usage`, the alerts
(lib/alerts/notify.AlertDelivery reads either log) and the irrigation check's rain and forecast (`ServiceRain`, `ServiceForecast`).
The charts the bot's own code asks for (charts drawn ahead, the period buttons, the alerts' pictures and their 🔄) come drawn by the
service (`chart_image`: the remote one sends its PNG, `chart_image: "png"` in the request's `_meta`, in the bot's own theme, `chart_dark`;
in-process, the same `lib/charts.render_data`); a question's charts (the model's answer, or a sentence answered in code) and the dashboard
(`lib/gauges.py`) are still drawn by the bot.
```
set -a; . <(sudo cat /etc/ecobot.env); set +a
MCP_SERVICE_TOKENS=$TOKEN python -m ecobot_service &                                     # the service, with the sources' settings
WEATHER_MCP_URL=http://127.0.0.1:8765/mcp WEATHER_MCP_TOKEN=$TOKEN python ecobot.py  # the bot, its client
```
In Docker (compose.yaml): in /etc/ecobot.env set `MCP_SERVICE_TOKENS=<token>`, `WEATHER_MCP_URL=http://ecobot-service:8765/mcp`,
`WEATHER_MCP_TOKEN=<token>` and `MCP_SERVICE_ALLOWED_HOSTS=127.0.0.1,localhost,ecobot-service`; then recreate the bot first
(`sudo docker compose up -d --force-recreate ecobot`, which frees the data folder) and start the service
(`sudo docker compose --profile service up -d ecobot-service`). Its tests: `tests/test_bot_remote.py`, the bot against the real server (in memory: a
question answered with the remote tools and the instructions from discover, a chart from a remote result with its buttons, the service's PNG of a chart byte for byte the bot's own, an alert through
the listen, the irrigation check's inputs; under uvicorn: a dropped connection rebuilt and the alerts after the last id read once, the
service down, `ecobot.main()` as a client).

## GitHub Actions (`.github/workflows/ci.yml`, and `docker.yml` for `image`)
| Job | When | What |
|---|---|---|
| `test` | every push to `main` and every pull request | the exact versions in `requirements.txt` and `requirements-charts.txt`: pytest (chart pictures included) and the offline evals. The gate. |
| `newest` | Sundays 21:17 UTC, or the Run workflow button | the tests with the newest Pillow. Advisory. |
| `live-evals` | Sundays 21:17 UTC, or the Run workflow button | asks the real model the six questions in `tests/evals/weekly.txt` as the bot asks it (`python scripts/eval_prompts.py --live`: its prompt and the service's ten tools) and checks which tool it calls against `tests/evals/service_cases.json`; a failure is asked once more before it is reported. Advisory. The button's "all cases" box asks all 94. Skipped until the secrets below exist. |
| `image` | pushes that change `Dockerfile`, `.dockerignore`, `requirements.txt` or `requirements-charts.txt` (any branch), or the Run workflow button | builds the Docker image, checks the bot starts in it and names the commit, draws the sample charts in it (they must be in Inter and match `tests/charts`); the image's size goes in the run's summary. |

Repository secrets (Settings, Secrets and variables, Actions, New repository secret):
- `OPENROUTER_API_KEY`: for `live-evals`. `OPENROUTER_MODEL` is optional (default `deepseek/deepseek-v4.1-flash-1017`): add it only if production runs another model.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: optional; failures of `newest` and `live-evals`, and of `test` on `main`, are then sent to Telegram.

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for the Python packages and the actions. `Pillow` comes in a group of its own, because the chart pictures depend on it.

## Chart pictures
Ten fixed sample charts and dashboards (`tests/chart_samples.py`, made-up data, no clock) are drawn and compared with reference images in `tests/charts/` (`tests/test_chart_images.py`, marker `golden`). They are drawn with Pillow's own font (so they match on any machine; it lacks a few characters, such as ₂ and µ, shown as boxes there only), shrunk to 40% and cut to 64 colours, so each is 7 to 46 KB (about 220 KB in all). A chart passes when under 0.1% of its pixels differ noticeably; a failure leaves the new picture and a red-on-grey picture of the difference in `chart-diffs/` (CI uploads it as the `chart-diffs` artifact). After an intended change to how charts look, run `python scripts/update_charts.py` (or name charts: `... wind air`) and commit the changed images: GitHub shows old and new side by side in the pull request. `Pillow` is pinned in `requirements-charts.txt` because the pictures depend on it (charts and the dashboard are drawn with it, `lib/draw.py`; the service needs it only for its chart images, so it is a file of its own); a Dependabot update whose chart test fails is the cue to redo the images.
