# MCP service plan

The plan for turning this repository into a weather and air-quality service that any MCP client can use: the readings file, the
syncers that fill it, and an MCP server over them. Claude Desktop, Claude Code, ChatGPT, Open WebUI or anyone's bot asks it questions
and gets answers it can pass on. Today's Telegram bot becomes the service's first client while the tools move over, and is retired
once another client does its job. Irrigation becomes a second MCP server in its own repository. This is a design and a sequence of
steps, each shipped on its own, in the manner of [storage-plan.md](storage-plan.md).

Decided:
- **The end state is the service alone.** No bot of ours; nothing in the interface may assume one.
- **Charts are data.** Tools return a chart's data; the client draws it (or its model does).
- **Alerts are decided by the service and published over MCP only**, as a log clients listen to.
- **This repository becomes the service**, keeping its history; the bot keeps working as a client until it is retired, then its code
  goes. Irrigation moves to its own repository and server.
- **Separate hosts** are possible: the service runs near Home Assistant and the sensors; clients connect over the network.

## 1. Why

- The archive, the live poll and the backfills run only while the bot runs: a restart, a crash or a Telegram outage leaves holes. As a
  service, the data keeps coming whatever any client is doing.
- Any assistant can ask about the station and the air sensor and get the same careful answers the bot gives today: records with when
  they happened, days ranked and counted, readings at an interval, rain against pressure, whether it will rain. Other Ecowitt MCP
  servers (brendo/ecowitt-mcp) pass Ecowitt's raw cloud JSON through: 90 days at 5 minutes, running rain totals the model must
  difference, no air sensor.
- The bot has grown a second brain in front of the model: regex shortcuts, period hints, rewritten arguments, a 200-line prompt of
  rules about the tools. A service any model can use has to carry that knowledge itself, in its tools and its results.

## 2. What is there now

### 2.1 Who asks for data

| Asker | How | Lives in |
|---|---|---|
| The model | 12 tools: `weather_now`, `weather_history`, `weather_days`, `weather_link`, `air_quality`, `air_days`, `plot_chart`, `air_link`, `air_scan`, `readings`, `weather_forecast`, `pollen_asthma` | `lib/ecowitt`, `lib/airgradient`, `lib/compose.py`, `lib/history.py`, `lib/forecast`, `lib/pollen` |
| Shortcuts in code | `intent.fast_call`, `report_calls`, `model_prefetch`: the same tools through `Tools.call_all`, their JSON parsed by `lib/report.py` | `lib/intent.py`, `lib/bot.py` |
| Report, dashboard, period buttons, charts drawn ahead | the shortcuts, `Ecowitt.dashboard()`, the same tools with new dates | `lib/report.py`, `lib/ecowitt/dashboard.py`, `lib/bot.py` |
| Alerts, their charts and 🔄 | `station.recent()`, `live_rain()`, `air.latest()`, `forecast.days`, `pollen.current()`; the charts through the shortcuts' sentences ("Wind chart 1d") | `lib/alerts` |
| Irrigation | `station.recent(24)` and the forecast, once a day; the valve watchdog every minute; buttons to water, stop and pause | `lib/alerts/irrigation.py`, `lib/irrigation`, `lib/bot.py` |

### 2.2 What fills the file

The station's warm (60 s: 5-minute and 30-minute readings, 4-hour and daily records), the Home Assistant live poll (5 s; minutes, live
bands), the nightly archive, the Home Assistant range import (15 min), the AirGradient's warm (1–4 min), import and backfill, and the
pollen and forecast refreshes (30 and 15 min). Questions fetch too: `weather_history` and the air tools ask Ecowitt or the AirGradient
cloud for what the file lacks, inline.

### 2.3 What the model is told outside the tools

All of this is what a client other than the bot would not know, so it has to move into the service:
1. **The prompt** (`lib/prompt.py`):
   - the devices and the names people use for them ("ecowitt", "ag"), what they measure and in which units, Indoor and Outdoor;
   - what "this week", "last month", "3m", "on record" mean;
   - one call per period, not many; when to use which tool; how to read each result (`_when`, `_date`, notes); that a record with when
     it happened is `weather_history`, a ranking of days `weather_days`; a period with no date named, for days, is the whole record;
   - fetch again for every question and every follow-up, never reuse earlier figures; the resolution is chosen for you;
   - a weather emoji before each day, inferred from that day's figures; the caption rules under a chart;
   - "will it rain?": the station read (pressure tendency over 3 hours from whether the high or the low came first; down more than
     3 hPa likely, 1–3 possible, under 1 or rising unlikely; under 1005 hPa unsettled, over 1020 settled; humidity 90% or more, or the
     temperature within 2°C of the dew point; recent rain; wind picking up), then the forecast;
   - what the bot can't do: no lightning, soil channels, indoor air quality or other places; custom alerts can't be added; the alerts
     there are and their thresholds; how far back the history goes.
