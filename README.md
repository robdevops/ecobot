# envirobot

A Telegram bot for a personal Ecowitt weather station and AirGradient air-quality sensor.
Ask it about the weather or air quality (text or charts); it also sends silent alerts to every
chat it is in: rain starting/stopping, rain likely soon, indoor/outdoor temperatures crossing,
and unhealthy air (with when it is safe again). `/alerts off` mutes a chat.

## Run

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python envirobot.py          # with the variables below in the environment
```
Under systemd see `envirobot.service` (it loads the environment file).

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `XAI_API_KEY` | required |
| `XAI_MODEL` (`grok-4.3`), `XAI_BASE_URL` (`https://api.x.ai/v1`) | model |
| `TZ` | e.g. `Australia/Melbourne` |
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY` | enables Ecowitt |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID` | enables AirGradient |
| `AIRGRADIENT_DASHBOARD_URL` | optional "live chart" link under air-quality replies and alerts |

## Layout

```
envirobot.py            wiring and lifecycle
lib/config.py           settings
lib/bot.py              Telegram handlers and replies
lib/llm.py, prompt.py   tool-calling loop; system prompt
lib/intent.py           reasoning effort, "needs data?", fast path
lib/tools.py, warm.py   tool registry; keep-warm helper shared by both sources
lib/ecowitt/            api, store (SQLite + memory), history, days (rank/count days), station, nightly archive
lib/airgradient/        metrics, store (SQLite), source
lib/alerts/             notify (chats, silent send), weather, air
lib/charts.py           chart renderer (shared theme)
tests/                  pytest, against fake Ecowitt/AirGradient/Telegram
scripts/                cache_status.py (is everything cached?), benchmark_history.py (30-minute vs daily),
                        show_request.py (exactly what is sent to the model for a question),
                        check_rain.py (are rain totals trustworthy at each resolution?)
```

Both sources have the same shape: `start()`, `tools`, `warm()`, `poke()`, `close()`.

## How it stays fast and cheap

- The model only reasons for predictions ("will it rain?") and "describe it" questions.
- Simple highs/lows, chart and "air quality now" questions are fetched by the bot first, so the
  model is called once, at the end, just to word the answer.
- Recent readings are prefetched when a question arrives and refreshed every 4 minutes.
- The whole history of both sensors is kept in SQLite (`ecowitt_cache.sqlite`,
  `airgradient_cache.sqlite`): a background archive copies every Ecowitt cycle (5-minute, 30-minute,
  4-hour, daily) before Ecowitt expires it, and backfills AirGradient day by day to where the sensor's
  data starts. Questions only go to the APIs for data the cache is missing.
