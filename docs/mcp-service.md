# MCP service plan

The plan for turning this repository into a weather and air-quality service that any MCP client can use: the readings file, the
syncers that fill it, and an MCP server over them. Claude Desktop, Claude Code, ChatGPT, Open WebUI or anyone's bot asks it questions
and gets answers it can pass on. Today's Telegram bot becomes the service's first client while the tools move over, and is retired
once another client does its job. Irrigation becomes a second MCP server in its own repository. This is a design and a sequence of
steps, each shipped on its own, in the manner of [storage-plan.md](storage-plan.md).

Decided:
- **The end state is the service alone.** No bot of ours; nothing in the interface may assume one.
- **Charts are data.** Tools return a chart's data; the client draws it (or its model does).
- **Alerts are decided by the service and published over MCP only**, as a resource clients listen to.
- **This repository becomes the service**, keeping its history; the bot keeps working as a client until it is retired, then its code
  goes. Irrigation moves to its own repository and server.
- **Separate hosts** are possible: the service runs near Home Assistant and the sensors; clients connect over the network.

## 1. Why

- The archive, the live poll and the backfills run only while the bot runs: a restart, a crash or a Telegram outage leaves holes. As a
  service, the data keeps coming whatever any client is doing.
- Any assistant can ask about the station and the air sensor and get the same careful answers the bot gives today: records with when
  they happened, days ranked and counted, readings at an interval, rain against pressure. Other Ecowitt MCP servers (brendo/ecowitt-mcp)
  pass Ecowitt's raw cloud JSON through: 90 days at 5 minutes, running rain totals the model must difference, no air sensor.
- The bot has grown a second brain in front of the model: regex shortcuts, period hints, rewritten arguments, a 200-line prompt of
  instructions about the tools. A service any model can use has to carry that knowledge itself, in its tools and its results.

## 2. What is there now

### 2.1 Who asks for data

| Asker | How | Lives in |
|---|---|---|
| The model | 12 tools: `weather_now`, `weather_history`, `weather_days`, `weather_link`, `air_quality`, `air_days`, `plot_chart`, `air_link`, `air_scan`, `readings`, `weather_forecast`, `pollen_asthma` | `lib/ecowitt`, `lib/airgradient`, `lib/compose.py`, `lib/history.py`, `lib/forecast`, `lib/pollen` |
| Shortcuts in code | `intent.fast_call`, `report_calls`, `model_prefetch`: the same tools through `Tools.call_all`, their JSON parsed by `lib/report.py` | `lib/intent.py`, `lib/bot.py` |
| Report, dashboard, period buttons, charts drawn ahead | the shortcuts, `Ecowitt.dashboard()`, the same tools with new dates | `lib/report.py`, `lib/ecowitt/dashboard.py`, `lib/bot.py` |
| Alerts | `station.recent()`, `live_rain()`, `air.latest()`, `forecast.days`, `pollen.current()`, after each refresh or new live reading | `lib/alerts` |
| Irrigation | `station.recent(24)` and the forecast, once a day; the valve watchdog every minute | `lib/alerts/irrigation.py`, `lib/irrigation` |

### 2.2 What fills the file

The station's warm (60 s: 5-minute and 30-minute readings, 4-hour and daily records), the Home Assistant live poll (5 s; minutes, live
bands), the nightly archive, the Home Assistant range import (15 min), the AirGradient's warm (1–4 min), import and backfill, and the
pollen and forecast refreshes (30 and 15 min). Questions fetch too: `weather_history` and the air tools ask Ecowitt or the AirGradient
cloud for what the file lacks, inline.

### 2.3 What the model is told outside the tools

This is what a client other than the bot would not know, so it has to move into the service:
1. **The prompt** (`lib/prompt.py`): the devices and the names people use for them ("ecowitt", "ag"); what "this week", "last month",
   "3m", "on record" mean; one call per period, not many; when to use which tool; how to read each result (`_when`, `_date`, notes);
   Indoor and Outdoor; metric units; a weather emoji before each day, inferred from that day's figures; the caption rules under a chart.
