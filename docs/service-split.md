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
- It forces the tools' hidden inputs into the open (2.3), which the tests and the evals want anyway.

## 2. What is there now

### 2.1 Who asks for data

| Asker | How | Lives in |
|---|---|---|
| The model | 12 tools: `weather_now`, `weather_history`, `weather_days`, `weather_link`, `air_quality`, `air_days`, `plot_chart`, `air_link`, `air_scan`, `readings`, `weather_forecast`, `pollen_asthma` | `lib/ecowitt`, `lib/airgradient`, `lib/compose.py`, `lib/history.py`, `lib/forecast`, `lib/pollen` |
| Shortcuts in code | `intent.fast_call`, `report_calls`, `model_prefetch`: the same tools through `Tools.call_all`, their JSON parsed by `lib/report.py` | `lib/intent.py`, `lib/bot.py` (`_chart_in_code`, `_lookup_in_code`, `_written_in_code`) |
| Report | `weather_now`, `air_quality`, `pollen_asthma{cached}`, `weather_forecast{cached}` (`cached`: never wait on the network) | `lib/report.py` |
| Dashboard and status gauges | `Ecowitt.dashboard()` directly: the raw live reading, plus the year's day rows | `lib/ecowitt/dashboard.py`, `lib/gauges.py` |
| Period buttons, charts drawn ahead | the shortcut again, with new dates; every 5 minutes for the buttons' sentences and recent alerts | `bot.on_period_button`, `bot.draw_buttons_ahead` |
| Weather alerts | `station.recent(3)` (5-minute rows, may fetch), `live_rain()` (may fetch, writes bands), a 7-day 30-minute read at start; after every 60-second refresh and every new live reading | `lib/alerts/weather.py` |
| Particulates alert | `air.latest()` every 30 minutes (its two-readings rule is set for that pace) | `lib/alerts/particulates.py` |
| Forecast, pollen alerts | `forecast.days`, `pollen.current()` in memory, after each refresh (`Warmer.after`) | `lib/alerts/forecast.py`, `pollen_and_asthma.py` |
| Irrigation | `station.recent(24)` and the forecast, once a day; the valve watchdog every minute | `lib/alerts/irrigation.py`, `lib/irrigation` |
| Alert charts, their 🔄 | `source.tools[...].handler` called directly, then `turn.charts[0]` | `lib/alerts/pictures.py` |
| The prompt, menus, `/usage` | each source's `describe()` line; which sources exist (`by_name`) | `lib/prompt.py`, `lib/templates.py`, `lib/alerts/menu.py` |

### 2.2 What fills the file

The station's warm (60 s: 5-minute and 30-minute readings, 4-hour and daily records), the Home Assistant live poll (5 s; minutes, live
bands), the nightly archive, the Home Assistant range import (15 min), the AirGradient's warm (1–4 min), import and backfill, and the
pollen and forecast refreshes (30 and 15 min). Questions fetch too: `weather_history` and the air tools ask Ecowitt or the AirGradient
cloud for what the file lacks, inline. All of it shares one `Storage`, one Home Assistant client, the in-memory `Recent` locks, the
live reading the poll keeps, and the station's group list (which the archive trims).

### 2.3 What makes it hard to split

1. **The `Turn` is a second, hidden set of arguments.** "Temperature chart 1d" and "Humidity chart 1d" send the same `weather_history`
   arguments; the reading charted comes from `turn.chart_field`. Tools read `chart_asked`, `chart_field`, `chart_fields`, `two_axes`,
   `average_asked`, `per_day`, `readings`, `text`, `air_named` and `ahead`, and write `charts`, `times`, `forecast_shown` and
   `air_extra_used`.
2. **Charts come back on the side.** Every chart tool appends `specs.Chart`s to `turn.charts` (a history can add a wind rose beside its
   chart); the rain caption reads the chart's bars.
3. **In-process hooks.** `Warmer.after` and `on_live` callbacks drive the weather, forecast and pollen alerts.
4. **The station decides the time zone** (`date_zone_id`) after the file opens, and the bot's whole config is rebuilt around it.
5. **Reads that write.** `weather_now` and `live_rain()` fetch the live reading and record its bands; with the cloud only, the minute
   alert check is what drives that fetch.