2. **The date** (the prompt's "RIGHT NOW", `intent.period_hints`): today's date and time, and every date range the words name, worked
   out by the bot because models get calendar maths wrong.
3. **Hidden inputs** (`Turn`): the reading charted, a chart asked for, an average asked for, the air reading the conversation was about,
   a holiday filter, "weather all week" (every reading, in a set order): the bot's regexes decide these beside the model's arguments,
   sometimes overruling them.
4. **Bot-side writing** (`lib/report.py`): the report's lines, the chart captions, the plain lookups' answers, written by the bot from
   the tools' JSON.
5. **Prefetching** (`intent.model_prefetch`): for "will it rain" the bot fetches the reading now, the last 3 hours and the forecast before
   the model starts.

## 3. Design

### 3.1 The service

| Stays (the service) | Leaves |
|---|---|
| `lib/storage`, `lib/ecowitt` (less the gauges' look), `lib/airgradient`, `lib/forecast`, `lib/pollen`, `lib/snapshots.py`, `lib/warm.py`, `lib/history.py`, `lib/compose.py`, `lib/daytable.py`, `lib/analysis`, `lib/lines.py`, `lib/panels.py`, `lib/rain.py`, `lib/specs.py` (chart data), the alert rules (`lib/alerts/weather.py`, `particulates.py`, `forecast.py`, `pollen_and_asthma.py`, `rules.py`), and new: `lib/mcp_server.py`, `lib/periods_text.py` (3.2), `lib/outlook.py` (3.3), `lib/alert_log.py` (3.6) | **To each client:** Telegram, the model loop, the persona and style of answers, drawing (`lib/charts.py`, `lib/draw.py`, `lib/gauges.py`, and the gauges' layout and colours, `GAUGES` and `sheet()` from `lib/ecowitt/dashboard.py`), delivering alerts, per-chat state. **To the irrigation server's repository:** `lib/irrigation`, `lib/alerts/irrigation.py`. **Deleted with the bot:** `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`, `lib/report.py`, the menus and keyboards |

### 3.2 An interface any model can use

The test of every tool, argument, result and note: would a model that has never seen this repository, given only the tool list,
answer the question right in one round of calls? Section 7's evals ask exactly that, of more than one model.

- **The tool descriptions and the results are the contract.** Every rule a choice of tool or the reading of a result depends on is in
  that tool's description (which questions it answers, an example call for each, how to read what comes back) or in the result itself
  (`value_is`, `how_to_read`, `note_*`, `missing`, `warning`). The server's `instructions` repeat a short summary (the devices and their
  nicknames, Indoor and Outdoor, units), but nothing depends on them: on the 2026-07-28 protocol a client only gets them by asking
  (`server/discover`), and many don't pass them to their model. The persona (a friendly Telegram bot, plain text, bullets) is the
  client's.
