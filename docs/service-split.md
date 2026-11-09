# Service split plan

The plan for moving the readings file and everything that fills it out of the bot, into a service on its own host that any MCP client
can ask. The bot keeps Telegram, the model, the prompt, the shortcuts, the alerts and the drawing; the service keeps the devices, the
archive and the answers. This is a design and a sequence of steps, each shipped on its own, in the manner of
[storage-plan.md](storage-plan.md).

Decided before this plan: the bot draws the charts (the service sends their data); new live readings are pushed to the bot over MCP;
the two run on separate hosts; the model is offered the service's MCP tools as they are.

## 1. Why

- The archive, the live poll and the backfills run only while the bot runs: a bot restart, a crash or a Telegram outage leaves holes,
  and a slow chart holds up the 5-second poll. As a service, the data keeps coming whatever the bot is doing.
- The data can be asked of by Claude Desktop, Claude Code or any MCP client, with the same answers the bot gives: today only the bot
  can ask, and other assistants get brendo/ecowitt-mcp's raw cloud JSON.
- The service can live next to Home Assistant and the AirGradient on the LAN; the bot can run anywhere Telegram reaches.
- It forces the tools' hidden inputs into the open (section 2.3), which the tests and the evals want anyway.

## 2. What is there now

From an inventory of the code (the paths below are where to look):

### 2.1 Who asks for data

| Asker | How | Lives in |
|---|---|---|
| The model | 12 tools: `weather_now`, `weather_history`, `weather_days`, `weather_link`, `air_quality`, `air_days`, `plot_chart`, `air_link`, `air_scan`, `readings`, `weather_forecast`, `pollen_asthma` | `lib/ecowitt`, `lib/airgradient`, `lib/compose.py`, `lib/history.py`, `lib/forecast`, `lib/pollen` |
| Shortcuts in code | `intent.fast_call`, `report_calls`, `model_prefetch`: the same tools, through `Tools.call_all` | `lib/intent.py`, `lib/bot.py` (`_chart_in_code`, `_lookup_in_code`, `_written_in_code`) |
| Report | `weather_now`, `air_quality`, `pollen_asthma{cached}`, `weather_forecast{cached}`; parses their JSON | `lib/report.py` |
| Dashboard and status gauges | `Ecowitt.dashboard()` directly: the raw live reading, plus the year's day rows | `lib/ecowitt/dashboard.py`, `lib/gauges.py` |
| Period buttons, charts drawn ahead | the shortcut again, with new dates; every 5 minutes for the buttons' sentences and recent alerts | `bot.on_period_button`, `bot.draw_buttons_ahead` |
| Weather alerts | `station.recent(3)` (5-minute rows, may fetch), `live_rain()` (may fetch, writes bands), a 7-day 30-minute read at start | `lib/alerts/weather.py` |
| Particulates alert | `air.latest()` every 30 minutes | `lib/alerts/particulates.py` |
| Forecast, pollen alerts | `forecast.days`, `pollen.current()` in memory, after each refresh (`Warmer.after`) | `lib/alerts/forecast.py`, `pollen_and_asthma.py` |
| Irrigation | `station.recent(24)` and the forecast, once a day | `lib/alerts/irrigation.py` |
| Alert charts, their 🔄 | `source.tools[...].handler` called directly, then `turn.charts[0]` | `lib/alerts/pictures.py` |

### 2.2 What fills the file

The station's warm (60 s: 5-minute and 30-minute readings, 4-hour and daily records), the Home Assistant live poll (5 s; minutes, live
bands), the nightly archive, the Home Assistant range import (15 min), the AirGradient's warm (1–4 min), import and backfill, and the
pollen and forecast refreshes (30 and 15 min). Questions fetch too: `weather_history` and the air tools ask Ecowitt or the AirGradient
cloud for what the file lacks, inline. All of it shares one `Storage`, one Home Assistant client, the in-memory `Recent` locks and the
live reading the poll keeps.

### 2.3 What makes it hard to split

1. **The `Turn` is a second, hidden set of arguments.** "Temperature chart 1d" and "Humidity chart 1d" send the same `weather_history`
   arguments; the reading charted comes from `turn.chart_field`. Tools read `chart_asked`, `chart_field`, `chart_fields`, `two_axes`,
   `average_asked`, `per_day`, `readings`, `text`, `air_named` and `ahead`, and write `charts`, `times`, `forecast_shown` and
   `air_extra_used`.