6. **Per-question schemas.** `Tool.asked` swaps in the air sensor's own temperature and humidity; `readings` builds its metric list from
   the file.
7. **Context-variable chart sources** (`CHART_SOURCE`), and alert charts that skip `Tools.call` and so have none.
8. **Imports across the line.** The bot imports helpers from data packages (`grouping_in`, the rain helpers, `describe_day`,
   `LEVEL_EMOJI`, the air labels, `unit_text`, `glance`, `POINT_BUDGET`, `STATUS`); two data sources import `intent` for `wants()`.

## 3. Design

### 3.1 The two programs

| Service (`ecobot_service.py`, host A, near Home Assistant) | Bot (`ecobot.py`, host B) |
|---|---|
| `lib/storage`, `lib/ecowitt` (less `dashboard.sheet`), `lib/airgradient`, `lib/forecast`, `lib/pollen`, `lib/snapshots.py`, `lib/warm.py`'s `Warmer`, `lib/history.py`, `lib/compose.py`, `lib/daytable.py`, `lib/analysis`, `lib/lines.py`, `lib/panels.py`, `lib/rain.py`, `lib/sources.py` | `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`, `lib/report.py`, `lib/report_menu.py`, `lib/templates.py`, `lib/buttons.py`, `lib/periods.py`, `lib/topicmemory.py`, `lib/entities.py`, `lib/admins.py`, `lib/charts.py`, `lib/draw.py`, `lib/gauges.py` with `sheet()`, `GAUGES` and `STATUS`, `lib/alerts`, `lib/irrigation` |
| builds chart data (`specs.Chart`) next to the reads that choose its resolution | draws it (Pillow, fonts, dark mode) |
| `lib/mcp_server.py`: the tools and resources below | `lib/mcp_client.py`: the connection (3.7) |
| `lib/config.py`'s service fields (3.9) | `lib/config.py`'s bot fields |

**Shared** (`lib/common/`, imported by both, importing neither side): `specs.py` (with JSON in and out, 3.6), `series.py`, `timeutil.py`,
`captions.py` (hint texts), `tools.py`'s `json_result`, `logs.py`, `netenv.py`, `warm.py`'s `every` and `safely`, and the pure helpers
the bot uses today from data packages: the thresholds (`GUST_ALERT_KMH`, `UVI_ALERT`, `TEMP_CHANGE_C`), `glance` (`solar_band`),
`unit_text`, `POINT_BUDGET`, the air metrics' labels and units, `grouping_in`, the rain helpers (`wet_flags`,
`rain_amount`, `duration`), the forecast's `describe_day` and `day_label`, and the pollen `LEVELS` and `LEVEL_EMOJI`.

One repository and one image, two commands. A test reads every module's imports and fails if a bot module reaches a service package or
the reverse: that boundary is what keeps the split real. The sources' `wants()` (which imports `intent`) goes with the poke (3.8).

### 3.2 The interface

**Anything that answers a question calls a tool, whether the model asks or a shortcut does; resources are the state the bot watches.**
A tool answers in words and figures (ready-made wording, notes to pass on); the bot's shortcuts, report, captions and period buttons
parse the same results, so each tool's result shape is a contract with the bot as well as the model, pinned by `test_contract`. A
resource is raw data an application keeps an eye on: the live readings, the rows the alerts look back over, the day figures behind the
gauges, the forecast and pollen pages. Other MCP clients see the same split: an assistant asks the tools; an app reads or subscribes to
the resources.