- **Periods in words, worked out by the service.** Every tool that takes a period takes `period`, the person's own words for it, passed
  unchanged ("this week", "in the last year", "past two years", "yesterday afternoon"), or `start` (and `end`). The service reads them
  by the table below, in the station's time zone, and every result begins with how it read them (`period: {asked: "this week", text:
  "the last 7 days: Wed 4 – Tue 10 Nov 2026", start, end}`) and the time of the figures (`as_of`). The model never needs today's date:
  every relative form is in the table, and the descriptions carry the table. The bot's period hints and its "RIGHT NOW" block go.
- **One naming scheme.** Readings are `sensor.reading`: `outdoor.temperature`, `indoor.humidity`, `pressure.relative`,
  `wind.wind_speed`, `rainfall.rain` (mm that fell), `solar_and_uvi.uvi`, `air.pm2_5`, `air.temperature` (the AirGradient's own).
  Tools that take readings also take the words people use, resolved by the service and listed in each description: a bare reading is
  the weather station's (`temperature` and `humidity` are outdoor and indoor; the AirGradient's own are only ever `air.temperature`
  and `air.humidity`), `pressure` is relative pressure, `uv`, `solar`, `rain` the main gauge, `wind` its speed shaded up to the gusts
  (and the compass rose on a chart); a sensor's name (`outdoor`, `indoor`, `wind`, `air`) is its usual set; `all` is every reading.
  The device nicknames ("ecowitt", "weather station", "ag", "airgradient", "air gradient") are in the descriptions too. `days` keeps
  its day-figure names (`temp_max` is outdoor, `indoor_temp_max`, `rain`, `pm2_5_max`), listed in its schema.
- **Answers ready to pass on.** Every figure comes with its wording: a value with its unit (`"12.3 °C"`), a record with when
  (`"around 3:30pm"`) and its day (`"Fri 9 Jan 2026"`), ratings and AQI for the air, and each day's look (`day_look: {word: "wet",
  emoji: "🌧️"}`), worked out by the service from that day's rain, highest gust, highest and lowest temperature and solar radiation, by
  a rule with its thresholds in the code (step 2). A model quotes; it never formats a time or works out a day's weather.
- **Every choice is an argument**, with a plain name and a default that suits the commonest question. Nothing about the question reaches
  a tool except its arguments: no hidden inputs, and no client rewrites them.
- **Fresh every time.** Every result says its figures are as of `as_of` and that a follow-up or a challenge ("that's wrong, it was
  cooler") needs the tool called again. The resolution is the service's choice, never an argument: a record is refined to the 5-minute
  reading where it is held, and `low_note` or `high_note` says when it isn't.
- **Errors that teach.** A wrong name lists the right ones; a period that isn't in the table lists the forms with what each means now
  (which also tells the model today's date); too many rows names the interval that fits; a period the data doesn't reach says what it
  does reach; a future period points to `forecast`. A model corrects its call once, from the error alone.
- **Small results.** A result is what a model needs to answer, not everything held: a summary's breakdown is by day or by month by the
  period's length, a chart's data is bucketed to a budget (3.7), `readings` caps at 300 rows. Full detail is a further call.
- **Read-only, and honest about fetching.** Every tool is `readOnlyHint`; those that fetch what the file lacks say so
  (`openWorldHint`); request `_meta` `fetch: false` answers from what is held.

**Periods.** `period` must match a form below as a whole phrase (case aside; N a digit or a word up to twelve). There is no partial match
and no falling back to a default: anything else is an error that lists the forms. Examples are as asked on Tue 10 Nov 2026 at 14:05.

| `period` | Means |
|---|---|
| `today` | 00:00 today to now |
| `yesterday` | yesterday 00:00–24:00 |
| `last N minutes`, `N min` | the last N minutes (`m` alone never means minutes) |
| `last N hours`, `Nh`, `last 24 hours`, `1d`, `last day` | the last N hours |
| `last N days`, `Nd` (N ≥ 2) | today and the N−1 whole days before it (`last 7 days`: Wed 4 Nov 00:00 to now) |
| `this week`, `past week`, `the last week`, `week`, `1w` | the last 7 days, as above |
| `last N weeks`, `Nw`, `fortnight` | the last 7N days |
| `past month`, `the last month`, `month`, `1m`, `last 30 days` | the last 30 days |
| `last N months`, `Nm`, `N mo` | the last round(N×365/12) days (`3m`: 12 Aug to now) |
| `past year`, `the last year`, `year`, `1y`, `last 12 months` | the last 365 days |
| `last N years`, `Ny` | the last 365N days |
| `week to date`, `since Monday` | Monday 00:00 to now (Mon 9 Nov) |
| `last week` | the previous Monday to Sunday (Mon 2 – Sun 8 Nov) |
| `this month`, `last month` | 1 Nov to now; all of October |
| `this year`, `last year` | 1 Jan 2026 to now; all of 2025 |
| `2025` | that year (to now if it is this one) |
| `October 2025`, `October` | that month; with no year, the latest that has begun (`December` is December 2025) |
| `A to B`, `between A and B`, `June to August 2026` | from the start of A to the end of B |
| `2026-09-05`, `5 Sep`, `5 September 2025`, `Sat 5 Sep` | that day; with no year, the latest not after today; a weekday that doesn't match the date is an error naming the right one |
| `Monday` … `Sunday`, `last Saturday` | the latest such day before today |
| `Christmas Day`, `Melbourne Cup` (any public holiday), with or without a year | that day, from the service's holiday calendar; the latest not after today |
| `this morning`, `yesterday afternoon`, `Tuesday evening` | 06:00–12:00, 12:00–18:00, 18:00–24:00 of that day |
| `last night`, `Tuesday night`, `overnight` | 22:00 the evening before to 06:00 that day |
| `since 6am`, `since Monday` | from the latest such time (or that day's 00:00) to now |
| `on record`, `all time`, `ever` | from the first reading held, for the sensors asked about, to now |

- **`period` or `start`**, never both (both is an error). `start` and `end` are the station's local time, `YYYY-MM-DD` or `YYYY-MM-DD
  HH:MM`; one with an offset or `Z` is moved into local time. An `end` that is a date includes that whole day; one with a time stops at
  it. No `end` means to now; `end` without `start` is an error.
- **Whole-day tools** (`days`, `relate`, `scan`) take whole days ending yesterday, because today is still settling: a rolling phrase is
  that many whole days to yesterday (`last 7 days`: Tue 3 – Mon 9 Nov), a calendar one runs to yesterday, and the result's `period`
  says it was cut ("to Mon 9 Nov: today isn't over"). A phrase that leaves nothing (`today`, `last 5 hours`, `this month` on the 1st)
  is an error that points to `summary` or `readings`. They take dates only; a time is refused with the same pointer.
- **A future period** is an error that points to `forecast`.

### 3.3 Tools

Ten tools in place of twelve: one per kind of question, each over both sensors where both have the reading.

| Tool | Answers | Replaces | Arguments (default) |
|---|---|---|---|
| `current` | how it is now: every reading, or the ones named; with `context`, today's, this month's and this year's highs and lows beside each reading, and rain's totals | `weather_now`, `air_quality` without dates, the gauges | `readings` (all), `context` |
| `summary` | a period's highs and lows with when; averages; day or month breakdown; wind direction; rain total; air ratings and AQI; a chart of any readings from either sensor or both | `weather_history`, `air_quality` with dates, `plot_chart` | `readings`, `period` (today; a chart with no period, the last 24 hours), `average`, `every_day`, `chart`, `chart_readings`, `layout`, `invert_second_axis`, `chart_answers_it` |
| `readings` | values at an interval, each with its time | `readings` | `reading`, `period` (the last 24 hours), `interval` |
| `days` | days found, ranked or counted by their figures; a figure per month or year, as bars; holidays and weekends; the same over part of each day (the 3pm temperature, the overnight low) | `weather_days`, `air_days` | today's day-tool arguments over one field list, `period` (on record; with `hours`, the last 30 days), `hours`, `chart_answers_it` |
| `relate` | rain against one reading: for pressure, humidity or wind, does rain follow a change in it; for PM2.5 or PM10, is the air cleaner after rain | `weather_link`, `air_link` | `reading`, `period` (90 days; 60 for air), `chart` |
| `scan` | each air reading against every weather reading: which go together | `air_scan` | `reading` (or all), `period` (90 days, at most 400), `chart` |
| `outlook` | will it rain in the next few hours: the station's read and the forecast's | the prompt's outlook rules, `model_prefetch` | none |
| `forecast` | today and the days ahead | `weather_forecast` | `period` (the next 7 days: `today`, `tomorrow`, `next 3 days`, `this weekend`) |
| `pollen` | grass pollen and thunderstorm asthma | `pollen_asthma` | none |
| `about` | what the service measures and doesn't, how far back each kind of history goes, the alerts there are with their thresholds, that alerts can't be added or changed by asking | the prompt's capabilities and limits | none |

**Which tool, in their descriptions.** Each description says what it is not for and where that goes:
- the highest or lowest reading of a period with when it happened (the hottest, coldest, fastest gust, "on record") is `summary`;
  a day's total, or days ranked, counted or filtered on a day's figures (the wettest day, the windiest day, the hottest day it also
  rained, days over 35°C, rain per month) is `days`;
- any chart over time is `summary`'s (one reading, several, both sensors, ratings); a figure per month or year is `days.group_by`;
  "X against rain" is `relate`;
- values at set times or per interval are `readings`;
- "report", "status", "sitrep": `current`, `forecast` (today) and `pollen`, called together; "will it rain" is `outlook`.

**`summary`'s figures and charts:**
- `readings` is what to summarise (default: the weather station's usual set, outdoor and indoor temperature and humidity);
  `chart_readings` is what to draw, defaulting to all of `readings`.
- `layout`: `auto` draws one reading as a line for each sensor that has it, plus the compass rose when wind direction is among them;
  more than one reading as panels, one each, on one time axis; `two_axes` puts two on one panel with a scale each; `same_axis` two of
  one unit over each other; `invert_second_axis` modifies `two_axes`. A reading can be drawn as its traffic-light rating
  (`air.pm2_5:rating`). `all` draws every reading in the station's usual order (temperature, humidity, solar, pressure, rain, dew point,
  then feels-like and VPD where configured, then wind), as "weather all week" does today.
- `chart` defaults to on for periods of 3 days or more. `average: true` adds each reading's average to the figures and draws the chart
  as each day's average.
- `every_day` lists every day of a period up to 31 days (by day or by month still follows the period's length).

**`summary`'s result** is one shape for both sensors: `{period, as_of, series: {"outdoor.temperature": {unit, low, low_text, low_when,
low_date, low_day_look, [low_note], high…, [average], [daily|monthly: each row with its day_look]}, "air.pm2_5": {unit, low…, high…,
average, high_rating, average_rating, high_aqi_us, high_band, average_aqi_us, average_band}, "wind.wind_direction": {most_common, then,
steadiness, [daily]}}, [rain_total_mm], [monthly_note], [notes: {sensor: {…}}], [charts], [missing], [warning]}`. The breakdown and
`monthly_note` are the station's; ratings, AQI and the 400-day cap (30 days fetched inline) the air sensor's; `rain_total_mm` whenever
rain is summarised.

**`current`'s result:** `{as_of, sensors: {"outdoor": {"temperature": {value, unit, text: "12.3 °C", [emoji], [today: {low, low_text,
low_when, high…}], [month…], [year…]}}, "rainfall": {"rain": {…, [today: {total}], [month: {total, wettest_day}], [year…]}}, "wind":
{"wind_speed": {…, [today: {high_gust…}]}}, "air": {"pm2_5": {value, unit, rating, aqi_us, band}}}}`: with `context`, everything the
bot's gauges show (rain as totals and its wettest day, wind's highest gust).

**`outlook`'s result** is the prompt's outlook rules, worked out by the service (`lib/outlook.py`) from the reading now and the last 3
hours: `{as_of, verdict: "likely"|"possible"|"unlikely", reason: "pressure down 2.4 hPa in 3 hours, humidity 91%", pressure: {change_hpa,
tendency, level, text}, moisture: {humidity, dew_point_spread}, recent_rain, wind_trend, forecast: [today's and tomorrow's lines, when
the forecast is on]}`. With the forecast off, the station's read is the whole answer.

**`days` over both sensors:** the fields shown and the default sort come from the sensor of the fields named (an air-only question
shows and ranks by `pm2_5_max`); `days_checked` counts the days on which every sensor named has a day row; each listed day carries each
sensor's source and its `day_look`; the result keeps both sensors' notes (`note_daily`, `exact_from`, `note_trace`, `note_averaged`;
`note_hourly`, `note_missing`). A condition mixing the two sensors leaves out the days whose station figures come from Ecowitt's daily
records (10am to 10am, not the air sensor's midnight to midnight) and says how many. With no period, every day on record: "per year",
"how many times", "the windiest day ever".

**Part of each day (`days.hours`, new; no tool can answer these today).** `hours` works out each day's figures over a stretch of local
time instead of the whole day, and the rest of `days` then works on those figures: "the typical overnight low this month" is
`{hours: "night", stat: "avg", of: "temp_min"}`, "nights below 5°C" `{hours: "night", where: temp_min < 5}`, "the average 3pm
temperature in October" `{hours: "15:00", stat: "avg", of: "temp_avg"}`.
- **Named stretches:** `night` (22:00–06:00), `morning` (06:00–12:00), `afternoon` (12:00–18:00), `evening` (18:00–24:00), `daytime`
  (06:00–18:00), as the periods table has them. The result states the stretch it used.
- **Times on the half hour.** A stretch starts and ends on the half hour ("15:00-16:00", "22:30-06:00"): the figures take each half
  hour's mean, and a 30-minute reading's own lowest and highest run to the end of its half hour, so part of a half hour would be
  counted as all of it. Any other time is refused, but for a moment: a single time on the hour or half hour ("15:00", or "15:00-15:05").
  On a day held at 5 minutes a moment is that reading; on a day held only at 30 minutes it is the half hour from it (on the air
  sensor's hourly days, the hour), and the result says so.
- **Which stretch.** `[start, end)` in local wall time, read as `start` to `end - 1` (the SQL's `BETWEEN` includes the end). "24:00" is
  the next local midnight, so a whole day is 23 or 25 hours on a clock-change day. A stretch that crosses midnight starts the evening
  before and belongs to the day it ends on (Monday 22:00 to Tuesday 06:00 is Tuesday's night): one stretch per sensor and day. A
  stretch the clock change removes (02:00–03:00 on the first Sunday of October) is left out and counted.
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
| `summary` | `average: true` adds each reading's average over the period, and one per day or month in its breakdown, and draws the chart as each day's average. The average is weighted by time: a part day at either end of the period counts for its hours, and an air day held only as hourly averages for its hours (until step 0 the station's average was the mean of its days' means, a part day counting as a whole one, and the air sensor's the mean of its stored readings, an hourly reading counting as one twelfth of a 5-minute hour). An air reading always has its average; its six air-quality readings also their `average_rating`, and PM2.5 its `average_aqi_us` and `average_band`; the air sensor's own temperature and humidity have no rating. Rain is a total, not an average; rain rate has an average (`weather_history` has given it one since step 0); wind direction has its most common direction instead. A chart's panels report each line's average beside the lowest and highest of its half-hour averages, a rain panel its total. |
| `readings` | for a reading that is averaged, each row is its interval's average with its lowest and highest: hourly averages at `1hour`; at `1day` the day's average, lowest and highest, and when those two happened; at `1min` and `5min` the stored reading itself. Rain is the mm that fell in the interval (at `1day`, the day's total); gusts the interval's highest (at `1day`, with when); wind direction the reading at the interval's end (at `1day`, the day's last). Those three have no lowest and highest. |
| `days` | a field for each day's average of every reading but rain, gusts and wind direction (`temp_avg`, `humidity_avg`, `pressure_avg`, `wind_speed_avg`, `rain_rate_avg`, `indoor_temp_avg` …; the air sensor's `pm2_5_avg`, `co2_avg` … for its six air-quality readings), and `stat: "avg"` over the matching days, overall or per month or year ("average humidity per year"). Rain is a day's total (`rain`), so "average daily rain in June" is `stat: "avg", of: "rain"`. With `hours`, the same over that part of each day (rain: the mm that fell in it). |

**What becomes of the bot's hidden inputs.** Today the bot reads the question with regexes and the tools use that reading beside the
model's arguments: some fill an argument the model left out, three overrule the model (the reading charted, the readings stacked, a
holiday filter). Each was added for a mistake the production model made (the eval cases `humidity-chart`, `temperature-and-rain`,
`days-over-limit-by-year`, `rain-by-month-two-years`), several of them because the choice was a hidden input, an easily missed argument
or a default that suited the wrong question. In the service each is an argument with a plain name and a default that suits the
question, and the model sets it:

| Today (`Turn`) | In the service |
|---|---|
| `chart_field`, `chart_fields` (overrule the model) | `summary.chart_readings`, defaulting to every reading summarised, drawn as panels |
| `readings` (which readings the words name) | `summary.readings` |
| `two_axes` | `layout: "two_axes"` |
| `average_asked` (the chart drawn as daily means; the model can't ask for that today) | `average` (figures and chart together) |
| `per_day` | `every_day` |
| `chart_asked` | `chart` |
| `text`: rain by month, grouping (fill), holidays (overrule) | `days.group_by`, `days.only`; the rain bars' grouping follows the period's length |
| `air_named` (the air reading charted when none is named) | `readings` (the conversation is in the model's context) |
| "weather all week" (`ALL_ORDER`, `CHART_ALL_*`) | `chart_readings: "all"`, in the service's order and settings |
| `ahead` (a chart drawn ahead, nobody waiting) | request `_meta.priority: "background"` |
| `Tool.asked` (the air sensor's own temperature and humidity offered only when named) | always listed, only as `air.temperature` and `air.humidity`, described as the AirGradient's own |
| `forecast_shown` (the forecast days a chat was shown) | gone: the forecast alert is the service's (3.6) |
| `CHART_SOURCE` | the chart data names its sources (3.7) |

### 3.4 Resources

For applications that keep an eye on the station rather than ask about it; a model doesn't read them (what it needs is in the tools).

| URI | Holds | Pushed |
|---|---|---|
| `ecobot://station` | name, time zone, latitude and longitude, created; the sensors, their readings with labels, units and the words people use for them; each source's mode (HA, cloud, local) and retention; the alert kinds, what each means and its threshold; the AirGradient dashboard link | on change |
| `ecobot://now/weather` | the live reading: `{as_of, group: {field: {value, unit, text, time}}}` | each new reading (5 s with HA, 60 s cloud only) |
| `ecobot://now/air` | the latest air reading, its time, ratings and AQI | each new reading |
| `ecobot://alerts` (in the resource list) and `ecobot://alerts{?since,expired}` (a template) | the alerts, newest last, kept 30 days; `since` an alert id: those after it; unexpired ones only unless `expired=true` (3.6) | each new alert, on the bare URI |
| `ecobot://forecast` | the forecast days, structured (min and max, rain chance, chance of at least 1 mm, summary), with `fetched_at` and `place` | each new forecast |
| `ecobot://pollen` | grass and asthma levels with their days, `fetched_at`, `in_season` | each new page |
| `ecobot://health` | each loop's last success and failure, archive progress, the file's size | no |
| `ecobot://schema/chart` | the chart data's JSON Schema (3.7) | no |

