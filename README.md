# ecobot

A Telegram bot for a personal Ecowitt weather station and AirGradient air-quality sensor.
Ask it about the weather or air quality (text or charts); it also sends silent alerts to every
chat it is in: rain starting/stopping, rain likely soon, gusts over 40 km/h, UV index of 9 or more, indoor/outdoor temperatures crossing,
and unhealthy air (with when it is safe again). Every alert carries Subscribe / Unsubscribe buttons that expand into the alert types (rain, rain predicted, gusts, UV, temperature crossing, air quality, pollen & asthma, forecast changes): Subscribe lists the types that are off, Unsubscribe the ones that are on; `/alerts` opens the same settings (and `/alerts on|off` still subscribes or unsubscribes every type). In a group only admins can change them (`lib/alerts/menu.py`).

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
| `POLLEN=on` | optional: Melbourne grass pollen and thunderstorm asthma risk (scraped from melbournepollen.com.au) in the report, a `pollen_asthma` tool and an alert when High or Extreme. Off by default; `POLLEN_DISTRICT` picks the district (default `Central`, never printed) |
| `PLACE` | the name shown after the Pollen & asthma and Forecast headings in the report (default `Melbourne`) |
| `FORECAST=on` | optional: the Open-Meteo daily forecast in the report and a `weather_forecast` tool. Off by default; `FORECAST_LAT` / `FORECAST_LON` set the location, else the weather station's own is used |
| `RAIN_STOP_MINUTES` | optional: how long it must stay dry before "the rain has stopped" is sent (default 60, from 5 to 150) |
| `RAIN_QUIET_HOURS` | optional: local hours with no rain alerts (started, stopped, predicted), as `0-6` (the default; `22-6` wraps midnight; `off` for none). Rain that fell in them is summed up in one message once they end |

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
lib/alerts/             notify (chats, silent send), weather, air, pollen
lib/pollen/, forecast/  the optional website sources: pollen + thunderstorm asthma, Open-Meteo forecast (fetched only 6 am-6 pm, see warm.py SYNC_HOURS)
tests/                  pytest, against fake Ecowitt/AirGradient/Telegram; tests/evals holds the routing cases
scripts/                ecowitt_metrics.py (which metrics the station reports), cache_status.py (is everything cached?), eval_prompts.py, show_request.py (exactly what is
                        sent to the model for a question), check_rain.py (are rain totals trustworthy?),
                        dump_specs.py (draw the charts from the caches, offline, to compare before/after a change)