2. **Period hints** (`intent.period_hints`, the prompt's "RIGHT NOW"): the bot works out every date range the words name and hands
   them to the model, because models get calendar maths wrong.
3. **Hidden inputs** (`Turn`): the reading charted, a chart asked for, an average asked for, the air reading the conversation was about,
   a holiday filter: the bot's regexes decide these beside the model's arguments, sometimes overruling them.
4. **Bot-side writing** (`lib/report.py`): the report's lines, the chart captions, the plain lookups' answers are written by the bot from
   the tools' JSON.

## 3. Design

### 3.1 The service

| Stays (the service) | Leaves |
|---|---|
| `lib/storage`, `lib/ecowitt`, `lib/airgradient`, `lib/forecast`, `lib/pollen`, `lib/snapshots.py`, `lib/warm.py`, `lib/history.py`, `lib/compose.py`, `lib/daytable.py`, `lib/analysis`, `lib/lines.py`, `lib/panels.py`, `lib/rain.py`, `lib/specs.py` (chart data), the alert rules (`lib/alerts/weather.py`, `particulates.py`, `forecast.py`, `pollen_and_asthma.py`, `rules.py`), and new: `lib/mcp_server.py`, `lib/periods_text.py` (3.3), `lib/alert_log.py` (3.6) | **to each client:** Telegram, the model loop, the persona and style of answers, drawing (`lib/charts.py`, `lib/draw.py`, `lib/gauges.py`), delivering alerts, per-chat state. **To the irrigation server's repository:** `lib/irrigation`, `lib/alerts/irrigation.py`. **Deleted with the bot:** `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`, `lib/report.py`, the menus and keyboards |

### 3.2 An interface any model can use

The test of every tool, argument, result and note: would a model that has never seen this repository, given only the server's
instructions and tool list, answer the question right in one round of calls? Section 6's evals ask exactly that, of more than one model.

- **The guidance lives in the server.** What the prompt teaches today moves to where every client gets it: the server's `instructions`
  (returned when a client connects) say what the devices are and the names people use, what the sensors measure and in which units,
  how to say Indoor and Outdoor, how to quote a record's time and date, how periods are written, that one call covers a period. Each
  tool's description says which questions it answers, with an example call for each, and how to read what comes back. Each result says
  what its figures are (`value_is`, `how_to_read`) and what to pass on (`note_*`, `missing`, `warning`). The persona (a friendly
  Telegram bot, plain text, bullets) is the client's.
- **Periods in words, worked out by the service.** Every tool that takes a period takes `period`: a phrase from a small grammar the
  description lists (`today`, `yesterday`, `last 5 hours`, `last 7 days`, `this week`, `last week`, `this month`, `last month`,
  `October 2025`, `2025`, `3m`, `1y`, `on record`), or `start` and `end`. The service resolves it in the station's time zone, with the
  rules the prompt states today (weeks start on Monday; "last week" is the previous calendar week; "the last week" or "past week" is 7
  days to now; "on record" is all of it), and every result begins with the period it used, written out ("Mon 3 – Sun 9 Nov 2026") and
  as dates. No client does calendar maths; the bot's period hints and its "RIGHT NOW" block go. The current time is in every result
  (`as_of`).
- **One naming scheme.** Readings are `sensor.reading` (`outdoor.temperature`, `indoor.humidity`, `pressure.relative`, `wind.wind_speed`,
  `rainfall.rain`, `solar_and_uvi.uvi`, `air.pm2_5`); `current` and `summary` also take a bare reading, meaning every sensor that has it
  (`temperature` is outdoor and indoor). The names, their plain-language labels, units and the words people use for them are in
  `ecobot://station` and in each tool's enum, built from what the file holds. `days` keeps its day-figure names (`temp_max` is
  outdoor, `indoor_temp_max`, `rain`, `pm2_5_max`), listed in its schema.
- **Answers ready to pass on.** Every figure comes with its wording: a value with its unit (`"12.3 °C"`), a record with when
  (`"around 3:30pm"`) and its day (`"Fri 9 Jan 2026"`), a day with its weather emoji (worked out by the service from that day's figures,
  as the prompt asks the model to today), ratings and AQI for the air. A model can quote; it never formats a time or infers an emoji.
- **Every choice is an argument.** Nothing about the question reaches a tool except its arguments; there are no hidden inputs and no
  client rewrites them. Arguments have plain names and defaults that suit the commonest question.
- **Errors that teach.** A wrong name lists the right ones; too many rows names the interval that fits; a period the data doesn't reach
  says what it does reach. A model corrects its call once, from the error alone.