Conventions for the tools:
- **Metrics** are `sensor.reading`: `outdoor.temperature`, `indoor.humidity`, `pressure.relative`, `wind.wind_speed` (always drawn
  shaded up to `wind.wind_gust`), `rainfall.rain` (mm that fell), `solar_and_uvi.uvi`, `air.pm2_5`. `current` and `summary` also take a
  bare reading, meaning every sensor that has it (`temperature` is outdoor and indoor; `pm2_5` the air sensor's); `readings`, `chart`
  and `relate` take full names only. `days` keeps its day-figure names (`temp_max` is outdoor, `indoor_temp_max`, `rain`, `pm2_5_max`),
  which read better for conditions and are listed in its schema. The lists come from what the file holds.
- **Two kinds of period.** Moments (`current`, `summary`, `readings`): `start` and `end` as `YYYY-MM-DD HH:MM` in the station's local
  time, `end` defaulting to now. Days (`days`, `relate`, `scan`, `chart`): `start` and `end` as `YYYY-MM-DD`, `end` defaulting to and
  capped at yesterday, because today is still settling. A time with an offset is moved into local time. Each tool's default length is in
  3.3. The station's zone is in `ecobot://station`.
- **Results** are JSON: figures with their wording (`low_when`, ratings, emoji), notes to pass on (`note_*`, `missing`, `warning`), and
  `error` with how to correct the call. Every tool is read-only to the caller (`readOnlyHint`); those that fetch what the file lacks
  before answering say so (`openWorldHint`).
- **Request `_meta`**, set by the application, never by the model: `ecobot/charts: true` (this client draws: send chart data, 3.6),
  `priority: "background"` (a chart drawn ahead: its fetches wait their turn; today's `turn.ahead`) and `fetch: false` (answer from
  what is held, never wait on the network; today's `cached`). **Result `_meta`**, for the application, which clients don't hand to the
  model: `ecobot/charts` (the chart data) and `timing` (the seconds spent; today's `turn.times["data"]`).
- **Arguments are the model's.** Every choice a tool makes is an argument the model sets (or a shortcut sets, 3.8); the bot forwards the
  model's calls as they are. The tools read nothing else about the question.

### 3.3 Tools

Nine tools in place of twelve, one per kind of question, each over both sensors where both have the reading.

| Tool | Answers | Replaces | Arguments (default period) |
|---|---|---|---|
| `current` | how it is now; one reading now; the air now | `weather_now`, `air_quality` without dates | `metrics` (default outdoor and indoor) |
| `summary` | a period's highs and lows with when; averages; day or month breakdown; wind direction; rain total; air ratings and AQI; charts | `weather_history`, `air_quality` with dates | `metrics`, `start`, `end` (today so far), `average`, `every_day`, `chart`, `chart_metrics`, `layout`, `daily_means`, `rain_by`, `source` |
| `readings` | values at an interval, each with its time | `readings` | `metric`, `start`, `end`, `interval` |
| `days` | find, rank and count days; a figure per month or year; holidays and weekends | `weather_days`, `air_days` | today's day-tool arguments over one field list (the last 30 days to yesterday) |
| `relate` | rain against one reading: for pressure, humidity or wind, does rain follow a change in it; for PM2.5 or PM10, is the air cleaner after rain | `weather_link`, `air_link` | `metric` (`pressure.relative`, `outdoor.humidity`, `wind.wind_speed`, `air.pm2_5`, `air.pm10`), `start`, `end` (90 days; 60 for air), `chart` |
| `scan` | each air reading against every weather reading: which go together | `air_scan` | `metric` (or all), `start`, `end` (90 days, at most 400), `chart` |
| `chart` | any readings from both sensors on one chart: a panel each, or two on one panel | `plot_chart` | `panels[{metric, style}]`, `start`, `end` (30 days), `layout`, `invert_second_axis`, `source` |
| `forecast` | today and the days ahead | `weather_forecast` | `days` |
| `pollen` | grass pollen and thunderstorm asthma | `pollen_asthma` | none |

**`summary`'s figures and charts** are two inputs, as they are today (`groups` and `chart_field(s)`):
- `metrics` is what to summarise; `chart_metrics` is what to draw, defaulting to the first of `metrics` (temperature when it is there).
- `layout` (shared with `chart`): `auto` draws one panel of `chart_metrics[0]` with a line for each sensor that has it, plus a second
  chart of wind with the compass rose when `wind.wind_direction` is summarised and nothing is stacked; `panels` stacks every
  `chart_metrics` reading, a panel each, on one time axis; `two_axes` puts two on one panel with a scale each; `same_axis` two of one
  unit over each other. `invert_second_axis` modifies `two_axes`.
- `chart` defaults to on for periods of 3 days or more, as now; `daily_means` draws one point a day (the words asked for an average);
  `average` adds means to the figures; `every_day` lists every day of a period up to 31 days (whether the breakdown is by day or by month
  still follows the period's length); `rain_by` (`auto`, `month`, `year`) sets the rain bars.
- A chart mixing station and air readings is `chart`'s, not `summary`'s: `summary` answers it with an error that says so.

**`summary`'s result** is one shape for both sensors:
`{period, series: {"outdoor.temperature": {unit, low, low_time, low_when, low_date, [low_note], high…, [average], [daily|monthly]},
"air.pm2_5": {unit, low…, high…, average, high_rating, average_rating, high_aqi_us, high_band}, "wind.wind_direction": {most_common,
then, steadiness, [daily]}}, [rain_total_mm], [monthly_note], [notes: {sensor: {…}}], [chart_hint], [missing], [warning]}`. The breakdown and
`monthly_note` are the station's; ratings, AQI and the 400-day cap (30 days fetched inline) the air sensor's; `rain_total_mm` whenever
`rainfall.rain` is summarised. `current`'s result is `{time, sensors: {"outdoor": {"temperature": {value, unit, text: "12.3 °C",
[emoji]}}, "air": {"pm2_5": {value, unit, rating, aqi_us, band}}}}`.

**`days` over both sensors:** the fields shown and the default sort come from the sensor of the fields named (an air-only question
shows and ranks by `pm2_5_max`); `days_checked` counts the days on which every sensor named has a day row; each listed day carries each
sensor's source; the result keeps both sensors' notes (`note_daily`, `exact_from`, `note_trace`, `note_averaged`; `note_hourly`,
`note_missing`). A condition mixing the two sensors leaves out the days whose station figures come from Ecowitt's daily records (10am
to 10am, not the air sensor's midnight to midnight) and says how many.

**Charts in a result** (3.6): for a client that asked for them, the chart data in the result's `_meta` (`ecobot/charts`, a list: a
history can add a wind rose beside its chart) and one line for the model, `chart_hint`: that a chart goes with its reply, so the reply
is its caption (today's `CHART_HINT`). The bot draws them in order (up to three a question); a drawn-ahead or alert chart still expects
exactly one.

**What each hidden `Turn` input becomes.** Today the bot reads the question with regexes and the tools use that reading beside the
model's arguments: some fill an argument the model left out, three overrule the model (the reading charted, the readings stacked, a
holiday filter). Each was added for a mistake the production model made (the eval cases `humidity-chart`, `temperature-and-rain`,
`days-over-limit-by-year`, `rain-by-month-two-years`), several of them because the choice was a hidden input or an easily missed
argument. Over MCP each becomes an argument with a plain name, and the model sets it:

| Today (`Turn`) | Over MCP |
|---|---|
| `chart_field`, `chart_fields` (overrule the model) | `summary.chart_metrics`, `layout: "panels"` |
| `readings` (which readings the words name) | `summary.metrics` |
| `two_axes` | `layout: "two_axes"` |
| `average_asked` (daily means on the chart; the model can't ask for that today) | `daily_means` |
| `per_day` | `every_day` |
| `chart_asked` | `chart` |
| `text`: rain by month, grouping (fill), holidays (overrule) | `rain_by`, `days.group_by`, `days.only` |
| `air_named` (the air reading charted when none is named) | `metrics` (the conversation is in the model's context) |
| `ahead` | request `_meta.priority: "background"` |
| `charts`, `times`, written | result `_meta`: `ecobot/charts`, `timing` |
| `forecast_shown`, written | `forecast`'s result has the days shown (`days: [{date, …}]`), which the bot records |
| `air_extra_used`, written | known from the arguments (`air.air_temperature` among the metrics) |
| `Tool.asked` | the service lists every metric; the bot leaves the air sensor's own temperature and humidity out unless the words name them (3.8) |
| `CHART_SOURCE` | `source` on the chart tools, written into the chart; the service sets it on every call, so alert charts get theirs too |

The bot's chart captions follow the arguments of the call that drew the chart (its `chart_metrics`, `daily_means`), so a caption
always matches its chart; the streamed draft is held back once a result brings chart data.

### 3.4 Resources

Raw data keeps the device's own field names (`rainfall.daily`, the running total with its midnight reset; `rainfall.rain_rate`), which
the alerts' rain helpers and the Rain gauge work on; the `sensor.reading` names are the tools'. Dates arrive as ISO strings and the
client turns them back into `date`s (the day figures' keys, the forecast's and pollen's days), as `specs.from_json` does for charts.

| URI | Holds | Pushed | Read by |
|---|---|---|---|
| `ecobot://station` | name, time zone, latitude and longitude, created; the configured sources and each one's prompt line (`describe()`); each source's mode (HA, cloud, local); the sensors and their metrics; retention; `air_link`; the service's start-up lines. Served once the station has named its zone. | on change | the bot at connect: zone, `by_name`, prompt, menus, `/usage` |
| `ecobot://now/weather` | the live reading: `{time, group: {field: {value, unit, time}}}` (each field keeps its own time), and the glance emoji | each new reading (5 s with HA, 60 s cloud only) | the rain alert, the gauges |
| `ecobot://now/air` | the latest air reading, its time, `sustained` (the minimum over `ANTISPIKE_REQUIRE_CONSECUTIVE` readings), ratings and AQI | each new reading | the particulates alert (on its timer) |
| `ecobot://rows/weather/{resolution}{?start,end,groups}` | rows `{ts, "group.field": value, …_low, …_high}` at 1min, 5min or 30min, fetching what the file lacks; a bare read is the last 3 hours | `…/5min` whenever a 5-minute row is written or replaced, provisional ones included | the weather alerts (3 hours; back to a rain spell's start; 7 days at 30 minutes at start), irrigation (24 hours) |
| `ecobot://days/{sensor}{?start,end,fields}` | `sensor` is `weather` or `air`: day figures `{field: {date: value}}`; a bare read is the year so far | no | the gauges |
| `ecobot://forecast` | the forecast days, raw, with `fetched_at` and `place` | each new forecast | the forecast alert, irrigation |
| `ecobot://pollen` | grass and asthma levels with their days, `fetched_at`, `in_season` | each new page | the pollen alert |
| `ecobot://health` | each loop's last success and failure, archive progress, the file's size | no | the bot's log at connect; troubleshooting |

The bot always reads `rows` and `days` with an explicit `start` and `end`; subscriptions are on the bare URIs only.

### 3.5 Pushing new readings

The MCP SDK (2.3, protocol revision 2026-07-28) delivers changes on a `subscriptions/listen` stream: each event says "this URI changed,
read it again", with no replay. List changes travel on the same stream, when asked for.
- **The service** passes an `InMemorySubscriptionBus` to the server and its loops publish to it: `ResourceUpdated(ecobot://now/weather)`
  after each new live reading, `…/now/air` after each new air reading, `…/rows/weather/5min` after each 5-minute row written,
  `…/forecast`, `…/pollen` and `…/station` when they change, and `ToolsListChanged` when a sensor sends a field it never sent (the
  metric lists grow). Repeats of an unchanged reading are not sent. It polls the live reading itself in every mode (today, with the
  cloud only, the bot's minute alert check drove that fetch).
- **The bot** listens once, for those URIs and for tool list changes, and runs what used to hang off the hooks: `check_rain` on a new
  live reading, `WeatherMonitor.check` on a new 5-minute row, the forecast and pollen alerts on a new page, a new tool list on a list
  change.
- **The alerts keep their clocks**, reading the resource each time: the weather alerts every 60 seconds (as after each refresh today:
  calm periods, quiet hours), the pollen alert's 7:30am start, and the particulates alert every 30 minutes and only then (its two-readings
  rule counts readings across checks; a check per reading would confirm a mask alert within minutes). A push makes an alert prompt; the
  clock makes it sure.
- **After a gap** (3.7) the bot listens again and reads every resource it depends on: the alerts compare against the state they keep, so
  a missed event costs latency, never an alert.

Only clients on the 2026-07-28 revision can listen; older ones can still call the tools and read the resources, and poll.

### 3.6 Charts

The tools that draw (`summary`, `days` with `group_by`, `relate`, `scan`, `chart`, and the period buttons through them) build `specs.Chart`
as now, next to the reads that chose its resolution, its buckets and its rose. A spec is plain data (strings, epochs, floats, lists;
`period` as two dates), so:
- `specs.to_json(chart)` and `specs.from_json(data)`, with a `version`; `from_json` runs the same checks as building one. Tuples come back
  as lists, which the renderer already takes. Non-finite numbers are dropped when the spec is built.
- The charts travel in the tool's own result, in its `_meta` (`ecobot/charts`), which MCP clients keep for the application and don't
  hand to the model: one round trip, nothing stored on the service. The model gets only `chart_hint`, the line that tells it a chart
  goes with its reply so the reply is the chart's caption: keep it short, don't describe the chart (today's `CHART_HINT`). Without that
  line a model writes a long answer, or describes or apologises for a chart it doesn't know is there.
- Charts are sent only to a client that asks (request `_meta` `ecobot/charts: true`, which the bot always sends). For any other client
  the tool builds no chart and adds no `chart_hint`, so its model is never told of a chart that won't appear.
- The bot rebuilds each spec as the result arrives (so the streamed draft is held back under a chart, as now) and draws it.
- The rain caption reads the rebuilt chart's bars; `newest` (what the alerts' picture cache keys on) and `caption()` work on it unchanged.
- A period button is a new `summary` call with new dates, as today; only pictures are cached (`periods.ImageCache`).

### 3.7 Separate hosts

- **Server:** the SDK's low-level `Server`, so each tool's input schema is the hand-written JSON the tools have today (with their
  descriptions); the list is built when asked, so it follows the file's metrics. Served as Streamable HTTP (`streamable_http_app`) under
  uvicorn, behind TLS (a reverse proxy such as Caddy, or uvicorn's own certificate), at `https://<service>/mcp`. The app is built with
  `TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS)`: by default it only answers `Host: 127.0.0.1` or `localhost`, and refuses
  the name a proxy or a LAN client uses.
- **Auth:** the service's own `TokenVerifier` (the SDK has only the protocol): a constant-time compare against `MCP_TOKENS`
  (`name:token,…`, one per client, so one can be revoked alone), with `AuthSettings(resource_server_url=None)` so no OAuth metadata is
  published. The bot sends its token in the HTTP client's headers (`create_mcp_http_client(headers={"Authorization": …})`); a bare URL
  sends none. Bind to the LAN or a tailnet; nothing needs the internet to reach it.
- **The bot's connection:** `Client(…, mode="2026-07-28")`, pinning the protocol revision as well as the SDK version (the default mode
  probes, and a proxy's 502 during a restart can land it on the older revision, which cannot listen). One supervisor task enters the
  client and runs the listen loop; questions, alerts and drawing ahead call it from their own tasks. In the SDK a connection failure on
  any request ends the whole client, so: a call that fails with "connection closed" or times out is answered as unreachable; the
  supervisor discards the client when it exits with a transport error, when the listen is lost, or when the listen ends, then builds a
  new one after a backoff, listens again and reads what it depends on. Nothing is sent on a dead client. The HTTP read timeout is about
  45 seconds (three of the server's 15-second pings), so a silently dead link is noticed.
- **Time:** both hosts keep NTP time. The bot takes the station's zone from `ecobot://station` and remembers it in `bot_state.json`;
  until it has one (a first start with the service down), it uses its own `TZ`. When the zone arrives or changes, the bot replaces its
  zone, configures the charts again and tells the irrigation check. The dates it builds ("the last 5 hours") are local strings in that
  zone.
- **When the service is down:** the bot starts and answers ("the weather station isn't reachable right now"), within the question's
  budget (`TURN_SECONDS`). The irrigation watchdog and the valve buttons start at once whatever the service's state (a restart mid-run
  must still close the valve); the irrigation check runs on the forecast it last had and skips the measured rain.
- **When the bot is down:** nothing is lost; the service keeps archiving.

### 3.8 The bot's side of a tool call

The model never reaches the service: it answers the bot with a tool call, the bot makes the call, and the service replies to the bot,
which hands the result back to the model for its reply. Every call passes through the bot both ways, so:

- **What it offers:** the service's tool list, except the air sensor's own temperature and humidity left out of the metric lists unless
  the words name them (the list only changes with the words, so the prompt cache holds, as now); a new list when the service says so.
  This narrows what the model can pick from; it never changes what the model picked.
- **What it forwards:** each call the model makes, as it is. The corrections the tools make from the words today are not carried over:
  with every choice an explicit argument, the model gets a fair chance to make it, and the regexes overruled it even when it was right,
  misfired on wording nobody foresaw ("miles per hour"), and would leave other MCP clients with different answers from the same service.
  The live evals decide (step 3): the cases each correction was added for are run against the nine tools, and a correction comes back
  only for one that still fails, in the bot, filling an argument the model left out, never overruling one it set, each with its case.
- **Shortcuts** build the tools' arguments directly (`intent.fast_call` and the rest), so a shortcut and the model ask the same way; the
  report sends `_meta.fetch: false` as `cached` does now.
- **When the chart is the answer, the model doesn't write the caption.** The bot adds one argument of its own to `summary` and to `days`
  (the tools whose captions code already writes: `report.chart_caption`, and `report.lookup` for a count per month or year):
  `chart_answers_it`, described as "true only when the question is just to see this chart and its figures: nothing else to answer". It
  removes the argument before forwarding, so the service and other clients never see it. When every call of the model's step set it and
  each result brings a chart, the bot draws them, writes the caption in code (the period, then each reading's high and low with when,
  and any note about missing data) and sends them: the turn ends there, with no second call to the model, the one that reads the whole
  result. A step without it, a result with no chart, an error, or a caption the code can't write goes back to the model as usual. The
  model decides, because it reads the question better than a regex: "plot pressure this week" is just the chart; "plot pressure this
  week: is a storm coming?" needs its words. A plain chart request the shortcuts recognise never reaches the model at all, as now. This
  needs nothing from the split: it can ship first, in today's bot, on `weather_history`, `air_quality` and the day tools.
- **The early cue:** today the bot pokes the sources the words concern as soon as a question arrives, so they refresh while the model
  thinks (a forecast question refreshes only that way). The bot sends the same cue at receipt as a request to the service's `warm` tool
  (`sources`), marked for applications (`_meta` `audience: "app"`), which the bot never offers the model. The sources lose `wants()`.

### 3.9 Configuration

| Service | Bot |
|---|---|
| `ECOWITT_*`, `HOMEASSISTANT_*`, `AIRGRADIENT_*` (including `AIRGRADIENT_DASHBOARD`), `ANTISPIKE_REQUIRE_CONSECUTIVE`, `POLLEN*`, `FORECAST_*`, `PLACE`, `TZ` (until the station names one), `DATA_DIR`, `MCP_TOKENS`, `MCP_BIND`, `MCP_ALLOWED_HOSTS` | `TELEGRAM_*`, `OPENROUTER_*`, `ADMIN_*`, `DARKMODE`, `CHART_ALL_*`, the alert settings, `IRRIGATION_*` / Tuya, `MCP_URL`, `MCP_TOKEN`, `TZ` (until the station's zone is known), `DATA_DIR` (the bot's state and picture cache only) |

`bot_state.json` (chats, subscriptions, alert state, chart questions, the station's last zone) stays with the bot; the readings file
and the snapshots go with the service.

## 4. What does not change

The readings file, its tables and its day rows; how each tool chooses its resolution and fetches what it lacks; the alert rules and
their state; the charts' look; the shortcuts' choices; Telegram.

## 5. Steps

Each step ships on its own, with the tests and evals green and the bot working the whole way. The tool list never offers old and new
names together.

0. **The chart that answers itself** (3.8), in today's bot, independent of the rest: `chart_answers_it` on the chart tools, the bot's
   early end of the turn with the caption written in code, and a pair of eval cases (a question that is just the chart, where the model
   sets it; a question with something to answer beside the chart, where it doesn't). Shipped on the live evals; the log's `LLM 2` lines
   show the calls it saves.

1. **Explicit arguments, shared helpers.** The twelve tools take the `Turn`'s inputs as arguments and read nothing else about the
   question; they return their charts in the result instead of appending to `turn.charts`, and `weather_forecast` returns the days it
   shows. So that this step changes no answer, today's corrections from the words move out of the tools into the bot, which applies them
   to the model's calls on the way in, as before; step 3 removes them. The pure helpers the bot imports from data packages move to
   `lib/common` (3.1). Still one process; the evals check the new arguments.
2. **Chart JSON.** `specs.to_json` and `from_json`, and a round trip over every chart the samples and `scripts/dump_specs.py` draw: the
   rebuilt spec draws the same picture (the golden images).
3. **The nine tools, in process.** The tools of 3.3 with their result shapes; the prompt, the shortcuts, `lib/report.py` (`report`,
   `weather_now`, `lookup`, `chart_caption`), the alert charts and the evals move to them together; the twelve go. The bot's corrections
   from the words go too (3.8), and come back only for a case that then fails. `chart_answers_it` moves to `summary` and `days`.
   Shipped only when the live evals (`--live`, all cases) do as well as before.
4. **The data interface, in process.** A `Data` protocol with the tools and resources of section 3, and an in-process implementation
   over today's sources. The bot's direct reads move to it: the gauges (`sheet()` moves to the bot), the alerts (resources, clocks and an
   in-process stand-in for the listen stream in place of `Warmer.after` and `on_live`), irrigation, the prompt's lines and `by_name`
   (from the station resource). Nothing in the bot holds a source object after this step.
5. **The MCP server and client.** `lib/mcp_server.py` serves `Data` (the bus, the low-level server, the transport settings, the token
   check); `lib/mcp_client.py` implements `Data` over MCP (the supervisor, date parsing, charts from the result's `_meta`). The evals read a checked-in tool
   list (`tests/evals/tools.json`), which a test keeps equal to the server's.
6. **Two programs.** `ecobot_service.py` starts the sources, the loops and the server; `ecobot.py` connects to `MCP_URL`. The import
   boundary test, both ways. Config split. One compose file per host, TLS and tokens. Scripts go where they work: on the service's host
   `edit_readings`, `cache_status`, `refill_airgradient`, `check_rain`, `bench_storage`, `conditions`, `ecowitt_metrics`; on the bot's
   `eval_prompts` (reading `tests/evals/tools.json`) and `update_charts`; `show_request` and `report_inputs` become MCP clients;
   `dump_specs` writes JSON on the service and draws on the bot.

## 6. Testing

- The suite splits along the boundary: the service's tests (storage, sources, tools, resources, the server) and the bot's (intent,
  report, alerts, drawing, the client), the bot's running against a fake `Data`, or against the real server in memory over an
  archived file.
- A wire test per tool and resource: the arguments it takes, the shape it returns (`test_contract`), dates that come back as `date`s, chart
  data in the result's `_meta` that reads back as a spec for a client that asks, and no chart and no `chart_hint` for one that doesn't.
- A reconnect test against the real app under uvicorn on a localhost port the test stops and starts (not in memory: a connection can't
  fail there, and the in-memory HTTP transport buffers a listen stream): the listen is lost, the bot listens again and reads what it
  depends on; a question asked while the service is down is answered as unreachable, and the next one after it is back is answered; an
  alert due during the gap is sent once.
- A deploy check: a tool call through the proxy's name succeeds (the allowed hosts and the token).
- The live evals run against the service's real tool list, with `chart_answers_it` added as the bot offers it; the pair of cases for it
  (set for a question that is only the chart, not set for one with more to answer) runs in every live run.

## 7. Risks

- **Latency.** Each call is a network round trip: tens of milliseconds on a LAN, against a question budget of seconds. Charts drawn
  ahead are a few calls every 5 minutes. The early cue keeps the head start the poke gives today.
- **The model and the new tools.** New names, arguments and shapes can change what the model picks, and without the corrections from
  the words it must make every choice itself: step 3 ships only on the live evals, in one process, before any network is involved; a
  correction that turns out to be needed comes back as a fill, never an override.
- **A chart sent without the answer.** If the model sets `chart_answers_it` on a question that needed words, the person gets the chart
  and its figures but not the answer. The description limits it to a question that is only the chart; the eval pair checks both sides,
  and a follow-up ("but is a storm coming?") goes to the model as usual.
- **A protocol still settling.** `subscriptions/listen` is the 2026-07-28 revision: the bot pins it, and both programs upgrade the SDK
  together.
- **Two things to run.** More to deploy and watch; `ecobot://health` and the bot's "unreachable" answers make a failure visible.
