# ecobot

A Telegram bot for a personal Ecowitt weather station and AirGradient air-quality sensor.
Ask it about the weather or air quality (text or charts); it also sends silent alerts to every
chat it is in: rain starting/stopping, rain likely soon, gusts over 40 km/h, UV index of 10 or more, indoor/outdoor temperatures crossing (held back for the cooldown, then announced if it still stands),
and unhealthy air (with when it is safe again). Every alert carries Subscribe / Unsubscribe buttons that expand into the alert types (rain, rain predicted, gusts, UV, temperature crossing, air quality, pollen & asthma, forecast changes): Subscribe lists the types that are off, Unsubscribe the ones that are on; `/alerts` opens the same settings (and `/alerts on|off` still subscribes or unsubscribes every type). In a group only admins can change them (`lib/alerts/menu.py`).

## Run

Python 3.13.5 (see `.python-version`; the code uses 3.12 and 3.13 features, so older Pythons will not run it):

```
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ecobot.py          # with the variables below in the environment
```
Tests: `pip install -r requirements-dev.txt && python -m pytest` (and `python scripts/eval_prompts.py`); GitHub Actions runs both on every push and pull request (`.github/workflows/ci.yml`, Python 3.13). Claude Code sessions build the same Python into `.venv` with uv (`.claude/hooks/session-start.sh`).
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
| `ALERT_COOLDOWN_MINUTES` | optional: the rain and temperature-crossing alerts: how long it must stay dry before "the rain has stopped" is sent, and the least time between alerts of the same kind (default 30, from 5 to 150). A change that comes inside the cooldown is held and announced when it ends, if the state still differs from the last one alerted; if it is back where it was, nothing is sent |
| `RAIN_QUIET_HOURS` | optional: local hours with no rain alerts (started, stopped, predicted), as `0-6` (the default; `22-6` wraps midnight; `off` for none). Rain that fell in them is summed up in one message once they end |
| `CHART_ALL_FEELS_LIKE=on` | optional: "weather all week" (every reading, a panel each, sent with just its title; UV is left out as it has the same shape as solar) also draws the feels-like panel; off by default, as it nearly repeats temperature. Naming either ("plot feels like", "plot solar and uv") always works |
| `CHART_ALL_VPD=on` | optional: "weather all week" also draws the vapour pressure deficit panel; off by default, as it is temperature and humidity combined (the station reports it for the outdoor sensor only). Naming it ("plot vpd") always works |

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
Both are off by default. They are websites, not APIs (the pollen page is parsed), so they are fetched gently: only between 6 am and 6 pm local time, pollen every 30 minutes with a conditional request (a 304 costs the site almost nothing) and the forecast every 15 minutes (a failure is logged), and questions use the last fetch. The forecast's rain figure is the Bureau-style "chance of at least 1 mm": the share of Open-Meteo's ECMWF ensemble members (51) that put 1 mm or more on the day, to the nearest 5% (a second request to `ensemble-api.open-meteo.com`; if it is down, Open-Meteo's own figure, the chance of more than 0.1 mm in the wettest hour, is shown as "chance of rain"). The report never fetches them: it always uses the cache. When a forecast day that was sent to a chat (in a report or an answer) is later revised substantially (rain to dry or dry to rain, or the highest temperature more than 2 degrees different), that chat is told, once per revision (`lib/alerts/forecast.py`). What they returned is kept in `conditions_cache.sqlite` (one row per distinct result), so a restart, even at night, starts from the last fetch. One fetch is made at start (or at the first question) when nothing is cached, even at night. `python scripts/conditions.py --lat .. --lon ..` fetches both once and prints what the report would show: run it before switching them on. Grass pollen and the thunderstorm asthma forecast both run October to December, so the pollen source only fetches, shows (in the report or an answer) and alerts in those months; outside them it makes no requests (not even at start), the report has no Pollen & asthma block, and a pollen question is told they only run October to December.

## How it stays fast and cheap

- Private chats also get a persistent button keyboard (`lib/templates.py`): tapping one sends its label as a message (a 3x3 grid: Help & Alerts = what the bot can do, then the alert settings, Rain, Status; Weather = every reading, a panel each, Temperature, Humidity; Air Quality = every air metric, Particulates = PM1, PM2.5 and PM10 on one chart, Wind; every chart button is the last 7 days). `/keyboard` shows it, `/keyboard off` hides it. When the buttons change, the bot says "Buttons updated." with the new keyboard to each private chat at its next start, and any chat missed gets it with its next reply (the bot remembers which version each chat has; a chat that hid it keeps it hidden). Typed "weather now" shows every reading the station has. Weather lines of 31 days or fewer are drawn with no shaded min–max range (except wind's gusts; AirGradient charts keep theirs); longer periods are daily with the range. Pressure, CO₂, VOC, NOx, solar, wind and VPD also show their latest value beside the line (not when it is zero). A single-panel chart's caption is one line ("Temperature, since Tue 7 Jul 2026" when the period runs up to today, else "Temperature, Wed 24 – Tue 30 Sep 2026"); a chart of several panels has its title and then its time range on a second line; any answer text is a message of its own, sent just before the chart. A single chart carries period buttons under it (Week, Month, Quarter and Year, the chart's own marked with ●; pressing it does nothing): pressing one redraws that chart in place for the new period (`lib/periods.py`, `Bot.on_period_button`). The bot keeps the question behind its last 300 charts in its state file, so the buttons keep working across restarts; an older chart answers "out of date, please ask again". A reply with period buttons doesn't carry the persistent keyboard; that follows on the next reply. A group of several charts has no buttons. Charts drawn in the last 10 minutes are kept (the newest 60), so toggling between periods brings a chart back instantly without a new question. Any reading or "weather" with a period of two days or more ("humidity 7d", "weather 30d", "particulates 7d") is a chart, sent with no text.
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

A plain chart request ("rain chart 7d", "plot temperature and humidity", "air quality 30d", the chart buttons) is also drawn and written up in code, with no model call (the chart goes out with just its title as the caption; the write-up is kept in the chat's memory, not sent) (`intent.weather_chart`, `report.chart_caption`): the period, then low and high per reading (the average first if one was asked), the peak and rating for air quality, and for a rain chart the least and most rain and whether more is expected. Anything that asks for correlation, analysis, a forecast, thinking or a description, or compares readings or names a particular date or time, still goes to the model.

Other plain lookups are answered in code too, from the one tool result (`intent.plain_lookup`, `report.lookup`): a single reading now ("how hot is it", "is it raining", "uv"), the air ("how's the air", "pm10"), pollen, the plain forecast ("forecast", "7 day forecast"), the highs, lows or average of a short period ("hottest today", "average temp yesterday", "highest humidity yesterday") and what the bot can do ("what can you do"). Any question with forecast, correlation, thinking, analysis or description words, a comparison, or anything beyond the reading itself goes to the model; so does a lookup whose result can't be used.

`weather_days` can count and rank days by any reading (a day's highest or lowest humidity, pressure, UV, solar, wind, dew point and so on), from the cache, so "how many days was UV 9 or more" is one cheap call (`count_only` returns just the counts; `group_by` month or year adds a count for each and draws them as a bar chart, a number above every bar; `stat` and `of` give a total, average, highest or lowest of any reading per month or year the same way, e.g. rain per month or the hottest day each year). A plain "rain by month for 2 years" or "average temperature per year" (one reading, a figure for each month or year) is worked out in code the same way (`weather_days`/`air_days` with `stat`, `of`, `group_by`): one bar for each month or year, a one-line caption with the figure for the whole period. A rain chart reaching back past the 30-minute readings (about a year) uses the daily records for the older days, and one asked for by month or year is monthly or yearly bars, even if the model answers it. A plain "days over UVI 10 by year" or "count days over PM2.5 of 90 per year" (one reading against one limit, optionally per year or month, over the whole record unless a period is named) is worked out in code without the model, so it always gives the same bar chart; if the model is asked instead and leaves out the by-year or by-month grouping the words ask for, it is added. `air_days` does the same for the air sensor's metrics (days PM2.5 passed 25, the worst day each month), from the stored readings. A question that pulls in a lot of data is not refused: when the tool results of one question pass twice what a 500-point query returns (about 10,000 tokens, roughly a cent and a quarter of input at Grok 4.3's price, and it rides on every later step), the bot sends a one-line heads-up to the chat before the model is sent them, then carries on without waiting for a reply (`lib/timeutil.py` `WARN_TOKENS`).

What still goes to the model is kept small (and, for "will it rain?" and a described today or yesterday, the readings it would fetch first are fetched ahead of it, so it answers in one call; each chat's calls carry one `x-grok-conv-id` so the provider's prompt cache is reused; a chat's older answers are cut to their first lines, the last one kept whole; the `weather_days`/`air_days` definitions list their fields once): the system prompt has a core that is the same on every call (so the provider can cache it) and adds guidance only for the topics a question touches (`intent.topics`: air, days, correlation, outlook, wind, describing a day, the bot itself); every call sends the same tool list in the same order (so the provider can cache the tools and the core prompt together; a changing list cost cache hits); a chat keeps its last 16 messages; `weather_history` leaves out humidity when only temperature was named (and the reverse) and, for periods over a week, sends only the days of the records unless day by day was asked for. Looking ahead, analysis and describing a day think a little (low), correlation and "think" more (medium), everything else not at all.

The system prompt starts with what never changes (the model provider caches the start of a prompt) and ends with the date ranges and the time.