- **Small results.** A result is what a model needs to answer, not everything held: a summary's breakdown is by day or by month by the
  period's length, a chart's data is bucketed to a budget (3.7), `readings` caps at 300 rows. Full detail is a further call.
- **Read-only, and honest about fetching.** Every tool is `readOnlyHint`; those that fetch what the file lacks say so
  (`openWorldHint`), and request `_meta` `fetch: false` answers from what is held.

### 3.3 Tools

Nine tools in place of twelve, one per kind of question, each over both sensors where both have the reading.

| Tool | Answers | Replaces | Arguments (default period) |
|---|---|---|---|
| `current` | how it is now; one reading now; the air now; with `context`, today's, this month's and this year's highs and lows beside each reading (the bot's gauges) | `weather_now`, `air_quality` without dates, the dashboard | `metrics` (default outdoor and indoor), `context` |
| `summary` | a period's highs and lows with when; averages; day or month breakdown; wind direction; rain total; air ratings and AQI; a chart | `weather_history`, `air_quality` with dates | `metrics`, `period` (today), `average`, `every_day`, `chart`, `chart_metrics`, `layout`, `daily_means`, `rain_by` |
| `readings` | values at an interval, each with its time | `readings` | `metric`, `period`, `interval` |
| `days` | find, rank and count days; a figure per month or year; holidays and weekends; the same over part of each day (the 3pm temperature, the overnight low) | `weather_days`, `air_days` | today's day-tool arguments over one field list, `period` (the last 30 days), `hours` |
| `relate` | rain against one reading: for pressure, humidity or wind, does rain follow a change in it; for PM2.5 or PM10, is the air cleaner after rain | `weather_link`, `air_link` | `metric`, `period` (90 days; 60 for air), `chart` |
| `scan` | each air reading against every weather reading: which go together | `air_scan` | `metric` (or all), `period` (90 days, at most 400), `chart` |
| `chart` | any readings from both sensors on one chart: a panel each, or two on one panel | `plot_chart` | `panels[{metric, style}]`, `period` (30 days), `layout`, `invert_second_axis` |
| `forecast` | today and the days ahead | `weather_forecast` | `days` |
| `pollen` | grass pollen and thunderstorm asthma | `pollen_asthma` | none |

Periods of whole days (`days`, `relate`, `scan`, `chart`) end yesterday, because today is still settling; the others run to now.

**`summary`'s figures and charts** are two inputs, as they are today (`groups` and `chart_field(s)`):
- `metrics` is what to summarise; `chart_metrics` is what to draw, defaulting to the first of `metrics` (temperature when it is there).
- `layout` (shared with `chart`): `auto` draws one panel of `chart_metrics[0]` with a line for each sensor that has it, plus a second
  chart of wind with the compass rose when `wind.wind_direction` is summarised and nothing is stacked; `panels` stacks every
  `chart_metrics` reading, a panel each, on one time axis; `two_axes` puts two on one panel with a scale each; `same_axis` two of one
  unit over each other. `invert_second_axis` modifies `two_axes`.
- `chart` defaults to on for periods of 3 days or more; `daily_means` draws one point a day; `average` adds means to the figures;
  `every_day` lists every day of a period up to 31 days (by day or by month still follows the period's length); `rain_by` (`auto`,
  `month`, `year`) sets the rain bars.
- A chart mixing station and air readings is `chart`'s, not `summary`'s: `summary` answers it with an error that says so.