Dates are ISO strings. On the 2026-07-28 protocol (MCP SDK 2.3), changes are delivered on a `subscriptions/listen` stream: each event
says "this URI changed, read it again", with no replay. The low-level server serves it with `Server(…,
on_subscriptions_listen=ListenHandler(bus))` and a `bus = InMemorySubscriptionBus()`; the service's loops `await
bus.publish(ResourceUpdated(uri))` for each URI above when it changes (repeats of an unchanged reading not sent), and
`ToolsListChanged()` when a sensor sends a field it never sent. Subscriptions match exact URIs, so clients subscribe to the bare URIs
the resource list gives. Only clients on the 2026-07-28 revision can listen; older ones read the resources and poll.

### 3.5 Prompts

The service also offers MCP prompts, the templates clients show as commands or buttons, each a question in words with its period as an
argument: `now`, `report`, `will_it_rain`, `weather` (every reading, a panel each), `chart` (a reading and a period), `rain`, `air`.
They hold no logic: the client's model answers them with the tools, as any question. They replace the bot's keyboard and `/commands`
for clients that show prompts.

### 3.6 Alerts

The rules move to the service: rain starting and stopping (with the quiet-hours note), gusts, UV, indoor and outdoor temperature
crossings, particulates (mask on, safe again), pollen and thunderstorm asthma, a forecast revised. Their thresholds, quiet hours and
cooldowns are the service's config, and their state its own store (`alert_state.json`, out of the bot's `bot_state.json`). Each runs
on the triggers it has today: a new live reading or 5-minute row, a new forecast or pollen page, and its own clock (the weather alerts
every 60 seconds, particulates every 30 minutes and only then, since its two-readings rule counts readings across checks).
- **An alert is an event:** `{id, kind, time, expires, title, text, data, [link: {label, url}], [chart]}`. `text` is ready to send
  ("🌧️ It's started raining: 0.4 mm so far"); `data` the figures behind it; `link` the page that goes with it (the AirGradient
  dashboard for particulates, the pollen site for pollen); `chart` its chart data when it has one (gusts, UV, temperature,
  particulates), which like every chart carries the call that drew it (`drawn_by`, 3.7), so a client's 🔄 calls it again for the
  latest readings with no knowledge of the kinds.
- **Alerts go stale:** `expires` is set by the rule (rain and gusts about an hour, UV its rearm time, particulates the next check,
  pollen the end of its day, a forecast the day it is about), and `ecobot://alerts?since=` returns only unexpired alerts unless asked.
  A client back after a gap sends what is still true, never the morning's rain as news.