2. **Charts come back on the side.** Every chart tool appends a `specs.Chart` to `turn.charts`; the rain caption reads the chart's bars.
3. **In-process hooks.** `Warmer.after` and `on_live` callbacks drive the weather, forecast and pollen alerts.
4. **The station decides the time zone** (`date_zone_id`) after the file opens, and the bot's whole config is rebuilt around it.
5. **Reads that write.** `weather_now` and `live_rain()` fetch the live reading and record its bands; with the cloud only, the minute
   alert check is what drives that fetch.
6. **Per-question schemas.** `Tool.asked` swaps in the air sensor's own temperature and humidity; `readings` builds its metric list from
   the file.
7. **Context-variable chart sources** (`CHART_SOURCE`), and alert charts that skip `Tools.call` and so have none.

## 3. Design

### 3.1 The two programs

| Service (`ecobot_service.py`, host A, near Home Assistant) | Bot (`ecobot.py`, host B) |
|---|---|
| `lib/storage`, `lib/ecowitt`, `lib/airgradient`, `lib/forecast`, `lib/pollen`, `lib/snapshots.py`, `lib/warm.py`, `lib/history.py`, `lib/compose.py`, `lib/daytable.py`, `lib/analysis`, `lib/lines.py`, `lib/panels.py`, `lib/rain.py` | `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`, `lib/report.py`, `lib/templates.py`, `lib/buttons.py`, `lib/periods.py`, `lib/charts.py`, `lib/draw.py`, `lib/gauges.py`, `lib/alerts`, `lib/irrigation` |
| builds chart data (`specs.Chart`) next to the reads that choose its resolution | draws it (Pillow, fonts, dark mode) |
| `lib/mcp_server.py`: the tools and resources below | `lib/mcp_client.py`: one connection, reconnects, listens |

Shared, imported by both: `lib/specs.py` (with JSON in and out, 3.6), `lib/series.py`, `lib/timeutil.py`, `lib/captions.py` (hint
texts), and the thresholds the prompt and the alerts both name (`GUST_ALERT_KMH`, `UVI_ALERT`, `TEMP_CHANGE_C`). One repository and one
image, two commands. A test reads every bot module's imports and fails if one reaches into the service's packages (`lib.storage`,
`lib.ecowitt.fetch` …): that boundary is what keeps the split real.

`lib/ecowitt/dashboard.py`'s `sheet()` (the gauges' layout and colours) is drawing, not data: it moves to the bot beside `gauges.py`.

### 3.2 The interface, in one rule

**Tools are for the model; resources are for the bot.** A tool answers a question in words and figures the model can read (what the
twelve tools do today). A resource is raw data an application reads: the live reading the gauges and the alerts need, the rows an alert
looks back over, a chart's points. Other MCP clients see the same split: an assistant asks the tools; an app can subscribe to the
resources.

Every tool and resource follows the same conventions:
- **Metrics** are `sensor.reading`: `outdoor.temperature`, `indoor.humidity`, `pressure.relative`, `wind.wind_gust`, `rainfall.rain`
  (mm that fell), `solar_and_uvi.uvi`, `air.pm2_5`. A bare reading means the sensors that have it: `temperature` is outdoor and indoor,
  `pm2_5` is the air sensor's. The list comes from what the file holds (as `readings` does now).
- **Times** are the station's local time, `YYYY-MM-DD HH:MM` (or `YYYY-MM-DD` for whole days); a time with an offset is moved into
  local time. The station's zone is in `ecobot://station`.
- **Periods** are `start` and `end`; `end` defaults to now, and nothing reaches past it.
- **Results** are JSON with ready-made wording where the model needs it (`low_when`, ratings, emoji), notes it must pass on (`note_*`,
  `missing`), and `error` with how to correct the call. Every tool is read-only to the caller (`readOnlyHint`); some fetch what the file
  lacks before they answer (`openWorldHint`).

### 3.3 Tools (model-facing)

Nine tools in place of twelve: one per kind of question, each over both sensors where both have the reading.