**`summary`'s result** is one shape for both sensors: `{period, as_of, series: {"outdoor.temperature": {unit, low, low_text, low_when,
low_date, [low_note], high…, [average], [daily|monthly]}, "air.pm2_5": {unit, low…, high…, average, high_rating, average_rating,
high_aqi_us, high_band, average_aqi_us, average_band}, "wind.wind_direction": {most_common, then, steadiness, [daily]}},
[rain_total_mm], [monthly_note], [notes: {sensor: {…}}], [charts], [missing], [warning]}`. The breakdown and `monthly_note` are the
station's; ratings, AQI and the 400-day cap (30 days fetched inline) the air sensor's; `rain_total_mm` whenever `rainfall.rain` is
summarised. `current`'s result is `{as_of, sensors: {"outdoor": {"temperature": {value, unit, text: "12.3 °C", [emoji], [today:
{low…, high…}], [month…], [year…]}}, "air": {"pm2_5": {value, unit, rating, aqi_us, band}}}}`.

**`days` over both sensors:** the fields shown and the default sort come from the sensor of the fields named (an air-only question
shows and ranks by `pm2_5_max`); `days_checked` counts the days on which every sensor named has a day row; each listed day carries each
sensor's source; the result keeps both sensors' notes (`note_daily`, `exact_from`, `note_trace`, `note_averaged`; `note_hourly`,
`note_missing`). A condition mixing the two sensors leaves out the days whose station figures come from Ecowitt's daily records (10am
to 10am, not the air sensor's midnight to midnight) and says how many.

**Part of each day (`days.hours`, new; no tool can answer these today).** `hours` works out each day's figures over a stretch of local
time instead of the whole day, and the rest of `days` then works on those figures: "the typical overnight low this month" is
`{hours: "22:00-06:00", stat: "avg", of: "temp_min"}`, "nights below 5°C" `{hours: "22:00-06:00", where: temp_min < 5}`, "the average
3pm temperature in October" `{hours: "15:00-15:05", stat: "avg", of: "temp_avg"}`.
- **Times on the half hour.** A stretch starts and ends on the half hour ("15:00-16:00", "22:30-06:00"): the figures take each
  half hour's mean, and a 30-minute reading's own lowest and highest run to the end of its half hour, so part of a half hour would be
  counted as all of it. Any other time is refused, but for one exception: a moment, five minutes from the hour or half hour
  ("15:00-15:05"). On a day held at 5 minutes a moment is that reading; on a day held only at 30 minutes it is the half hour from it
  (on the air sensor's hourly days, the hour), and the result says so.
- **Which stretch.** `[start, end)` in local wall time, read as `start` to `end - 1` (the SQL's `BETWEEN` includes the end). "24:00" is
  the next local midnight, so a whole day is 23 or 25 hours on a clock-change day. A stretch that crosses midnight starts the evening
  before and belongs to the day it ends on (Monday 22:00 to Tuesday 06:00 is Tuesday's night): one stretch per sensor and day. A
  stretch the clock change removes (02:00-03:00 on the first Sunday of October) is left out and counted.
- **Which readings.** Each sensor's stretch is read at the first of the day rows' own resolutions that holds all of it, by the
  coverage (`Reader.held`): the station's 5-minute, then 30-minute readings; the air sensor's 5-minute, then hourly ones; never the
  minutes. A stretch held at neither (a day held only as Ecowitt's daily or 4-hour records, a night whose evening is before the
  30-minute archive) is left out and counted in a note; one that is held but has no readings (the station was off) counts as missing.
  This is not `lib/ecowitt/period.py`'s choice for a period's part days, which falls back to whatever readings there are.
- **The figures** come from the day rows' own SQL over one window a day (`Reader.part_days`), so a stretch's mean, lowest, highest and
  their times are worked out as a whole day's are, the air sensor's glitches taken out. Except rain: the day's `rain` is the highest
  of the running daily total, which over a stretch would be the rain since midnight up to its end. Over a stretch, `rain` is the mm
  that fell in it, the total's rise from the last reading before the stretch to its last reading in it, a fall counted as the midnight
  reset (`lib/rain.rain_slots`, as `readings` works it out), computed beside `part_days`.
- **The period.** As `days`' (to yesterday), except that today counts once today's stretch is over and held: asked this morning,
  "nights below 5°C this week" includes last night. At most 400 days, as `scan`'s: the stretches cost far more than day rows (over
  four years of 5-minute readings a night takes about 1 s for a climate sensor, 3.4 s for the air sensor with its glitch rule, against
  0.01 s for the day rows; the default 30 days under 0.1 s), and a stretch that is the whole day reads the day rows.

**Averages**, wherever the question asks for one:

| Tool | Averages |
|---|---|
| `summary` | `average: true` adds each reading's average over the period, and one per day or month in its breakdown. The average is weighted by time: a part day at either end of the period counts for its hours, and an air day held only as hourly averages for its hours (today the station's average is the mean of its days' means, a part day counting as a whole one, and the air sensor's the mean of its stored readings, an hourly reading counting as one twelfth of a 5-minute hour: fixed in step 0). An air reading always has its average; its six air-quality readings also their `average_rating`, and PM2.5 its `average_aqi_us` and `average_band`; the air sensor's own temperature and humidity have no rating. Rain is a total, not an average; rain rate has an average (today `weather_history` leaves every rain reading out, though `days` has `rain_rate_avg`); wind direction has its most common direction instead. `daily_means` draws the chart as each day's average. |
| `readings` | for a reading that is averaged, each row is its interval's average with its lowest and highest: hourly averages at `1hour`; at `1day` the day's average, lowest and highest, and when those two happened; at `1min` and `5min` the stored reading itself. Rain is the mm that fell in the interval (at `1day`, the day's total); gusts the interval's highest (at `1day`, with when); wind direction the reading at the interval's end (at `1day`, the day's last). Those three have no lowest and highest. |
| `days` | a field for each day's average of every reading but rain, gusts and wind direction (`temp_avg`, `humidity_avg`, `pressure_avg`, `wind_speed_avg`, `rain_rate_avg`, `indoor_temp_avg` …; the air sensor's `pm2_5_avg`, `co2_avg` … for its six air-quality readings), and `stat: "avg"` over the matching days, overall or per month or year ("average humidity per year"). Rain is a day's total (`rain`), so "average daily rain in June" is `stat: "avg", of: "rain"`. With `hours`, the same over that part of each day (rain: the mm that fell in it). |
| `chart` | each line's average over the period (`mean`), beside the lowest and highest of its half-hour averages (`min`, `max`: not the true extremes, which `summary` gives with their times). A rain panel has its total (`total_mm`) instead, a rating panel the share of time in each rating. With `two_axes` or `same_axis`, each series keeps its own figures. |

**What becomes of the bot's hidden inputs.** Today the bot reads the question with regexes and the tools use that reading beside the
model's arguments: some fill an argument the model left out, three overrule the model (the reading charted, the readings stacked, a
holiday filter). Each was added for a mistake the production model made (the eval cases `humidity-chart`, `temperature-and-rain`,
`days-over-limit-by-year`, `rain-by-month-two-years`), several of them because the choice was a hidden input or an easily missed
argument. In the service each is an argument with a plain name, and the model sets it:

| Today (`Turn`) | In the service |
|---|---|
| `chart_field`, `chart_fields` (overrule the model) | `summary.chart_metrics`, `layout: "panels"` |
| `readings` (which readings the words name) | `summary.metrics` |
| `two_axes` | `layout: "two_axes"` |
| `average_asked`, from the words "average", "avg", "mean": the chart drawn as daily means, the average-first wording (the model's own `average` only adds averages to the figures; it can't ask for that chart today) | `daily_means` |
| `per_day` | `every_day` |
| `chart_asked` | `chart` |
| `text`: rain by month, grouping (fill), holidays (overrule) | `rain_by`, `days.group_by`, `days.only` |
| `air_named` (the air reading charted when none is named) | `metrics` (the conversation is in the model's context) |
| `ahead` (a chart drawn ahead, nobody waiting) | request `_meta.priority: "background"` |
| `Tool.asked` (the air sensor's own temperature and humidity offered only when named) | always listed, described as the AirGradient's own readings, not the weather station's |
| `forecast_shown` (the forecast days a chat was shown) | gone: the forecast alert is the service's (3.6) |
| `CHART_SOURCE` | the chart data names its sources (3.7) |

### 3.4 Resources

For applications that keep an eye on the station rather than ask about it.

| URI | Holds | Pushed |
|---|---|---|
| `ecobot://station` | name, time zone, latitude and longitude, created; the sensors, their readings with labels, units and the words people use for them; each source's mode (HA, cloud, local) and retention; the alert kinds and what each means; the AirGradient dashboard link | on change |
| `ecobot://now/weather` | the live reading: `{as_of, group: {field: {value, unit, text, time}}}` | each new reading (5 s with HA, 60 s cloud only) |
| `ecobot://now/air` | the latest air reading, its time, ratings and AQI | each new reading |
| `ecobot://alerts{?since}` | the alerts sent, newest last, kept 30 days; `since` an alert id: those after it (3.6) | each new alert |
| `ecobot://forecast` | the forecast days, with `fetched_at` and `place` | each new forecast |
| `ecobot://pollen` | grass and asthma levels with their days, `fetched_at`, `in_season` | each new page |
| `ecobot://health` | each loop's last success and failure, archive progress, the file's size | no |