- **Delivery is the client's.** A client listens, reads `ecobot://alerts?since=<last id it handled>`, and sends each alert to whoever
  it serves. Our bot does this for its subscribed chats until it is retired.
- **The forecast alert is station-wide:** for today and tomorrow, it compares each new forecast with the first one fetched after 6am
  that day (its baseline), alerts when the maximum moves by more than 2°C or the chance of rain crosses 50%, and makes the forecast it
  alerted on the new baseline. Its text says what changed ("The forecast for tomorrow has changed: was 24°C and dry, now 19°C with
  rain likely"), not "since I sent it", which only the bot could say.
- **Only listening clients get alerts.** Most chat clients don't keep an MCP listen stream open; once the bot is retired, alerts reach
  no one unless a client that listens is running (a short script with the SDK, or Home Assistant). Section 8.

### 3.7 Charts

Charts are data: the client draws them, or its model does from the numbers.
- **`summary` and `days` (with `group_by`) draw** (and `relate` and `scan` when asked); they build the chart as now (`specs.Chart`), next
  to the reads that chose its resolution, buckets and rose, and return it in the result: `charts: [{title, period, source, drawn_by,
  panels: [{unit, [label], series: [{name, kind: "line"|"band"|"bars"|"rating", points: [[time, value, (low, high)]]}], [zones],
  [compass]}]}]`. Times are ISO local times; `source` names the devices ("Ecowitt weather station and AirGradient sensor"). The JSON
  Schema is in the repository (`schemas/chart.json`) and at `ecobot://schema/chart`, versioned.
- **Every chart says how to draw it again:** `drawn_by: {tool, arguments}` is the call that drew it as the service read it, with the
  readings' names resolved, every default filled in and the period stated, so the same call with only `period` changed draws the same
  chart over another period, and the same call again draws it with the latest readings. A client's period buttons, or an alert's 🔄,
  replay it; they need no knowledge of which tool or readings made the chart, and never the model.
- **Small enough for a model to read and redraw:** each line is bucketed to at most 150 points (the bands with it), so a model can turn
  it into a plot with its own tools and an application can draw it directly. An application that draws for itself asks for more with
  request `_meta` `chart_points` (at most 500, today's budget); the bot asks for 500, so its pictures and golden images don't change.
  Full detail is `readings`.
- **The note to the model:** a result with a chart says so in one line (`chart_note`: the chart's data is in `charts`; draw it if you
  can, and keep the text to the figures; don't describe the chart), so a model neither ignores it nor repeats every point.
- **When the chart is the answer, the reply comes ready.** `summary` and `days` take `chart_answers_it`, described as "true only when
  the question is just to see this chart and its figures: nothing else to answer" ("plot pressure this week", not "plot pressure this
  week: is a storm coming?"). The model judges that; no regex does. When it is set and a chart is made, the result carries a finished
  reply for the person (the period, each reading's high and low with when, any note about missing data) as a text block annotated
  `audience: ["user"]` (MCP's mark for content meant for the person, not the model), and result `_meta` `{"ecobot/complete": true}`.
  Only a client can save the model's second call, since it runs the model loop and MCP gives a server no way to keep a result from a
  client's model; the service makes it one generic rule: *if a result is complete, send its user text and its chart, and end the
  turn*. Our bot follows the rule while it lives, and any bot can; a client that doesn't (a desktop assistant) gives the result to its
  model as usual, which then has the reply ready to repeat. A result with no chart, an error or a note that needs the model's words is
  never complete.
- **Our bot**, while it lives, draws `charts` with its renderer (`lib/charts.py`, reading the JSON) and sends the picture; the rain
  caption reads its bars. The renderer leaves with the bot.

### 3.8 Connecting

- **One program:** `python -m ecobot_service` runs the syncers, the alert rules and the server, at `https://<service>/mcp`
  (Streamable HTTP, `streamable_http_app`, under uvicorn behind TLS: a reverse proxy such as Caddy, or uvicorn's own certificate). The
  server is the SDK's low-level `Server`, so each tool's input schema is hand-written JSON with its description; the list is built when
  asked, so it follows the file's metrics. The app is built with `TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS)`: by
  default it answers only `Host: 127.0.0.1` or `localhost`, and refuses the name a proxy or a LAN client uses.
- **Stdio, for a desktop client:** `python -m ecobot_service.bridge` is a thin bridge, an SDK client to the running service re-exposed
  on stdio (tools, resources, prompts and the listen stream forwarded), with no syncers, no rules and no file of its own. A second
  process on the readings file would duplicate the fetching and the alerts, and its in-memory bus would hear nothing.
- **Auth:** the service's own `TokenVerifier` (the SDK has only the protocol): a constant-time compare against `MCP_TOKENS`
  (`name:token,…`, one per client, so one can be revoked alone), passed with `auth=AuthSettings(issuer_url="https://<service>",
  resource_server_url=None)` (`issuer_url` is required but unused without an OAuth provider; no OAuth metadata is published). Bind to
  the LAN or a tailnet; nothing needs the internet to reach it.
- **A client that listens** (our bot, an alert forwarder): `Client(…, mode="2026-07-28")`, pinning the protocol revision (the default
  probes, and a proxy's 502 during a restart can land it on the older revision, which cannot listen). A pinned client skips
  `server/discover`, so it calls it once itself (`session.send_discover`, then `session.adopt`) to get the instructions and
  capabilities. In the SDK a connection failure on any request ends the whole client, so one supervisor task owns it: it discards the
  client when it exits with a transport error or the listen is lost or ends, builds a new one after a backoff, discovers and listens
  again, and reads the alerts after its last id. A read timeout of about 45 seconds (three of the server's 15-second pings) notices a
  silently dead link.

### 3.9 Irrigation, its own server

`lib/irrigation` and the irrigation check move to their own repository and MCP server, next to the valve, once the weather service runs
on its own (step 5).
- **Tools:** `irrigation_status` (battery, weather delay, running or not), `water` (minutes; refused while a weather delay is on, with an
  error that says so), `stop`, `delay` (`24h`, `48h`, `72h`, `cancel`); the last three `destructiveHint`, so a client asks before it
  calls them.
- **The watchdog** (a run must end; a restart mid-run still closes the valve) and the daily check (pause when it rained or will) run in
  that server whatever any client does.
- **Rain and forecast** come from the weather service, as its client: the measured rain from `summary{readings: ["rain"], period: "last
  24 hours"}`, the forecast from `ecobot://forecast` (its days' chance of at least 1 mm and `fetched_at`). When the weather service is
  unreachable, the check runs on the forecast it last read and skips the measured rain.
- **Its alerts** (battery low, paused for rain) are its own `irrigation://alerts`, in the same shape; a client listens to both servers.

### 3.10 Configuration

`ECOWITT_*`, `HOMEASSISTANT_*`, `AIRGRADIENT_*`, `ANTISPIKE_REQUIRE_CONSECUTIVE`, `POLLEN*`, `FORECAST_*`, `PLACE`, `TZ` (until the
station names one), `DATA_DIR`, the alert settings (cooldowns, quiet hours, thresholds), `CHART_ALL_FEELS_LIKE`, `CHART_ALL_VPD`,
`MCP_TOKENS`, `MCP_BIND`, `MCP_ALLOWED_HOSTS`. The irrigation server: `IRRIGATION_*` (Tuya), `WEATHER_MCP_URL`, `WEATHER_MCP_TOKEN`,
its own `MCP_TOKENS`.

## 4. The bot until it is retired

The bot is the service's first client, and the measure of each step: it keeps answering in Telegram while its insides move to the
service. Beyond what the steps need, it gets no new features. While it lives it:
- offers the model the service's tools as they are, with the server's instructions, adding only its persona (a friendly Telegram bot,
  plain text, bullets, the caption length a photo allows);
- ends the turn on a complete result (3.7), sending the ready reply and the chart without a second model call;
- keeps a few shortcuts for its buttons and commonest typed requests (a named reading's chart and a period, weather now, the report,
  the gauges), each a fixed call to a service tool whose result is already worded, so it writes nothing itself;
- draws `charts` and the gauges (`current` with `context`) with its renderer;
- listens to `ecobot://alerts` (and, from step 5, `irrigation://alerts`) and sends each alert to the chats subscribed to its kind, with
  its chart, its link and a 🔄 that replays its chart's `drawn_by`;
- builds `/usage`, Help and its alert menu from `ecobot://station` (and whether an irrigation server is configured);
- keeps per-chat state: subscriptions, admins, and each chart's `drawn_by` behind its period buttons (a press replays it with a new
  `period`).

It is retired when a replacement does what you use it for: another client for questions (any MCP-capable assistant), and, for alerts,
a client that listens. Its code is then deleted from this repository: `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`,
`lib/report.py`, the menus, the renderer and its tests and golden images.

## 5. What does not change

The readings file, its tables and its day rows; how each tool chooses its resolution and fetches what it lacks; the alert rules
themselves; the charts' content.

## 6. Steps

Each step ships on its own, with the tests and evals green and the bot working the whole way. The model's tool list changes once
(step 3): old and new names are never offered together, and no tool interface is rewritten before it is replaced.

0. **Fixes the service needs anyway**, in today's code (done: `lib/partday.py`, the averages, `chart_answers_it`):
   - **Part of each day** (3.3): the engine (stretches through `Reader.part_days`, rain over a stretch, the coverage choice) with tests
     against stretches worked out by hand (a night across midnight, a clock-change day, a moment on a 5-minute and on a 30-minute day,
     a day held only as daily records, a night with rain in the evening only, an air glitch inside the stretch). The `hours` argument
     comes with `days` (step 2).
   - **Averages weighted by time** (3.3): `weather_history`'s and `air_quality`'s averages over the period's time, not its days or its
     rows, and rain rate's average; tests with a part day at each end and an air period across its hourly history.
   - **The chart that answers itself** (3.7): `chart_answers_it` on `weather_history`, `air_quality` and the day tools, the finished
     reply (today's `report.chart_caption`) and the complete mark in the result, and the bot's early end of the turn on it; an eval pair
     (a question that is just the chart, where the model sets it; one with something else to answer, where it doesn't). Shipped on the
     live evals; the log's `LLM 2` lines show the calls it saves. It carries over to `summary` and `days` in step 2.
1. **Charts as data, periods in words** (done: `specs.to_json`/`from_json`, `schemas/chart.json`, `lib/periods_text.py`). The chart
   builders return 3.7's JSON with its schema, and the bot's renderer reads the JSON (at 500 points, so the golden images don't change).
   `lib/periods_text.py` reads the periods table, tested by hand against it at fixed times (a Monday, a Tuesday, the 1st of a month, 1
   January, 29 February, 00:30, both clock-change days), with whole-day tools cut to yesterday and errors for phrases outside the table
   and for the future. No tool interface changes.
2. **The ten tools, beside the twelve.** The tools of 3.3, built once with their arguments, periods, worded results (`day_look`, ready
   lines, captions), charts, descriptions and errors, and `lib/outlook.py`; offered to no model yet. Contract tests; the evals run
   against them with the tool list alone. The bot's own code paths call them instead of the twelve: its shortcuts, report,
   lookups, gauges and alert charts, and the calls behind its period buttons (a press replays the chart's `drawn_by` with a new
   `period`; the buttons themselves, their keyboard and picture cache stay in the bot, and go with it). The service knows nothing of
   buttons.
3. **The swap**, gated on the live evals (all cases, the production model and one other, with the tool list alone and with the
   instructions): the model gets the ten tools; the bot's prompt keeps only its persona; the twelve, the `Turn`'s inputs, the period
   hints, the prefetch and the corrections from the words go.
4. **Alerts in the service.** The rules, their state and config move service-side and append events to the alert log, with `expires`,
   `link` and `chart` (with its `drawn_by`); the forecast alert gets its station-wide baseline. The bot delivers from the log.
5. **MCP, and the service on its own.** `lib/mcp_server.py` serves the tools, resources and prompts (the bus and listen handler, the
   transport settings, the token check); the service starts as its own program (`python -m ecobot_service`, its compose file) with the
   stdio bridge; the bot becomes a client over the network (the supervisor, discover for the instructions, `charts` from results, the
   alert log; `schemas/` goes into the image, which `.dockerignore` keeps out today). Then the irrigation server is built in its own
   repository against it, the bot connects to both, and `lib/irrigation` goes from here. The service's scripts (`edit_readings`,
   `cache_status`, `refill_airgradient`, `check_rain`, `bench_storage`, `conditions`, `ecowitt_metrics`, `dump_specs`) stay;
   `show_request` and `report_inputs` become MCP clients.
6. **The bot retired** (section 4), its code and tests deleted; the repository is the service.

## 7. Testing

- **Any model can use it.** The evals are the service's: each question is asked of a model given only the tool list (no prompt of
  ours), and again with the server's instructions, and checked for the tools and arguments it picks and, where it matters, the answer.
  They run against the production model and at least one other, so the interface isn't tuned to one model's habits. The cases carry
  over from `tests/evals/cases.json`, with their periods checked as the service reads them, and gain one for each periods form, each
  routing rule (a record or a day, a chart or a figure per month), `outlook`, `about` and `chart_answers_it`.
- **The contract:** a test per tool and resource for the arguments it takes and the shape it returns (`test_contract`); the periods
  table at its fixed times (step 1); charts against their schema; every chart's `drawn_by`, replayed, draws the same chart,
  and with only `period` changed, the same chart over that period; `day_look` against its rule.
- `days.hours` gives, for a stretch that is the whole day ("00:00-24:00"), the same figures as its day row for every day held whole at
  5 or 30 minutes (the air sensor's at 5 minutes or an hour), a clock-change day included; a day from a daily record, or from whatever
  readings there were, is left out and counted.
- **Alerts:** the rules' tests move with them; the log keeps order and survives a restart; a client that reconnects reads what it
  missed, once, and nothing that has expired; the forecast alert's baseline.
- **A client that listens:** against the real app under uvicorn on a localhost port the test stops and starts (not in memory: a
  connection can't fail there, and the in-memory HTTP transport buffers a listen stream): the listen is lost, the client listens again
  and reads the alerts after its last id; the bot's model request carries the instructions from discover.
- **A deploy check:** a tool call through the proxy's name succeeds (the allowed hosts and the token).

## 8. Risks

- **Alerts reach only listening clients.** With alerts over MCP only (3.6), once the bot is retired nobody gets them unless a client
  that listens is running. Plan that client before step 6, or accept no alerts.
- **Charts only where a client can draw.** A client that neither draws the chart data nor lets its model draw gets the figures without
  a picture.
- **Models other than ours.** A client's model may be weaker, or tuned differently, than the one the evals were built on; some clients
  never pass the instructions on. The descriptions and results as the contract, and the evals across models with and without the
  instructions, are the defence, and every answer still rests on figures the model quotes, not works out.
- **Every question through a model.** Without the bot's shortcuts each question costs at least one model call; that is the client's
  choice and cost. A chart that answers the question costs only one where the client ends the turn on a complete result (3.7).
- **A chart sent without the answer.** If a model sets `chart_answers_it` on a question that needed words, the person gets the chart
  and its figures but not the answer: the description limits it to a question that is only the chart, the eval pair checks both
  sides, and a follow-up goes to the model as usual.
- **A protocol still settling.** `subscriptions/listen` is the 2026-07-28 revision; listening clients pin it, and the SDK is upgraded
  on both sides together.
