# ecobot

A Telegram bot for a personal Ecowitt weather station and AirGradient air-quality sensor.
Ask it about the weather or air quality (text or charts); it also sends silent alerts to every
chat it is in: rain starting/stopping, rain likely soon, gusts over 40 km/h, UV index of 9 or more, indoor/outdoor temperatures crossing,
and unhealthy air (with when it is safe again). `/alerts off` mutes a chat.

## Run

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ecobot.py          # with the variables below in the environment
```
Under systemd see `ecobot.service` (it loads the environment file; the clone's location is its one `WorkingDirectory` line, the rest is relative).

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
ecobot.py            wiring and lifecycle
lib/config.py           settings
lib/bot.py              Telegram handlers and replies
lib/llm.py, prompt.py   tool-calling loop; system prompt
lib/intent.py           intent.read(text) -> one Reading: effort, needs data, fast path, chart asks, period hints
lib/tools.py, warm.py   tool registry and the per-question Turn; keep-warm helper shared by both sources
lib/series.py           the weather readings a chart can plot (one table)
lib/lines.py            the one rule that turns readings into a chart line (raw, bucketed, daily band, smoothing)
lib/rain.py             rain per slot, spells and bars from the daily total
lib/analysis/           pairs (rain vs a reading, rain vs air quality), scan (air quality vs everything)
lib/compose.py          plot_chart / air_link / air_scan: any series on one time axis
lib/specs.py            what a chart is (typed: Chart > Panel > Line/Bars/Shares), validated when built; stack() titles a stacked chart
lib/panels.py           how a reading becomes a panel (title, unit, colour key, zones): every chart producer uses it
lib/charts.py           the one renderer: panels on a shared time axis, rain behind the lines, one colour per reading
lib/captions.py         when a chart is drawn, and the hint that makes the model's reply its caption
lib/ecowitt/            api, store (SQLite + memory), fetch, extremes, query (one history question), link (weather_link),
                        days (rank/count days), outlook (raining / likely soon), station, nightly archive
lib/airgradient/        metrics, store (SQLite), source
lib/alerts/             notify (chats, silent send), weather, air
tests/                  pytest, against fake Ecowitt/AirGradient/Telegram; tests/evals holds the routing cases
scripts/                ecowitt_metrics.py (which metrics the station reports), cache_status.py (is everything cached?), eval_prompts.py, show_request.py (exactly what is
                        sent to the model for a question), check_rain.py (are rain totals trustworthy?),
                        dump_specs.py (draw the charts from the caches, offline, to compare before/after a change)
```

A design for tappable commands and buttons is in `TELEGRAM_UX.md` (not built yet).

Both sources have the same shape: `start()`, `tools`, `warm()`, `poke()`, `close()`.

## How it stays fast and cheap

- Private chats also get a persistent button keyboard (`lib/templates.py`): tapping one sends its label as a message (a 3x3 grid: Report, Rain chart 7d, Humidity chart 7d; Temperature chart 7d/30d/90d; Air quality 7d/30d (PM1, PM2.5 and PM10 together), Capabilities & alerts = what the bot can do, then the alert settings). `/keyboard` shows it, `/keyboard off` hides it. When the buttons change, the bot says "Buttons updated." with the new keyboard to each private chat at its next start, and any chat missed gets it with its next reply (the bot remembers which version each chat has; a chat that hid it keeps it hidden). Typed "weather now" fetches every reading the station has. A chart's text is cut to Telegram's 1024-character caption so it goes as one message.
- In private chats the bot shows Telegram's "Thinking..." draft (sendMessageDraft, re-sent every 20 s, as drafts expire after 30) and streams the answer into it before sending the real message; groups show "typing...". A "message generation stopped" update is only logged.
- The model only reasons for predictions ("will it rain?") and "describe it" questions, or when asked to think, try, reason, predict, estimate, grind or whirl.
- Simple highs/lows, chart and "air quality now" questions are fetched by the bot first, so the
  model is called once, at the end, just to word the answer.
- Recent readings are prefetched when a question arrives and refreshed every 4 minutes.
- The whole history of both sensors is kept in SQLite (`ecowitt_cache.sqlite`,
  `airgradient_cache.sqlite`): a background archive copies every Ecowitt cycle (5-minute, 30-minute,
  4-hour, daily) before Ecowitt expires it, and backfills AirGradient day by day to where the sensor's
  data starts. Questions only go to the APIs for data the cache is missing.