Dates are ISO strings. The MCP SDK (2.3, protocol revision 2026-07-28) delivers changes on a `subscriptions/listen` stream: each event
says "this URI changed, read it again", with no replay. The service passes an `InMemorySubscriptionBus` to the server, and its loops
publish `ResourceUpdated` for each URI above when it changes, and `ToolsListChanged` when a sensor sends a field it never sent (the
metric lists grow). Repeats of an unchanged reading are not sent. Only clients on the 2026-07-28 revision can listen; older ones can
read the resources and poll.

### 3.5 Prompts

The service also offers MCP prompts, the templates clients show as commands or buttons ("/rain", "Weather now"), each a question in
words with its period as an argument: `now`, `report` (now, the air, today's forecast and pollen), `chart` (a reading and a period),
`rain`, `air`. They hold no logic of their own: the client's model answers them with the tools, as any question. They replace the bot's
keyboard and `/commands` for clients that show prompts.

### 3.6 Alerts

The rules move to the service: rain starting and stopping (with the quiet-hours note), gusts, UV, indoor and outdoor temperature
crossings, particulates (mask on, safe again), pollen and thunderstorm asthma, a forecast revised. Their thresholds, quiet hours and
cooldowns are the service's config, and their state its own store (`alert_state.json`, out of the bot's `bot_state.json`). Each runs
on the triggers it has today: a new live reading or 5-minute row, a new forecast or pollen page, and its own clock (the weather alerts
every 60 seconds, particulates every 30 minutes and only then, since its two-readings rule counts readings across checks).
- **An alert is an event:** `{id, kind, time, title, text, data, [chart]}`, the text ready to send ("🌧️ It's started raining: 0.4 mm
  so far"), `data` the figures behind it, `chart` its chart data when it has one (gusts, UV, temperature, particulates). Each is
  appended to `ecobot://alerts`, which pushes an update.
- **Delivery is the client's.** A client listens, reads `ecobot://alerts?since=<last id it handled>`, and sends each alert to whoever
  it serves. After a gap it reads from its last id, so nothing is lost while the alert is still in the 30-day log. Our bot does this
  for its subscribed chats until it is retired.
- **The forecast alert is station-wide:** it compares a new forecast with the last one it alerted on, not with what each chat was
  shown, which only the bot knew.
- **Only listening clients get alerts.** Most chat clients don't keep an MCP listen stream open; with the bot retired, alerts reach no
  one unless a client that listens is set up (a few lines of Python with the SDK, or Home Assistant). Section 7.

### 3.7 Charts

Charts are data: the client draws them, or its model does from the numbers.
- **The tools that draw** (`summary`, `days` with `group_by`, `relate`, `scan`, `chart`) build the chart as now (`specs.Chart`), next to
  the reads that chose its resolution, buckets and rose, and return it in the result: `charts: [{title, period, source, panels: [{unit,
  [label], series: [{name, kind: "line"|"band"|"bars"|"rating", points: [[time, value, (low, high)]]}], [zones], [compass]}]}]`.
  Times are ISO local times; a `source` names the devices ("Ecowitt weather station and AirGradient sensor").
- **Small enough for a model to read and redraw:** each line is bucketed to at most 150 points (a week at hourly, a year at
  daily-ish), the bands with it; a model can turn it into a plot with its own tools, and an application can draw it directly. Full
  detail is `readings`. A JSON Schema for it is in the repository (`schemas/chart.json`) and at `ecobot://schema/chart`, versioned.
- **The note to the model:** a result with a chart says so in one line (`chart_note`: the chart's data is in `charts`; draw it if you
  can, and keep the text to the figures; don't describe the chart), so a model neither ignores it nor repeats every point.
- **Our bot**, while it lives, draws `charts` with its renderer (`lib/charts.py`, reading the JSON) and sends the picture; the rain
  caption reads its bars. The renderer leaves with the bot.

### 3.8 Connecting

- **Server:** the SDK's low-level `Server`, so each tool's input schema is hand-written JSON with its description; the list is built
  when asked, so it follows the file's metrics. Served as Streamable HTTP (`streamable_http_app`) under uvicorn, behind TLS (a reverse
  proxy such as Caddy, or uvicorn's own certificate), at `https://<service>/mcp`, built with
  `TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS)` (by default it answers only `Host: 127.0.0.1` or `localhost`, and
  refuses the name a proxy or a LAN client uses). Also over stdio (`python -m ecobot_service --stdio`), for a desktop client on the
  same machine as the file.
- **Auth:** the service's own `TokenVerifier` (the SDK has only the protocol): a constant-time compare against `MCP_TOKENS`
  (`name:token,…`, one per client, so one can be revoked alone), with `AuthSettings(resource_server_url=None)` so no OAuth metadata is
  published. Bind to the LAN or a tailnet; nothing needs the internet to reach it.
- **A client that listens** (our bot, an alert forwarder): `Client(…, mode="2026-07-28")`, pinning the protocol revision (the default
  probes, and a proxy's 502 during a restart can land it on the older revision, which cannot listen). In the SDK a connection failure on
  any request ends the whole client, so one supervisor task owns it: it discards the client when it exits with a transport error or
  the listen is lost or ends, builds a new one after a backoff, listens again and reads what it depends on. A read timeout of about 45
  seconds (three of the server's 15-second pings) notices a silently dead link.

### 3.9 Irrigation, its own server

`lib/irrigation` and the irrigation check move to their own repository and MCP server, next to the valve.
- **Tools:** `irrigation_status` (battery, weather delay, running or not), `water` (minutes), `stop`, `delay` (`24h`, `cancel`), the
  last three `destructiveHint`, so a client asks before it calls them.
- **The watchdog** (a run must end; a restart mid-run still closes the valve) and the daily check (pause when it rained or will) run
  in that server whatever any client does.
- **Rain and forecast** come from the weather service, as its client: `summary{metrics: ["rainfall.rain"], period: "last 24 hours"}`
  and `forecast`. When the weather service is unreachable, the check runs on the forecast it last had and skips the measured rain.
- **Its alerts** (battery low, paused for rain) are its own `irrigation://alerts`, in the same shape.

### 3.10 Configuration

`ECOWITT_*`, `HOMEASSISTANT_*`, `AIRGRADIENT_*`, `ANTISPIKE_REQUIRE_CONSECUTIVE`, `POLLEN*`, `FORECAST_*`, `PLACE`, `TZ` (until the
station names one), `DATA_DIR`, the alert settings (cooldowns, quiet hours, thresholds), `MCP_TOKENS`, `MCP_BIND`, `MCP_ALLOWED_HOSTS`.
The irrigation server: `IRRIGATION_*` (Tuya), `WEATHER_MCP_URL`, `WEATHER_MCP_TOKEN`, its own `MCP_TOKENS`.

## 4. The bot until it is retired

The bot is the service's first client, and the measure of each step: it keeps answering in Telegram while its insides move to the
service. It gets no new features. While it lives it:
- connects to the service over MCP and offers the model the service's tools and instructions as they are, adding only its persona
  (a friendly Telegram bot, plain text, bullets, the caption length a photo allows);
- keeps a few shortcuts for its buttons and commonest typed requests (a named reading's chart and a period, weather now, the report,
  the gauges), each a fixed call to a service tool whose result is already worded, so it writes nothing itself;
- draws `charts` and the gauges (`current` with `context`) with its renderer;
- listens to `ecobot://alerts` and sends each alert to the chats subscribed to its kind, with its chart;
- keeps per-chat state: subscriptions, admins, the chart questions behind its period buttons.

It is retired when a replacement does what you use it for: another client for questions (any MCP-capable assistant), and, for alerts,
a client that listens. Its code is then deleted from this repository: `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`,
`lib/report.py`, the menus, the renderer and its tests and golden images.

## 5. What does not change

The readings file, its tables and its day rows; how each tool chooses its resolution and fetches what it lacks; the alert rules
themselves; the charts' content.

## 6. Steps

Each step ships on its own, with the tests and evals green and the bot working the whole way. The tool list never offers old and new
names together.

0. **Fixes that belong to the service anyway**, in today's code:
   - **Part of each day** (3.3, `days.hours`): `hours` on `weather_days` and `air_days`, with tests against stretches worked out by hand
     (a night across midnight, a clock-change day, a moment on a 5-minute and on a 30-minute day, a day held only as daily records, a
     night with rain in the evening only, an air glitch inside the stretch) and eval cases ("average 3pm temperature in October",
     "typical overnight low this month", "nights below 5°C this week").
   - **Averages weighted by time** (3.3): `weather_history`'s and `air_quality`'s averages over the period's time, not its days or its
     rows, and rain rate's average; tests with a part day at each end and an air period across its hourly history.
1. **Explicit arguments, worded results.** The twelve tools take the `Turn`'s inputs as arguments and read nothing else about the
   question; they return their charts in the result (3.7) instead of appending to `turn.charts`. Results gain the wording the bot writes
   today (`lib/report.py`'s lines and captions, the day emoji), and the bot quotes them instead of writing its own. So that this step
   changes no answer, today's corrections from the words move out of the tools into the bot, which applies them to the model's calls on
   the way in, as before; step 3 removes them.
2. **The guidance moves to the service.** Periods in words (`lib/periods_text.py`, tested against the bot's period hints for every
   phrase they handle); the server instructions and the tool descriptions take over the prompt's knowledge (2.3); the bot's prompt keeps
   only its persona. The evals run each question with only the instructions and the tool list, as any client would.
3. **The nine tools.** The tools of 3.3 with their result shapes; the bot's shortcuts and its alert charts move to them; the twelve go,
   and with them the bot's corrections from the words, which come back only for a case that then fails. Shipped only when the live
   evals (all cases) do as well as before, with the production model and one other.
4. **Alerts in the service.** The rules, their state and config move service-side and append to the alert log; the bot delivers from
   the log. `current` gains `context` and the gauges come from it.
5. **MCP.** `lib/mcp_server.py` serves the tools, resources and prompts (the bus, the low-level server, the transport settings, the
   token check, stdio); the bot becomes an MCP client over the network (the supervisor, `charts` from results, the alert log). The
   evals read the server's own tool list. The irrigation server is built in its own repository and the bot's irrigation buttons call
   it; `lib/irrigation` goes from here.
6. **Two programs.** The service starts on its own (`python -m ecobot_service`), with its compose file; the bot connects to it. The
   service's scripts (`edit_readings`, `cache_status`, `refill_airgradient`, `check_rain`, `bench_storage`, `conditions`,
   `ecowitt_metrics`, `dump_specs`) stay; `show_request` and `report_inputs` become MCP clients.
7. **The bot retired** (section 4), its code and tests deleted; the repository is the service.

## 7. Testing

- **Any model can use it.** The evals are the service's: each question is asked of a model given only the server's instructions and
  tool list (no prompt of ours), and checked for the tools and arguments it picks and, where it matters, the answer. They run against
  the production model and at least one other, so the interface isn't tuned to one model's habits.
- **The contract:** a test per tool and resource for the arguments it takes and the shape it returns (`test_contract`), periods in
  words against their dates (every phrase, a clock change, the first of a month and a year), charts against their schema.
- `days.hours` gives, for a stretch that is the whole day ("00:00-24:00"), the same figures as its day row for every day held whole at
  5 or 30 minutes (the air sensor's at 5 minutes or an hour), a clock-change day included; a day from a daily record, or from whatever
  readings there were, is left out and counted.
- **Alerts:** the rules' tests move with them; the log keeps order and survives a restart; a client that reconnects reads what it
  missed, once.
- **A client that listens:** against the real app under uvicorn on a localhost port the test stops and starts (not in memory: a
  connection can't fail there, and the in-memory HTTP transport buffers a listen stream): the listen is lost, the client listens again
  and reads the alerts after its last id.
- **A deploy check:** a tool call through the proxy's name succeeds (the allowed hosts and the token).

## 8. Risks

- **Alerts reach only listening clients.** With alerts over MCP only (3.6), once the bot is retired nobody gets them unless a client
  that listens is running. Plan that client before step 7, or accept no alerts.
- **Charts only where a client can draw.** A client that neither renders the chart data nor lets its model draw gets the figures
  without a picture.
- **Models other than ours.** A client's model may be weaker, or tuned differently, than the one the evals were built on: the guidance
  in the server and the evals across models are the defence, and every answer still rests on figures the model quotes, not works out.
- **Every question through a model.** Without the bot's shortcuts each question costs at least one model call; that is the client's
  choice and cost.
- **A protocol still settling.** `subscriptions/listen` is the 2026-07-28 revision; listening clients pin it, and the SDK is upgraded
  on both sides together.