```

A design for tappable commands and buttons is in `TELEGRAM_UX.md` (not built yet).

Both sources have the same shape: `start()`, `tools`, `warm()`, `poke()`, `close()`.

## The pollen and forecast sources
Both are off by default. They are websites, not APIs (the pollen page is parsed), so they are fetched gently: only between 6 am and 6 pm local time, pollen every 30 minutes with a conditional request (a 304 costs the site almost nothing) and the forecast every 15 minutes (a failure is logged), and questions use the last fetch. The report never fetches them: it always uses the cache. When a forecast day that was sent to a chat (in a report or an answer) is later revised substantially (rain to dry or dry to rain, or the highest temperature more than 2 degrees different), that chat is told, once per revision (`lib/alerts/forecast.py`). What they returned is kept in `conditions_cache.sqlite` (one row per distinct result), so a restart, even at night, starts from the last fetch. One fetch is made at start (or at the first question) when nothing is cached, even at night. `python scripts/conditions.py --lat .. --lon ..` fetches both once and prints what the report would show: run it before switching them on. Grass pollen and the thunderstorm asthma forecast both run October to December, so the pollen source only fetches, shows (in the report or an answer) and alerts in those months; outside them it makes no requests (not even at start), the report has no Pollen & asthma block, and a pollen question is told they only run October to December.

## How it stays fast and cheap

- Private chats also get a persistent button keyboard (`lib/templates.py`): tapping one sends its label as a message (a 3x3 grid: Report, Rain chart 7d, Humidity chart 7d; Temperature chart 7d/30d/90d; Air quality 7d/30d (PM1, PM2.5 and PM10 together), Capabilities & alerts = what the bot can do, then the alert settings). `/keyboard` shows it, `/keyboard off` hides it. When the buttons change, the bot says "Buttons updated." with the new keyboard to each private chat at its next start, and any chat missed gets it with its next reply (the bot remembers which version each chat has; a chat that hid it keeps it hidden). Typed "weather now" shows every reading the station has. A chart's text is cut to Telegram's 1024-character caption so it goes as one message.
- In private chats the bot shows Telegram's "Thinking..." draft (sendMessageDraft, re-sent every 20 s, as drafts expire after 30) and streams the answer into it before sending the real message; groups show "typing...". A "message generation stopped" update is only logged.
- The model only reasons for predictions ("will it rain?") and "describe it" questions, or when asked to think, try, reason, predict, estimate, grind or whirl.
- Simple highs/lows, chart and "air quality now" questions are fetched by the bot first, so the
  model is called once, at the end, just to word the answer.
- Recent readings are prefetched when a question arrives and refreshed every 4 minutes.
- The whole history of both sensors is kept in SQLite (`ecowitt_cache.sqlite`,
  `airgradient_cache.sqlite`): a background archive copies every Ecowitt cycle (5-minute, 30-minute,
  4-hour, daily) before Ecowitt expires it, and backfills AirGradient day by day to where the sensor's
  data starts. Questions only go to the APIs for data the cache is missing.

## The report
"report" (and "status", "sitrep") and "weather now" are written in code, with no model call (`lib/report.py`): the bot fetches the tools' results together and lays them out, so they are quick and always the same. Sun, UV and wind are left out when zero (night, calm). The report is: Weather station, Air quality, and Pollen & asthma and Forecast when those sources are on (pollen and forecast always come from the cache).

A plain chart request ("rain chart 7d", "plot temperature and humidity", "air quality 30d", the chart buttons) is also drawn and captioned in code, with no model call (`intent.weather_chart`, `report.chart_caption`): the period, then low and high per reading (the average first if one was asked), the peak and rating for air quality, and for a rain chart the least and most rain and whether more is expected. Anything that asks for correlation, analysis, a forecast, thinking or a description, or compares readings or names a particular date or time, still goes to the model.

Other plain lookups are answered in code too, from the one tool result (`intent.plain_lookup`, `report.lookup`): a single reading now ("how hot is it", "is it raining", "uv"), the air ("how's the air", "pm10"), pollen, the plain forecast ("forecast", "7 day forecast"), the highs, lows or average of a short period ("hottest today", "average temp yesterday", "highest humidity yesterday") and what the bot can do ("what can you do"). Any question with forecast, correlation, thinking, analysis or description words, a comparison, or anything beyond the reading itself goes to the model; so does a lookup whose result can't be used.

`weather_days` can count and rank days by any reading (a day's highest or lowest humidity, pressure, UV, solar, wind, dew point and so on), from the cache, so "how many days was UV 9 or more" is one cheap call (`count_only` returns just the counts, `group_by` month or year adds a count for each). A question that really needs many calls is not refused: the model first sends a one-line heads-up to the chat (a reply starting with a warning sign alongside its tool calls is passed on at once), then does it.

What still goes to the model is kept small: the system prompt has a core that is the same on every call (so the provider can cache it) and adds guidance only for the topics a question touches (`intent.topics`: air, days, correlation, outlook, wind, describing a day, the bot itself); only the tools a question can use are sent (`intent.tools_for`); a chat keeps its last 16 messages; `weather_history` leaves out humidity when only temperature was named (and the reverse) and, for periods over a week, sends only the days of the records unless day by day was asked for. Looking ahead, analysis and describing a day think a little (low), correlation and "think" more (medium), everything else not at all.

The system prompt starts with what never changes (the model provider caches the start of a prompt) and ends with the date ranges and the time.