| Tool | Answers | Replaces | Arguments |
|---|---|---|---|
| `current` | "how is it now", one reading now, the air now | `weather_now`, `air_quality` (no dates) | `sensors` (default outdoor, indoor) |
| `summary` | a period's highs, lows and averages with when; daily or monthly breakdown; wind direction; rain total; air ratings and AQI; a chart | `weather_history`, `air_quality` (dates) | `metrics`, `start`, `end`, `average`, `breakdown` (auto, day, month), `chart`, `layout` (auto, stack, two_axes), `rain_by` (auto, month, year), `source` |
| `readings` | values at an interval, each with its time | `readings` | `metric`, `start`, `end`, `interval` |
| `days` | find, rank and count days; a figure per month or year; holidays and weekends | `weather_days`, `air_days` (one field list: `temp_max`, `rain`, `pm2_5_max` …) | as today's day tools |
| `relate` | does one reading go with another (rain with pressure, humidity, wind; air with rain) | `weather_link`, `air_link` | `metric`, `with` (`rain`), `start`, `end`, `chart` |
| `scan` | which readings go with which, every pair | `air_scan` | `metric` (or all), `start`, `end`, `chart` |
| `chart` | any readings from both sensors on one chart, a panel each or on shared axes | `plot_chart` | `panels[{metric, style}]`, `start`, `end`, `two_axes`, `same_axis`, `invert_second_axis`, `source` |
| `forecast` | today and the days ahead | `weather_forecast` | `days` |
| `pollen` | grass pollen and thunderstorm asthma | `pollen_asthma` | none |

What each hidden `Turn` input becomes:

| Today (`Turn`) | Over MCP |
|---|---|
| `chart_field`, `chart_fields`, `readings` (which readings the words name) | `summary.metrics`, in order; `layout` for how they share the chart |
| `two_axes` | `layout: "two_axes"` (`chart.two_axes`) |
| `average_asked` | `average` |
| `per_day` | `breakdown: "day"` |
| `chart_asked` | `chart` |
| `text` (rain bars by month; `only` holidays; grouping) | `rain_by`; `days.only` and `days.group_by`, which the bot drops or fills from the words before it forwards the call (3.8) |
| `air_named` (the air reading charted when none is named) | `metrics`, filled by the bot from the conversation |
| `ahead` (a chart drawn ahead, nobody waiting) | the request's `_meta.priority: "background"` |
| `charts` (written) | a `chart` link in the result (3.6) |
| `times["data"]` (written) | the result's `_meta.timing` |
| `forecast_shown` (written) | `forecast`'s structured `days`, which the bot remembers |
| `air_extra_used` (written) | known from the arguments (`metrics` names `air.air_temperature`) |
| `Tool.asked` (per-question schema) | the service lists everything; the bot leaves the air sensor's own temperature and humidity out of what it offers the model unless the words name them (3.8) |
| `CHART_SOURCE` | `source` on the chart tools, written into the chart; the service sets it for every call, so alert charts get theirs too |

The old names stay as aliases on the service for one release (each mapped to its new tool and arguments), so the bot and the evals can
move over in their own step (section 5).

### 3.4 Resources (bot-facing)

| URI | Holds | Subscribe | Read by |
|---|---|---|---|
| `ecobot://station` | name, time zone, latitude and longitude, created, the sensors and their metrics, what each source is (HA, cloud, local), retention, `air_link` (the AirGradient dashboard), alert kinds the data supports, the service's start-up lines | on change (rare) | the bot at connect: time zone, capabilities, `/usage`, menus |
| `ecobot://now/weather` | the live reading, raw: `{time, group: {field: {value, unit}}}`, and the glance emoji | each new reading (5 s with HA, 60 s cloud only) | gauges, report, the rain alert |
| `ecobot://now/air` | the latest air reading, its time, `sustained` (the minimum over `ANTISPIKE_REQUIRE_CONSECUTIVE`), ratings and AQI | each new reading | the particulates alert, report |
| `ecobot://rows/weather/{resolution}{?start,end,groups}` | rows `{ts, "group.field": value, …_low, …_high}` (1min, 5min, 30min), fetching what the file lacks | `…/5min` when a 5-minute row settles | the weather alerts (3 hours, a rain spell back, 7 days at 30 minutes), irrigation (24 hours) |
| `ecobot://days/{weather,air}{?start,end,fields}` | day figures `{field: {date: value}}` | no | gauges (the year so far) |
| `ecobot://forecast` | the forecast days, raw, with `fetched_at` and `place` | on each new forecast | the forecast alert, irrigation, report |
| `ecobot://pollen` | grass and asthma levels with their days, `fetched_at`, `in_season` | on each new page | the pollen alert, report |
| `ecobot://chart/{id}` | a chart's data (3.6), kept 15 minutes | no | the bot, after a tool returned a link to it |
| `ecobot://health` | each loop's last success and failure, archive progress, the file's size | no | the bot's log line at connect; troubleshooting |

### 3.5 Pushing new readings

The MCP SDK (2.3, the 2026-07-28 protocol) delivers resource changes on a `subscriptions/listen` stream: each event says "this URI
changed, read it again", with no replay. So:
- The service publishes `ecobot://now/weather` after each new live reading, `ecobot://now/air` after each new air reading,
  `ecobot://rows/weather/5min` when a 5-minute row settles, and `ecobot://forecast`, `ecobot://pollen` and `ecobot://station` when they
  change. Repeats of an unchanged reading are not sent.
- The bot listens on one stream and, for each event, reads the resource and runs what used to hang off the hook: `check_rain` on a new
  live reading, `WeatherMonitor.check` on a new 5-minute row, the forecast and pollen alerts on a new snapshot.
- The alerts also keep their own timers, reading the resource each time: the weather alerts' calm periods and quiet hours, the 7:30am
  pollen start and the 30-minute particulates check depend on the clock, not only on new data. A push makes them prompt; the timer
  makes them sure.
- When the stream drops (service restart, network), the bot reconnects with backoff, listens again and reads every resource it depends
  on: the alerts compare against the state they keep, so a missed event costs latency, never an alert.
- The service polls the live reading itself in every mode (today, with the cloud only, the bot's minute alert check drove that fetch).

### 3.6 Charts

Tools that draw (`summary`, `days` with `group_by`, `relate`, `scan`, `chart`, and the period buttons through them) build the
`specs.Chart` as now, next to the reads that chose its resolution, its buckets and its rose. The spec is plain data (strings, epochs,
floats, lists; `period` as two dates), so:
- `specs.to_json(chart)` and `specs.from_json(data)` with a `version`; `from_json` runs the same validation as building one does.
  Tuples come back as lists, which the renderer already accepts. Non-finite numbers are dropped when the spec is built.
- The tool keeps the chart in `ecobot://chart/{id}` and returns a link to it (`resource_link` content, plus `chart: {uri, hint}` in its
  JSON). The model sees a line, not 100 KB of points; the bot reads the link, rebuilds the spec and draws it. Another MCP client can read
  the JSON and plot it however it likes.
- The rain caption reads the chart's bars from the rebuilt spec, as it does now. `newest` (what the alerts' cache keys on) and
  `caption()` work on the rebuilt spec unchanged.
- The bot never asks for a stored spec again: a period button is a new `summary` call with new dates, as today; only pictures are
  cached (`periods.ImageCache`).

### 3.7 Separate hosts

- **Transport:** MCP Streamable HTTP, served by the SDK's Starlette app under uvicorn, behind TLS (a reverse proxy such as Caddy, or
  uvicorn's own certificate). One endpoint, `https://<service>/mcp`.
- **Auth:** a bearer token checked by the SDK's `TokenVerifier` against `MCP_TOKEN`; the bot sends it from its own `MCP_TOKEN`.
  Other clients get their own token (`MCP_TOKENS=name:token,…`), so one can be revoked alone. Bind to the LAN or a tailnet; nothing
  needs the internet to reach it.
- **Time:** both hosts keep NTP time. The bot takes the station's time zone from `ecobot://station` before it schedules anything, as it
  takes it from the station today; the dates it builds ("the last 5 hours") are local strings in that zone.
- **When the service is down:** the bot still starts and answers ("the weather station isn't reachable right now"), keeps retrying, and
  starts its alerts once it has the station's zone. Calls time out within the question's budget (`TURN_SECONDS`).
- **When the bot is down:** nothing is lost; the service keeps archiving.

### 3.8 The bot's side of a tool call

The bot is the MCP client, so it can shape what it offers and what it forwards:
- **What it offers:** the service's `tools/list`, as it is, except the air sensor's own temperature and humidity left out of the metric
  lists unless the words name them (the prompt cache holds: the list only changes with the words, as now). On `tools/list_changed`
  (a sensor sent a new field) it reads the list again.
- **What it forwards:** each call the model makes, with the corrections the words call for, the ones the tools make from `Turn` today:
  `chart: true` when the words ask for a chart; `days.only` dropped unless the words say holidays or weekends; `days.group_by` from
  "per month"; the air reading the conversation was about when the model named none. These live in one function, `lib/arguments.py`,
  with the tests of what each phrase does.
- **Shortcuts** build the new tools' arguments directly (`intent.fast_call` and the rest), so a shortcut and the model ask the same way.
- **Poke** goes: a tool call is itself the service's cue to refresh what it touches.

### 3.9 Configuration

| Service | Bot |
|---|---|
| `ECOWITT_*`, `HOMEASSISTANT_*`, `AIRGRADIENT_*` (including `AIRGRADIENT_DASHBOARD`), `ANTISPIKE_REQUIRE_CONSECUTIVE`, `POLLEN*`, `FORECAST_*`, `PLACE`, `TZ` (until the station names one), `DATA_DIR`, `MCP_TOKEN(S)`, `MCP_BIND` | `TELEGRAM_*`, `OPENROUTER_*`, `ADMIN_*`, `DARKMODE`, `CHART_ALL_*`, the alert settings, `IRRIGATION_*` / Tuya, `MCP_URL`, `MCP_TOKEN`, `DATA_DIR` (the bot's state file and picture cache only) |

The bot's `bot_state.json` (chats, subscriptions, alert state, chart questions) stays with the bot; the readings file and the
snapshots go with the service.

## 4. What does not change

The readings file, its tables and its day rows; how each tool chooses its resolution and fetches what it lacks; the alert rules and
their state; the charts' look; the shortcuts' choices; Telegram. The split moves where code runs and makes its inputs explicit.

## 5. Steps

Each step ships on its own, with the tests and evals green, and the bot working the whole way.

1. **Explicit arguments.** The tools take the `Turn`'s inputs as arguments (3.3), and the bot fills them (`lib/arguments.py`). Tools
   return their charts (as specs, in the result) instead of appending to `turn.charts`; `forecast` returns its days. Still one process.
   The evals check the new arguments; `Turn` loses every field the tools read.
2. **Chart JSON.** `specs.to_json` and `from_json`, and a round-trip test over every chart the samples and `scripts/dump_specs.py` draw:
   the rebuilt spec draws the same picture (the golden images).
3. **The data interface, in process.** A `Data` protocol with the tools and the resources of section 3, and an in-process
   implementation over today's sources. The bot's direct reads move to it: the gauges (with `sheet()` moved to the bot), the report, the
   alerts (resources and timers in place of `Warmer.after` and `on_live`), irrigation, alert charts. Nothing in the bot holds a source
   object after this step.
4. **The MCP server and client.** `lib/mcp_server.py` serves the `Data` implementation; `lib/mcp_client.py` implements `Data` over MCP,
   with reconnects and the listen stream. The tests run the bot against the server through the SDK's in-memory transport; the contract
   test (`test_contract`) checks the tool schemas and result shapes on the wire. The evals read a checked-in `tools/list`
   (`tests/evals/tools.json`), which a test keeps equal to the server's.
5. **Two programs.** `ecobot_service.py` starts the sources, the loops and the server; `ecobot.py` connects to `MCP_URL`. The import
   boundary test. Config split. One compose file per host, TLS and tokens. The service's scripts (`edit_readings`, `cache_status`,
   `refill_airgradient`, `check_rain`, `bench_storage`) run on the service's host; `show_request` and `report_inputs` become MCP clients;
   `dump_specs` writes JSON on the service and draws on the bot.
6. **The new names.** The prompt, the shortcuts and the evals use the nine tools; the old names go from the service one release later.

## 6. Testing

- The suite splits along the boundary: the service's tests (storage, sources, tools, resources, the server) and the bot's (intent,
  report, alerts, drawing, the client). The bot's tests run against a fake `Data`, or the real server in memory over an archived file.
- A wire test for each tool and resource: the arguments it accepts, the shape it returns, a chart link that reads back as a spec.
- A reconnect test: the stream drops, the bot listens again and reads what it depends on; an alert due during the gap is sent once.
- The live evals (`--live`) run against the service's real tool list.

## 7. Risks

- **Latency.** Each call is a network round trip: tens of milliseconds on a LAN, against a question budget of seconds. Charts drawn
  ahead are a few calls every 5 minutes.
- **The model and the new names.** Renamed tools and arguments can change what the model picks; step 6 is gated on the live evals, and
  the aliases make it reversible.
- **A protocol still settling.** `subscriptions/listen` is the 2026-07-28 revision; the client is pinned to the SDK version the server
  runs, and both upgrade together.
- **Two things to run.** More to deploy and watch; `ecobot://health` and the bot's "unreachable" answers make a failure visible.
