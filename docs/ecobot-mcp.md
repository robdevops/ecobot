# MCP service plan (ecobot-mcp)

> **Names today.** The program this plan calls "the service" is **ecobot-mcp**: the package `ecobot_mcp` (`python -m ecobot_mcp`),
> the systemd unit `ecobot-mcp.service`, the Docker image and container `ecobot-mcp`. Its settings start with `MCP_`, irrigation-mcp's
> with `IRRIGATION_`, the shared ones have neither (`AUTH_TOKENS`, which replaced `MCP_SERVICE_TOKENS`): see
> [configuration.md](configuration.md). Every request needs a token: with `AUTH_TOKENS` unset, ecobot-mcp generates one in the data
> folder, so it never runs open. The plan keeps its history: section 2 and steps 0 to 8 record how the Telegram bot's tools, alerts and
> pictures moved into the service, and name the bot as it was; the bot itself was removed afterwards (step 9).

The design of a weather and air-quality service that any MCP client can use: the readings file, the syncers that fill it, and an MCP
server over them. Claude Desktop, Claude Code, ChatGPT, Open WebUI, signalbot or anyone's client asks it questions and gets answers
it can pass on. Irrigation is a second MCP server, irrigation-mcp, in this repository as its own package and Docker image (decided
November 2026). This is a design and a sequence of steps, each shipped on its own, in the manner of [storage-plan.md](storage-plan.md).

Decided:
- **The service alone.** No client of ours; nothing in the interface may assume one.
- **Charts are data.** Tools return a chart's data; the client draws it (or its model does).
- **Alerts are decided by the service and published over MCP only**, as a log clients listen to (`ecobot://feed`).
- **This repository is ecobot-mcp**, keeping its history. Irrigation is its own server (irrigation-mcp: its own package and image, in this repository).
- **Separate hosts** are possible: the service runs near Home Assistant and the sensors; clients connect over the network.

## 1. Why

- The archive, the live poll and the backfills run in a service of their own: a client's restart, crash or outage leaves no holes in
  the data, which keeps coming whatever any client is doing.
- Any assistant can ask about the station and the air sensor and get careful answers: records with when they happened, days ranked
  and counted, readings at an interval, rain against pressure, whether it will rain. Other Ecowitt MCP servers
  (brendo/ecowitt-mcp) pass Ecowitt's raw cloud JSON through: 90 days at 5 minutes, running rain totals the model must
  difference, no air sensor.
- A service any model can use carries the knowledge of its tools itself, in its tools and its results, not in a client's prompt of
  rules, regex shortcuts and rewritten arguments.

## 2. What it replaced

Before step 1 the Telegram bot held all of this itself: twelve tools for its model (`weather_now`, `weather_history`, `weather_days`,
`plot_chart`, `air_quality` and others), regex shortcuts and period hints that rewrote the model's arguments, a 200-line system prompt
of rules about the tools (the devices and the names people use for them, units, what "this week" means, when to use which tool, how to
read each result, "will it rain?"), the report and the chart captions written in the bot from the tools' JSON, and the alerts, the
irrigation controller and the pictures drawn ahead. All of that a client other than the bot would not know, so it moved into the
service: the tools carry their descriptions, periods in words, worded results and notes (3.2 and 3.3), the server's instructions say
the rest, the rules run in the service (3.6), and the pictures are drawn there (3.7). Section 3 says what each part became.
The sources, their warm-ups and the archive are unchanged in what they fill: the station's warm (60 s: 5-minute and 30-minute readings,
4-hour and daily records), the Home Assistant live poll (5 s; minutes, live bands), the nightly archive, the Home Assistant range import
(15 min), the AirGradient's warm (1–4 min), import and backfill, and the pollen and forecast refreshes (30 and 15 min).

## 3. Design

### 3.1 The service

| Stays (the service) | Leaves |
|---|---|
| `lib/storage`, `lib/ecowitt` (less the gauges' look), `lib/airgradient`, `lib/forecast`, `lib/pollen`, `lib/snapshots.py`, `lib/warm.py`, `lib/history.py`, `lib/compose.py`, `lib/daytable.py`, `lib/analysis`, `lib/lines.py`, `lib/panels.py`, `lib/rain.py`, `lib/specs.py` (chart data), the alert rules (`lib/alerts/weather.py`, `particulates.py`, `forecast.py`, `pollen_and_asthma.py`, `rules.py`), and new: `lib/mcp_server.py`, `lib/periods_text.py` (3.2), `lib/service/` (the tools of 3.3 and what they share), `lib/service/outlook.py` (3.3), `lib/alert_log.py` (3.6) | **To each client:** Telegram, the model loop, the persona and style of answers, drawing (`lib/charts.py`, `lib/draw.py`, `lib/gauges.py`, and the gauges' layout and colours, `GAUGES` from `lib/ecowitt/dashboard.py` and `lib/gauge_sheet.py`), delivering alerts, per-chat state. **To the irrigation server's repository:** `lib/irrigation`, `lib/alerts/irrigation.py`. **Deleted with the bot:** `lib/bot.py`, `lib/llm.py`, `lib/prompt.py`, `lib/intent.py`, `lib/report.py`, the menus and keyboards |

### 3.2 An interface any model can use

The test of every tool, argument, result and note: would a model that has never seen this repository, given only the tool list,
answer the question right in one round of calls? Section 7's evals ask exactly that, of more than one model.

- **The tool descriptions and the results are the contract.** Every rule a choice of tool or the reading of a result depends on is in
  that tool's description (which questions it answers, an example call for each, how to read what comes back) or in the result itself
  (`value_is`, `how_to_read`, `note_*`, `missing`, `warning`). The server's `instructions` repeat a short summary (the devices and their
  nicknames, Indoor and Outdoor, the readings' names, units, and that every question, a follow-up or a challenge too, is fetched afresh: a client may drop earlier tool turns, and with them `fresh`), but nothing depends on them: on the 2026-07-28 protocol a client only gets them by asking
  (`server/discover`), and many don't pass them to their model. The persona (its tone, plain text or markdown) is the
  client's.
- **Periods in words, worked out by the service.** Every tool that takes a period takes `period`, the person's own words for it, passed
  unchanged ("this week", "in the last year", "past two years", "yesterday afternoon"), or `start` (and `end`). The service reads them
  by the table below, in the station's time zone, and every result begins with how it read them (`period: {asked: "this week", text:
  "the last 7 days: Wed 4 – Tue 10 Nov 2026", start, end}`) and the time of the figures (`as_of`). The model never needs today's date:
  every relative form is in the table, and the descriptions carry the table. No client needs to work out a date range.
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

- **`period` or `start`**: models fill in every argument they see, sometimes with junk, so when both are sent: `start` and `end` that
  aren't dates or times (`count_only`), or that read as the same span as the period (within 15 minutes at each end), are set aside, the
  period is read and the result's `period.note` says so; dates that read as another span are refused, naming both spans as read ("`period`
  'on record' is Sat 1 Oct 2022 – Mon 9 Nov 2026 but `start`/`end` say Wed 1 Jan 2025 – now: send one of them"), since real dates are the
  person's; a period that can't be read beside readable dates is refused with its forms, saying the dates were set aside because `period`
  was sent and to send them without it to use them. A note on the period keeps a chart from being complete (3.7): the model reads it.
  `start` and `end` are the station's local time, `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`; one with an offset or `Z` is moved into local time. An `end` that is a date includes that whole day; one with a time stops at
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
  more than one reading as panels, one each, on one time axis; `two_axes` puts two on one panel with a scale each (up to four in two units: the first reading's unit on the left, the other on the right, one call for all of them, asked for by "one chart", "another axis", "second axis", "dual axis", "inverted axis", "two Y axes", "two scales" or "overlay ... on another axis"); `same_axis` two of
  one unit over each other; `invert_second_axis` modifies `two_axes`. A reading can be drawn as its traffic-light rating
  (`air.pm2_5:rating`), in `chart_readings` or in `readings` (summarised as the reading, and drawn as its rating when
  `chart_readings` isn't sent; a model sends it either way). `all` draws every reading in the station's usual order (temperature, humidity, solar, pressure, rain, dew point,
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
{"wind_speed": {…, [today: {high…, high_gust…}]}}, "air": {"pm2_5": {value, unit, rating, aqi_us, band}}}}`: with `context`, everything the
gauges show (rain as totals and its wettest day, wind speed's highest and its highest gust); with `context` and no readings named, every gauge's reading
the station sends now, those its file hasn't recorded yet (outside the catalogue) too, as the gauges show them. The air sensor's
air-quality readings have their highs and lows with `context` too (from its day rows, with their day but no time of day), so a
reading it lacks now, or the sensor not answering, still gives its figures with `value` null.

**`outlook`'s result** is the prompt's outlook rules, worked out by the service (`lib/service/outlook.py`) from the reading now and
the last 3 hours: `{as_of, verdict: "likely"|"possible"|"unlikely", reason: "pressure down 2.4 hPa in 3 hours, humidity 91%",
pressure: {change_hpa, tendency, level, text}, moisture: {humidity, dew_point_spread}, recent_rain, wind_trend, forecast: [today's and
tomorrow's lines, when the forecast is on]}`. With the forecast off, the station's read is the whole answer.

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

**What became of the old bot's hidden inputs.** The Telegram bot read the question with regexes and the tools used that reading beside the
model's arguments: some fill an argument the model left out, three overrule the model (the reading charted, the readings stacked, a
holiday filter). Each was added for a mistake the production model made (the eval cases `humidity-chart`, `temperature-and-rain`,
`days-over-limit-by-year`, `rain-by-month-two-years`), several of them because the choice was a hidden input, an easily missed argument
or a default that suited the wrong question. In the service each is an argument with a plain name and a default that suits the
question, and the model sets it:

| Was (`Turn`) | In the service |
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
| `ecobot://station` | name, time zone, latitude and longitude, created; the sensors, their readings with labels, units and the words people use for them; each source's mode (HA, cloud, local) and retention; the alert kinds, what each means and its threshold; the AirGradient dashboard link | when the catalogue gains a reading |
| `ecobot://now/weather` | the live reading as kept, in the service's names (`current`'s, nothing fetched for it): `{as_of, sensors: {sensor: {reading: {value, unit, text, [emoji], [compass]}}}, lines, heading, [unavailable]}` | each new reading (5 s with HA, 60 s cloud only) |
| `ecobot://now/air` | the latest air reading, the same shape: its time, each reading's rating, PM2.5's and PM10's AQI and band (the US AQI is the higher) | each new reading |
| `ecobot://feed` | the alerts as a feed for signalbot (`lib/feed.py`, 3.6): `{identity, kinds: [{kind, label, words}], items: [{id, kind, time, expires, text, [link], [chart: {tool, arguments, [arg, values]}]}]}`, unexpired only | each new alert |
| `ecobot://forecast` | the forecast days, structured (min and max, rain chance, chance of at least 1 mm, the day's rain in mm with its likely range and the floor `rain_mm_floor` (the `≥N` of the line `~17 mm (9–25 likely; ≥5 at 90%)`), summary), with `fetched_at` and `place` | each new forecast |
| `ecobot://pollen` | grass and asthma levels with their days, `fetched_at`, `in_season` | each new page |
| `ecobot://status` | the Status picture: the station's temperature, humidity and rain gauges in a row as a PNG (a blob, `image/png`, `lib/gauges.py`), after their readings as JSON text (`{gauges: [{title, value, unit, rows}], lines}`: the reading now and the day's, month's and year's highs and lows, which the picture leaves out), then the report alone as `text/plain`, for a client that posts words as they are; from `current` with context, read as a call is; without Pillow, the text with `image_note` | no |
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
argument: `now`, `report`, `will_it_rain`, `weather_chart` (every reading, a panel each), `chart` (a reading and a period),
`rainfall`, `air`. They hold no logic: the client's model answers them with the tools, as any question. Their names are chat
commands too (a chat client such as signalbot may offer each as a `/command`), so they keep to the usual form (lowercase letters, digits and
`_`, at most 32).

### 3.6 Alerts

The rules move to the service: rain starting and stopping (with the quiet-hours note), gusts, UV, indoor and outdoor temperature
crossings, particulates (mask on, safe again), pollen and thunderstorm asthma, a forecast revised. Their thresholds, quiet hours and
cooldowns are the service's config, and their state its own store (`alert_state.json`). Each runs
on the triggers it has today: a new live reading or 5-minute row, a new forecast or pollen page, and its own clock (the weather alerts
every 60 seconds, particulates every 30 minutes and only then, since its two-readings rule counts readings across checks).
- **An alert is an event:** `{id, kind, time, expires, title, text, data, [link: {label, url}], [chart]}`. `text` is ready to send
  ("🌧️ It's started raining: 0.4 mm so far"); `data` the figures behind it; `link` the page that goes with it (the AirGradient
  dashboard for particulates, the pollen site for pollen); `chart` its chart data when it has one (gusts, UV, temperature,
  particulates), which like every chart carries the call that drew it (`drawn_by`, 3.7), so a client's 🔄 calls it again for the
  latest readings with no knowledge of the kinds.
- **Alerts go stale:** `expires` is set by the rule (rain and gusts about an hour, UV its rearm time, particulates the next check,
  pollen the end of its day, a forecast the day it is about), and `ecobot://feed` lists only unexpired alerts.
  A client back after a gap sends what is still true, never the morning's rain as news.
- **Delivery is the client's.** A client listens for `ecobot://feed`, reads it when it changes, and sends each alert it has not handled
  to whoever it serves.
- **As a feed for signalbot.** `ecobot://feed` (and irrigation-mcp's `irrigation://feed`) gives the unexpired alerts in signalbot's feed
  format (its `docs/FEEDS.md`; `lib/feed.py`), so signalbot posts them to the chats subscribed with `/alerts`, with no client of ours:
  the kinds this server can send now (by its sources), each with its label and the words people use for it; each alert's id as a
  string, its `text`, its link's URL, and its chart's `drawn_by` as `{tool, arguments}`, with `arg` "period" and its periods as `values`
  when the call names one, which signalbot offers as buttons. signalbot reads it every 30 seconds. There is no `ecobot://alerts`: the feed is
  the one alert resource, with no `since` or `expired` query.
- **The forecast alert is station-wide:** for today and tomorrow, it compares each new forecast with the first one fetched after 6am
  that day (its baseline), alerts when the maximum moves by more than 2°C or the chance of rain rises past 50% (or falls from there to 30% or less: the band
  keeps a chance hovering around 50% from alerting each way on every refresh), and makes the forecast it
  alerted on the new baseline. Its text says what changed ("The forecast for tomorrow has changed: was 24°C and dry, now 19°C with
  rain likely"), not "since I sent it", which only a client could say.
- **Only listening clients get alerts.** Most chat clients don't keep an MCP listen stream open: signalbot, which reads the feed, is the client that
  delivers them (a short script with the SDK, or Home Assistant, could too). Section 8.

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
- **And the periods it makes sense over:** `drawn_by.periods: [{period, label}]`, decided by the service for that chart, so a client
  offers only buttons that draw something worth having. A chart of readings over time: `1d` "1D", `7d` "7D", `1m` "1M", `3m` "3M",
  `1y` "1Y". A count or total per month (`days` with `group_by`): `3m`, `1y`, `on record` "All"; per year: `on record` alone, so no
  buttons. Rain against a reading (`relate`) and `scan`: `1m`, `3m`, `1y`. A chart that can't be redrawn over another period lists
  none.
- **Small enough for a model to read and redraw:** each line is bucketed to at most 150 points (the bands with it), so a model can turn
  it into a plot with its own tools and an application can draw it directly. An application that draws for itself asks for more with
  request `_meta` `chart_points` (at most 500, today's budget); the service's own pictures use 500, so its golden images don't change.
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
  turn*. A chat client can follow the rule; a client that doesn't (a desktop assistant) gives the result to its
  model as usual, which then has the reply ready to repeat. A result with no chart, an error or a note that needs the model's words is
  never complete.
- **Or as images, for a client that asks.** A client that shows pictures but can't draw JSON asks for the charts drawn: per connection
  with its MCP URL's query, `/mcp?charts=png` (any client can set a URL; not all can set headers), or per call with request `_meta`
  `chart_image: "png"` (or `"none"`, which overrides the connection's). Off by default: without it the result is as above, unchanged.
  With it, a result with charts carries its JSON text with each chart's data replaced by `{title, period}` and a `chart_note` that fits
  the image (the chart is the image attached: keep the text to the figures, don't describe it), then one `ImageContent` (`image/png`,
  base64) per chart, in order; the full chart JSON, each with its `drawn_by` (what the period buttons replay), is in result `_meta`
  `"ecobot/charts"`, for applications, and the theme in `"ecobot/chart_dark"`. A complete answer keeps its `reply` block for the
  person, after the images, and `"ecobot/complete"`. The pictures are the service's own: the same renderer (`lib/charts.render_data`, Pillow,
  run off the event loop), from every point (`chart_points` `"drawn"` unless the call says otherwise), so they and the golden images
  don't change; dark per `DARKMODE`, or request `_meta` `chart_dark: true|false`. Pillow is optional on the service
  (`requirements-charts.txt`) and imported at the first image asked for: without it the warning is logged once, and the result is the
  JSON as if no image had been asked for, with `chart_image_note` saying images aren't available on this service.
- **Or as links, for a client that shows a markdown image but not a tool's.** Some chat apps (LibreChat with an Ollama endpoint) give
  a tool's image to the model as text, but show a markdown image in the model's reply. Such a client asks with `/mcp?charts=link`, or
  per call `chart_image: "link"`. So there are three modes, the connection's from its URL (`?charts=png`, `?charts=link`, neither
  for none) and each call's `chart_image` (`"png"`, `"link"`, `"none"`) overriding it; off by default. With links the charts are
  drawn as for images (the same renderer, off the event loop; without Pillow, the same JSON and note) and kept in memory under a
  random, unguessable id (`secrets.token_urlsafe(16)`), for `MCP_CHART_LINK_HOURS` (24) and at most 500 pictures and 64 MB (the
  oldest dropped first): a restart forgets them. The result's text carries each chart as `{title, period, url, markdown}`
  (`markdown` is `![<title>](<url>)`) with a `chart_note` asking the model to put each chart in its reply as that markdown image,
  exactly as given, and keep its text to the figures; then one `ResourceLink` per chart (`uri` the URL, `name` the title,
  `image/png`), with no image bytes; result `_meta` `"ecobot/charts"` and `"ecobot/chart_dark"` as with images. A complete answer's
  `reply` (in the JSON and its block for the person) ends with the markdown images, so a client that shows the complete reply shows
  the chart. The pictures are served at `GET /charts/<id>.png` beside `/mcp`, with no token and any `Host` (read-only, like the
  dashboard; the id is the key: anyone with a link can open it until it expires), `Cache-Control: private` for at most the link's
  remaining life, and 404 once it has gone; the request log names them by the id's first characters. The URL is
  `MCP_CHART_BASE_URL`'s (where a proxy serves `/charts` to browsers, e.g. nginx's `location /ecobot-charts/ { proxy_pass
  http://127.0.0.1:8765/charts/; }`), else the MCP request's own scheme and host (`X-Forwarded-Proto`, `X-Forwarded-Host` when
  present) with `/charts`.
- **A client without a model** (signalbot's direct commands, which post a result as it comes) gets the caption too: with images or
  links made, an answer that isn't complete has its `caption` (or `text`) as a last text block for the person (`audience: ["user"]`),
  as a complete answer has its `reply`. Asking with the connection URL (`/mcp?charts=png`) is enough, as such a client sends no `_meta`.
- **A chat client** can have every chart it sends drawn by the service, as images (`chart_image: "png"`, with `chart_dark` for its
  own theme): each call the model makes asks for its pictures, and the model can be sent the text alone, the charts' titles, never an
  image. The Status picture is `ecobot://status`'s. The service draws for a client that draws nothing: a client that neither draws the
  chart data nor asks for images gets an answer without its picture.


### 3.8 Connecting

- **One program:** `python -m ecobot_mcp` runs the syncers, the alert rules and the server, at `https://<service>/mcp`
  (Streamable HTTP, `streamable_http_app`, under uvicorn behind TLS: a reverse proxy such as Caddy, or uvicorn's own certificate). The
  server is the SDK's low-level `Server`, so each tool's input schema is hand-written JSON with its description; the list is built when
  asked, so it follows the file's metrics. The app is built with `TransportSecuritySettings(allowed_hosts=MCP_ALLOWED_HOSTS)`: by
  default it answers only `Host: 127.0.0.1` or `localhost`, and refuses the name a proxy or a LAN client uses.
- **No stdio:** clients connect over HTTP (Claude Code: `claude mcp add --transport http ...` with the bearer header). A stdio bridge
  for clients that can only start a local command was built in step 5 and removed: nothing here needs it.
- **Auth:** the service's own `TokenVerifier` (the SDK has only the protocol): a constant-time compare against `AUTH_TOKENS`
  (`token,token,…`, one per client, so one can be revoked alone), passed with `auth=AuthSettings(issuer_url="https://<service>",
  resource_server_url=None)` (`issuer_url` is required but unused without an OAuth provider; no OAuth metadata is published). Bind to
  the LAN or a tailnet; nothing needs the internet to reach it.
- **A client that listens** (signalbot, an alert forwarder): `Client(…, mode="2026-07-28")`, pinning the protocol revision (the default
  probes, and a proxy's 502 during a restart can land it on the older revision, which cannot listen). A pinned client skips
  `server/discover`, so it calls it once itself (`session.send_discover`, then `session.adopt`) to get the instructions and
  capabilities. In the SDK a connection failure on any request ends the whole client, so one supervisor task owns it: it discards the
  client when it exits with a transport error or the listen is lost or ends, builds a new one after a backoff, discovers and listens
  again, and reads the feed again (`lib/upstream.Upstream` is that supervisor, which irrigation-mcp uses for ecobot-mcp). A read timeout of about 45 seconds (three of the server's 15-second pings) notices a
  silently dead link.


### 3.9 Irrigation, its own server

The irrigation controller (`lib/irrigation`: the Tuya client, the watchdog, the daily check) is its own MCP server, irrigation-mcp
(`python -m irrigation_mcp`, `irrigation-mcp.service`, its own Docker image `irrigation-mcp` under compose's `irrigation` profile),
built in step 8. It stays in this repository as its own package (decided November 2026: not a repository of its own), sharing
`lib/upstream.py`, the alert log and the token helpers (`lib/mcp_http.py`), and importing none of ecobot-mcp's sources
and tools (`requirements-irrigation.txt`: the MCP SDK, uvicorn, httpx). It listens at `http://IRRIGATION_BIND/mcp` (127.0.0.1:8766), with the same
token check (`AUTH_TOKENS`, or the generated token) and Host check (`IRRIGATION_ALLOWED_HOSTS`) as ecobot-mcp; one runs per data folder
(`irrigation.lock`); its state is `irrigation_state.json`.
- **Tools:** `irrigation_status` (`readOnlyHint`: `switch`, `countdown` seconds left, `weather_delay` and `paused`,
  `battery_percentage`, `work_state`, and the watchdog's `off_at` while a run is watched), `water {minutes: 1..60}` (refused while a
  weather delay is on, with an error that says so; the watchdog armed first), `stop` (the valve off, the watchdog disarmed), `delay
  {duration: 24h|48h|72h|cancel}`; the last three `destructiveHint`, so a client asks before it calls them. Each answers JSON as one text
  block, as ecobot-mcp's tools do; a refusal or the controller unreachable is `isError` with `{"error": ...}`, a message safe to show,
  a refusal with `"refused"` too (`"paused"` or `"arguments"`: a client can say why, not that it couldn't reach the controller).
- **The watchdog** (a run must end; a restart mid-run still closes the valve) and the daily check (pause when it rained or will) run in
  that server whatever any client does.
- **Rain and forecast** come from the weather service, as its client: the measured rain from `summary{readings: ["rain"], period: "last
  24 hours"}`, the forecast from `ecobot://forecast` (its days' chance of at least 1 mm and `fetched_at`). When the weather service is
  unreachable, the check runs on the forecast it last read and skips the measured rain.
- **Its alerts** (battery low, paused for rain: `irrigation_battery`, `irrigation_pause`) are its own `irrigation://feed`, in the same
  shape and with the same listen as `ecobot://feed`, from an alert log of its own (`irrigation_alerts.sqlite`, its own identity); a
  client listens to both servers.
- **The time zone** is ecobot://station's once read, `TZ` before. It reaches ecobot-mcp at `IRRIGATION_ECOBOT_MCP_URL`, sending the
  first of `AUTH_TOKENS` (else the generated token).
- **Clients** reach it as they reach ecobot-mcp, with the same token: a model client calls the four tools (`lib/irrigation/common.py`
  is what the server and its tests share, without the Tuya client), and signalbot reads `irrigation://feed`.


### 3.10 Configuration

`MCP_ECOWITT_*`, `MCP_HOMEASSISTANT_*`, `MCP_AIRGRADIENT_*`, `MCP_ANTISPIKE_REQUIRE_CONSECUTIVE`, `MCP_POLLEN*`, `MCP_FORECAST_*`, `MCP_PLACE`, `TZ` (until the
station names one), `DATA_DIR`, the alert settings (cooldowns, quiet hours, thresholds), `MCP_CHART_ALL_FEELS_LIKE`, `MCP_CHART_ALL_VPD`,
`AUTH_TOKENS`, `MCP_BIND`, `MCP_ALLOWED_HOSTS`. The irrigation server: `IRRIGATION_TUYA_*` (old names `BOT_TUYA_*`, then `TUYA_*`),
`IRRIGATION_CHECK_HOUR` (old `BOT_IRRIGATION_CHECK_HOUR`), `IRRIGATION_BIND`, `IRRIGATION_ALLOWED_HOSTS`, `IRRIGATION_ECOBOT_MCP_URL`, and
the shared `AUTH_TOKENS`, `DATA_DIR`, `TZ`, `LOG_LEVEL`. The Telegram bot's settings (`BOT_*`, `TELEGRAM_*`, `OPENROUTER_*`) are
not read; `scripts/migrate_env.py` removes them from an env file.

## 4. The Telegram bot (removed)

The bot was the service's first client, and the measure of each step: it kept answering in Telegram while its insides moved to the
service. It offered the model the service's tools as they were, ended the turn on a complete result (3.7), kept a few shortcuts for its
buttons (each a fixed call to a service tool), and listened for alerts to send to the chats subscribed to each kind, with a 🔄 that
replayed the chart's `drawn_by`. It was slimmed to a client of ecobot-mcp alone (step 6) and then removed (step 9), signalbot taking its
place: the contract it exercised (the tools' worded results, `drawn_by`, `ecobot://feed`) is what signalbot and every other client use.


## 5. What does not change

The readings file, its tables and its day rows; how each tool chooses its resolution and fetches what it lacks; the alert rules
themselves; the charts' content.

## 6. Steps

Each step shipped on its own, with the tests and evals green and the bot working the whole way (until step 9). The model's tool list changes once
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
2. **The ten tools, beside the twelve** (done). The tools of 3.3, built once with their arguments, periods, worded results
   (`day_look`, ready lines, captions), charts, descriptions and errors, in `lib/service/` (what they share: `common.py`, `names.py`,
   `daylook.py`, `context.py`; the outlook's rules in `outlook.py`), registered (`__init__.py`: `build`, `Service`), built in
   `ecobot.py` and handed to the bot; offered to no model yet. Contract tests per tool; every chart validates against
   `schemas/chart.json`, `drawn_by` included, and replays to the same chart and over each of its `periods` without a refusal. The
   evals have the service's expectations (`tests/evals/service_cases.json`, every case) and `scripts/eval_prompts.py --service` checks
   offline that the tools accept each expected call; `--live --service` asks a model with the tool list alone, and `--instructions`
   adds the server's instructions (`lib/service/instructions.py`) after the neutral line. First full runs (10 Nov 2026, one run each):
   deepseek-v4.1-flash 92/94 alone and 92/94 with the instructions, gpt-6-luna 89/94 alone. The gate's second round (the same day, after the period rule and the descriptions above): deepseek 90/94 alone and 90/94 with the instructions, gpt-6-luna 90/94 and 87/94; each failure asked 3 times more, those that failed 2 of 3 or worse fixed in the descriptions (wind 3m, a rating against rain, lowest and highest, air quality against rain, today against yesterday) and then passing 3 of 3. The bot's own code paths call the ten instead of the twelve: its shortcuts, report, lookups, Help, gauges and
   alert charts, through `lib/service/legacy.py` (the old call and the `Turn`'s inputs read as the service's call, in one place,
   deleted whole in step 3), quoting their words; its answers pinned before and after (`tests/test_service_switch.py`), unchanged but
   for the deliberate changes below. Its gauges (the dashboard, the report's three) are `current` with `context` and the gauges'
   readings named (the air sensor isn't read), laid out bot-side (`lib/gauge_sheet.py`); `Ecowitt.dashboard()` and `sheet()` are gone.
   It asks for its charts as drawn (request `_meta` `chart_points: "drawn"`), since a chart of each day's average over years has more
   points than 500. Period buttons replay the chart's `drawn_by` with a new `period` (the buttons, their keyboard and the picture
   cache stay in the bot; the service knows nothing of buttons), one button per `drawn_by.periods` entry with the service's label;
   when it lists none, a 🔄 that replays the call as it is if its period runs to now (through yesterday for a whole-day tool), else
   nothing; the chart's own shaded 1D, when it runs to now, draws it again with the latest readings (a longer period's does nothing);
   a replay the service refuses shows its refusal on the button, and one that fails after "Drawing ..." says so in the chat. What the
   bot draws ahead replays calls at background priority: the other periods after a chart is sent, the keyboard's charts every 5
   minutes, recent alerts' charts. The picture cache and the pre-upload are keyed by the call, so two wordings or two chats share a
   picture. Deliberate changes: a count per month offers 3M, 1Y and All (never 1D), per year a 🔄 (its period runs to now); whole-day
   `1y` is the 365 days to yesterday (364 and today before); alert charts name their source. Until step 3, a chart the model draws
   through the twelve gets a `drawn_by` from `legacy.py` when its call translates and lists periods; otherwise (`plot_chart`, the
   links and the scan, a period given as times) its buttons ask its question again, as before. Left for later: `current` and `outlook`
   read the sources' kept live readings through private members (`Ecowitt._live`, `AirGradient._current`); `fetch: false` doesn't stop
   the air sensor's inline fetch; `tests/report_words.py` keeps the old report's words as the tests' reference, deleted with the bot.
3. **The swap** (done), gated on the live evals (all cases, the production model and one other, with the tool list alone and with
   the instructions): the model gets the ten tools; the bot's prompt keeps only its persona; the twelve, the `Turn`'s inputs, the
   period hints, the prefetch and the corrections from the words go. The bot offers the ten as they are (`lib/model_tools.py`: each
   result kept whole for drawing and its buttons, the model sent it with each chart as its title and period and the bot's own
   `chart_note`: the bot sends the chart, the reply its caption), with its persona, the server's instructions and the time
   (`lib/prompt.py`); tool_choice is the model's own, the reasoning effort still comes from the words. Its shortcuts (`lib/intent.py`)
   are fixed calls built directly: a named reading's chart and a period (summary with the person's words for both, the keyboard's and
   the alerts' sentences among them; with no chart word, over more than a day or a day given as a number, "co2 1d"), weather now, the
   report (current, pollen, 3 days' forecast) and the gauges; everything else it answered in code goes to the model. The turn
   ends on a step whose every result is complete. The live-chart link goes under an answer whose calls read the air (`bot.reads_air`),
   and the forecast days shown, by `forecast` or `outlook`, are recorded for the forecast alert. `lib/service/legacy.py` is gone; the
   `Turn` is only the engines' options, set by the service from its arguments. `--live` asks the model as the bot does (its prompt,
   the ten tools; a question answered in code is scored by its fixed calls). The gate, one run each of 94 cases (DeepSeek v4.1 flash,
   the production model, and gpt-6-luna): the bot as it runs 90 and 92 (today's bot with the twelve: 90); the tool list alone 91 and
   92; with the instructions 91 and 93. What fails is mostly a model leaving `group_by` out of a figure per month or year (the twelve
   had it filled in from the words): `days` says so in its result (`note_group_by`) and the model calls again; the rest are one-off or
   refusals a model corrects in its next call. Expectations are widened where another call answers as well, not the descriptions
   tightened. The instructions say every question is fetched afresh, a follow-up or a challenge too.
4. **Alerts in the service** (done). The rules, their state and config move service-side and append events to the alert log, with
   `expires`, `link` and `chart` (with its `drawn_by`); the forecast alert gets its station-wide baseline. The bot delivers from the log.
   - **The log** (`lib/alert_log.py`): 3.6's events, `since(id, expired=False)`, `get(id)`, `last_id()`, and `wait(after)`, an asyncio
     notification on each append that step 5's listen will use. Its own SQLite file in `DATA_DIR` (`alerts.sqlite`), not the readings
     file: it must exist whatever sources run (pollen and the forecast need no readings file), it is written in tiny rows from the event
     loop while the readings file takes bulk writes from worker threads, and it can be looked at or deleted alone. Ids are
     AUTOINCREMENT, so they never go back; events older than 30 days are dropped when it opens. Times are ISO local with their offset.
   - **The rules** (`lib/alerts/`: weather, particulates, pollen and asthma, forecast) append events and know nothing of chats or
     Telegram; `lib/service/alerts.py` (`AlertRules`) starts them on today's triggers. Their texts are today's, byte for byte, but the
     forecast's. `expires`: rain (started, stopped, the overnight summary) an hour, gusts and UV their rearm time (an hour), the
     temperature crossing its cooldown, particulates the next check (30 minutes), pollen the end of the day it is about, a forecast the
     end of the day it is about. `link`: the AirGradient dashboard (particulates), the pollen site. `chart`: the alert chart as the
     service gives it (`pictures.chart_data`: the button sentence's `summary` call, every point as drawn, as the bot's pictures were),
     `drawn_by` included; made when the alert fires, whoever is listening (before, only when some chat got the alert). `data`: the
     figures behind each.
   - **Their state** is the service's (`lib/alerts/store.py`, `alert_state.json` beside `bot_state.json`): the first start with no
     `alert_state.json` moves the rules' keys out of `bot_state.json`'s `monitor` (once), so nothing re-fires after the upgrade; the
     irrigation controller's stay in the bot's file. **Config**: the quiet hours and the cooldown stay `MCP_RAIN_QUIET_HOURS` and
     `MCP_ALERT_COOLDOWN_MINUTES`, handed to the rules (and `about`) by the service; the thresholds stay the rules' own constants, which
     `about` reads, as no setting changes them today.
   - **The forecast alert is station-wide** (deliberate): the first forecast fetched from 6 am is the day's baseline for today and
     tomorrow (no alert); a later one alerts, one event per day, when the high moves by more than 2°C or the chance of at least 1 mm
     rises from under 50% to 50% or more, or falls from 50% or more to 30% or less, and becomes that day's baseline. The 30% band is
     kept: without it a chance hovering around 50% (rounded to 5%, refetched every 15 minutes) alerts likely, unlikely, likely through
     the day. Its text says what changed: "🔄 The forecast for tomorrow has changed: the high is now 19°C (was 24°C) and rain is
     now likely (80%, was 10%).\nNow: 🌧️ Rain. 10–19°C, 80% chance of rain". The bot no longer records the forecast days each answer
     showed (`record_forecast`, `forecast_sent`, `shown` through the turn): nothing needs them, and the old record is dropped.
   - **The bot delivers** (`lib/alerts/notify.AlertDelivery`): it waits on the log in process, sends each unexpired event after the last
     id it handled (kept in `bot_state.json` as `alerts_read`, with the log's identity as `alerts_log`, so a log removed or replaced
     starts it afresh after that log's newest, with a warning, rather than waiting for the new ids to pass the old) through the
     Notifier as before (the chats subscribed to its kind, silent unless the chat turned sound on, the bell, the link as the footer),
     with the chart drawn from the event's data, uploaded once. Its
     first start begins after the newest event; after a gap it skips what expired. The 🔄 under a chart names the event
     (`al:refresh:<kind>:<id>`) and replays the chart's `drawn_by` from the log (a chart drawn ahead since the message changed is still
     sent first); an alert sent before the log, or one dropped from it, draws its type's chart as before. Irrigation alerts are sent by
     the bot as before.
   - Tests: the rules' tests assert events appended (`tests/test_alerts.py`), the log and the delivery (`tests/test_alert_log.py`), the
     forecast baseline (`tests/test_forecast_alert.py`).
5. **MCP, and the service on its own.** `lib/mcp_server.py` serves the tools, resources and prompts (the bus and listen handler, the
   transport settings, the token check); the service starts as its own program (`python -m ecobot_mcp`, its compose file); the bot becomes a client over the network (the supervisor, discover for the instructions, `charts` from results, the
   alert log; `schemas/` goes into the image, which `.dockerignore` keeps out today). `ecobot.py`'s `warm_up()` is split: the first
   fetch, the archive, the backfill, the Home Assistant imports and the startup line go to the service; the bot keeps only its charts
   drawn ahead, as `summary` calls at background priority, failing quietly until the service has warmed. The bot's poke of the sources
   when a question arrives goes: the service's keep-warm and the tools' own fetches cover it. Then the irrigation server is built in its own
   repository against it, the bot connects to both, and `lib/irrigation` goes from here. The service's scripts (`edit_readings`,
   `cache_status`, `refill_airgradient`, `check_rain`, `bench_storage`, `conditions`, `ecowitt_metrics`, `dump_specs`) stay;
   `show_request` becomes an MCP client (`report_inputs`, what the report written in code was made from, went with it in step 6c).
   - **The service side (done).** `lib/mcp_server.py` (`EcobotServer`): the SDK's low-level `Server` with the ten tools (their
     hand-written schemas, `readOnlyHint` and `openWorldHint` from each ServiceTool, the list built from the current catalogue; a call
     runs the ServiceTool with the request's `_meta` and answers its JSON unchanged as one text block, `isError` for a refusal, a
     complete answer with its `reply` as a second block for `audience: ["user"]` and result `_meta` `{"ecobot/complete": true}`),
     3.4's resources and the alerts' template, 3.5's prompts, the instructions, and `subscriptions/listen` (`InMemorySubscriptionBus`,
     `ListenHandler`): `ResourceUpdated` for the live resources when what the sources keep changes (compared every 2 s, nothing fetched
     for it; a reading asked for again and unchanged is not sent), the alerts on each new event (`AlertLog.wait`), the station and
     `ToolsListChanged` when the file's catalogue gains a reading (read every minute; the tools and instructions are then built again).
     `http_app`: `streamable_http_app` with `TransportSecuritySettings` over `MCP_ALLOWED_HOSTS` and the `TokenCheck` verifier over
     `AUTH_TOKENS` (`AuthSettings(issuer_url=…, resource_server_url=None)`). `ecobot_mcp/`: `core.py` is the service's startup
     (`start_sources`, each source started from what the data folder kept, nothing asked of the network before the port opens; `Core`: the alert log, the tools, the alert rules, the keep-warm, and `warm_up`, the first fetch (the station's device list in the background, its live reading first, then its history, the 5-minute and 30-minute tails first, a fetch a question waits for hurried to the urgent line; the air sensor's reading and the rest of its start; the forecast and pollen page if due), the weather
     and particulates rules once their device has been read, the live poll, the archive, the backfill and Home Assistant's imports, each loop's last success and failure kept for
     `ecobot://health`), which `ecobot.py` now calls in-process, its behaviour unchanged; `__main__.py` runs it with the server under
     uvicorn (`MCP_BIND`; at first it refused a non-loopback address without tokens, and now it always has one, `AUTH_TOKENS` or the generated one); `lib/upstream.py` is the bot's one supervised
     client, pinned to 2026-07-28, that discovers by hand, listens and re-listens after a backoff (a stdio bridge was built on it and later removed). The image ships `ecobot_mcp/` and
     `schemas/`; compose has `ecobot-mcp`, off unless its profile is asked for. Tests: `tests/test_mcp_server.py` (in memory) and
     `tests/test_mcp_listen.py` (uvicorn on a localhost port: the lost listen and the reading after the last id, auth, hosts, the service started with the fakes and called through a proxy's Host name). Settings: `Config.from_env(bot=False)` needs
     no Telegram token.
   - **The bot as a client (done; removed in step 9).** The bot connected to ecobot-mcp over MCP through `lib/upstream.Upstream` (pinned to
     2026-07-28, `send_discover` and `adopt` for the instructions, one supervisor task that drops the client on a transport error or a
     lost listen, rebuilds it after a backoff and reads the alerts after its last id); 3.8 keeps those rules for any listening client.
     The service's side is the alert log, the alert store and the resources of 3.4.

   - **Chart images (done).** The service draws a result's charts as PNGs for a client that asks (3.7: `?charts=png` on its URL, or
     request `_meta` `chart_image`; `chart_dark`; the full JSON in result `_meta` `"ecobot/charts"`, the theme in `"ecobot/chart_dark"`),
     with `lib/chart_images.py`, which imports Pillow and the renderer only at the first image (Pillow is `requirements-charts.txt`;
     without it, the JSON with a note and one warning). `lib/weather.py` gains `chart_image(name, args, meta)` and `picture(chart)`
     (`ChartImage`: the PNG, the chart's JSON, the theme): `LocalWeather` draws in-process with `lib/charts.render_data`,
     `RemoteWeather` asks the service and takes the PNG from the `ImageContent` and the chart and theme from `_meta` (an alert's
     chart by replaying its `drawn_by`; a service that sends no image has it drawn here). The bot's `_draw_call` (charts drawn ahead,
     preloads, the 🔄 under an alert), the period buttons and the alerts' pictures (`lib/alerts/pictures.py`, `AlertDelivery`) use it;
     the image cache keeps the theme the service drew in. The model path is unchanged; a question's charts and the dashboard are still
     drawn by the bot. Tests: `tests/test_mcp_server.py` (in memory and under uvicorn: off unchanged, the URL's `?charts=png`, the
     per-call override both ways, without Pillow, no Pillow imported by the server), `tests/test_bot_remote.py` (the service's PNG
     byte for byte the bot's own, for a call and an alert's chart). Then **chart links** (3.7, `?charts=link`, for LibreChat and other
     chat apps that show a markdown image in the reply but not a tool's image): the same PNGs kept in memory and served at
     `/charts/<id>.png` (`chart_images.Links`, `MCP_CHART_BASE_URL`, `MCP_CHART_LINK_HOURS`), the text with each chart's `url` and
     `markdown`, a `ResourceLink` per chart. Tests: `tests/test_mcp_server.py` (in memory and under uvicorn: the link served byte for
     byte as png mode's image, without a token or an allowed Host, `/mcp` still needing both; the override both ways; 404 for an
     unknown or expired id; the base URL from the setting and the proxy's headers; the store's bounds; the bot unaffected).
   - **Left:** the irrigation server, now step 8; and `show_request` as an MCP client.
6. **The slim bot** (done November 2026; removed in step 9). The Telegram bot was kept for a time as a thin client of ecobot-mcp: it did what
   only a Telegram bot can do (chats, buttons, pushing alerts), and everything about the weather, the air, the alerts' rules and the
   pictures came from ecobot-mcp over MCP. In sub-steps 6a (every picture drawn by ecobot-mcp), 6b (a client only, with no sources of
   its own) and 6c (less code in the bot: everything typed went to the model with ecobot-mcp's tools) it lost its renderer, its
   sources and its shortcuts. What it needed of the service is what 3.2 to 3.8 describe; its code, tests and documentation are gone.

7. **Faster: the storage, and the test suite.** The suite (then 1793 tests, now 1454, `python -m pytest -q`, 4 workers) takes 46-49 s with the fake
   archive cached (about 65 s when it is rebuilt after a change to lib/storage, lib/ecowitt or tests/fakes.py), against a wish of
   20 s. Its setup is cheap now (a test's copy of the archive and its station: 30 ms); the time is the tests' own work, about 154 s of
   it, much of it the storage code the service runs for real questions, so speeding that up speeds up both. From a profile of the
   whole suite run serially (November 2026), each its own change, every test and assertion kept:
   - **7a. Rows into columns in one step** (`lib/storage/read.py` `rows`: 12 s, 42 million list appends): `itertools.groupby` over
     the rows by sensor and `zip(*rows)`, in C. Written and passing the storage tests; set aside until this step.
   - **7b. The queries** (21 s in SQLite over 94,000 queries): the largest is writing day rows (`lib/storage/days.py` `_insert`,
     13 s over 12,800 queries), then the roll-ups (`write.py` `roll_up`, 2 s) and `read.py` `part_days` (1.5 s). EXPLAIN QUERY PLAN
     each for a table scan or a missing index, and look for the same work done twice within one write or one question.
   - **7c. Closing the file** (4.5 s over 3,000 closes): likely a WAL checkpoint at each close; any change here touches how writes
     are made durable, so it needs care.
   - **7d. Python loops over every reading** for the day figures and summaries (`lib/lines.py` `_summarise`, `lib/ecowitt/reads.py`
     `readings_on`: about 10 s): the riskiest, as they make the figures people see; checked against a full-size archive before and
     after, not only the tests.
   Measured after each. Also waiting (about 42 s of the serial run on timers, servers stopping and a browser; the browser test alone is
   11 s). If still short of 20 s after 7a-7d: mark the heaviest groups (the browser tests, the reference images, the benchmark, the
   tests that start servers) `slow`, so plain `pytest` leaves them to `pytest -m slow` and CI, which runs all. The hangs are fixed and
   a stuck test fails after 60 s (pytest-timeout).

   *Done* (measured warm, four workers; serially the tests' own time was 137 s):
   - 7a as written: 48-50 s to 44.9 s.
   - 7b: every query reads by the primary key (EXPLAIN QUERY PLAN: no table scan, no missing index); the day rows' cost is the work
     itself, done after every write a test makes. Nothing changed.
   - 7c: closing a copy of the archive takes 3 ms (the `PRAGMA optimize` and the close); in the profile of the heaviest test files
     closes were 0.06 s. Nothing changed.
   - 7d: `daily_summary` places each reading in its day by the day's span (`lines.day_of`, the same day as `local_date` in every zone,
     tested), the station's readings rounded inline (`ecowitt/reads.py`) and a row's column names worked out once (`write.py` `put`): a
     year of 5-minute readings' days in 0.010 s instead of 0.062 s; within the noise for the suite (45.4 s).
   - The fallback: the 60 tests of 0.5 s or more (`tests/slow_tests.txt`, written by `scripts/slow_tests.py`) are marked `slow` and
     left out of plain `pytest`: 1731 tests in 19 s. `pytest -m slow` runs them (32 s), `pytest -m ""` everything (about 45 s), as CI
     does.
8. **Irrigation on its own** (was 5b): the irrigation server as its own package, MCP server and Docker image in this repository (3.9).
   *Done.*
   - **8a (done):** irrigation-mcp (`irrigation_mcp/`, `lib/irrigation/server.py`): its tools, the watchdog and the daily check
     (`lib/irrigation/check.py`, moved from `lib/alerts/irrigation.py`) as ecobot-mcp's client (`lib/irrigation/ecobot.py`),
     `irrigation://feed`, its state, its settings (`IRRIGATION_*`, a setting may have several old names, `scripts/migrate_env.py` too),
     its image (`--target irrigation-mcp`, compose's `irrigation` profile). What the two servers share over HTTP moved to
     `lib/mcp_http.py`.
   - **8b (done; the bot's side removed in step 9):** the bot connected to it as a second server and delivered its alerts as ecobot-mcp's.
     Packaging: `irrigation-mcp.service` (wants ecobot-mcp's, after it), compose's host-network and journald files cover irrigation-mcp.
     The controller's own tests (Tuya, the check, the watchdog) are `tests/test_irrigation_controller.py`.
9. **The bot removed** (done November 2026; signalbot replaced it). Deleted: `ecobot.py`, the bot's modules (`lib/bot.py`, `llm.py`,
   `prompt.py`, `intent.py`, `model_tools.py`, `weather.py`, `irrigation_remote.py`, `irrigation_menu.py`, `buttons.py`, `entities.py`,
   `admins.py`, `templates.py`, `periods.py`, `alerts/notify.py`, `alerts/menu.py`), its tests and the evals that drove a model (the
   example calls in `tests/evals/service_cases.json` stay, checked against the tool descriptions), `requirements-bot.txt`, `docker/bot.env`,
   `ecobot.service`, the `ecobot` image and compose service, the live-evals and Telegram-notification CI jobs, the bot's settings
   (`BOT_*`, `TELEGRAM_*`, `OPENROUTER_*`), the one-off copies from `bot_state.json`, and the resources `ecobot://alerts` and
   `irrigation://alerts` (the feeds are the alert resources). The servers' tests use the SDK's client (`tests/mcp_client.py`), and
   `tests/test_server_imports.py` keeps the servers' import graph free of any chat or model client.


## 7. Testing

- **The tool descriptions are checked against example calls.** `tests/evals/service_cases.json` holds a call for each routing rule (a
  record or a day, a chart or a figure per month, `outlook`, `about`, `chart_answers_it`, each periods form); the tests check that every
  tool's schema and description are well formed, within their length caps, and that each example call is answered, not refused. (Until
  step 9, evals asked real models which tool they pick from the tool list alone and with the instructions, against the production
  model and at least one other, so the interface was not tuned to one model's habits; those runs went with the bot.)
- **The contract:** a test per tool and resource for the arguments it takes and the shape it returns (`test_contract`); the periods
  table at its fixed times (step 1); charts against their schema; every chart's `drawn_by`, replayed, draws the same chart,
  and with only `period` changed to each of its `periods`, the same chart over that period, never an error; `day_look` against its rule.
- `days.hours` gives, for a stretch that is the whole day ("00:00-24:00"), the same figures as its day row for every day held whole at
  5 or 30 minutes (the air sensor's at 5 minutes or an hour), a clock-change day included; a day from a daily record, or from whatever
  readings there were, is left out and counted.
- **Alerts:** the rules' tests; the log keeps order and survives a restart; a client that reconnects reads the feed and gets what is
  unexpired, and nothing that has expired; the forecast alert's baseline.
- **A client that listens:** against the real app under uvicorn on a localhost port the test stops and starts (not in memory: a
  connection can't fail there, and the in-memory HTTP transport buffers a listen stream): the listen is lost, the client listens again
  and reads the feed again.
- **A deploy check:** a tool call through the proxy's name succeeds (the allowed hosts and the token).
- **Chart images:** off, a result is as it was; asked by the URL (under uvicorn) or per call, each chart comes as a PNG byte for byte
  the service's own render, its JSON in `_meta`; a call overrides its connection both ways; without Pillow, the JSON with a note and one
  warning; importing the server imports no Pillow.
- **The servers' imports:** `tests/test_server_imports.py` walks the import graph of both servers and starts them in a subprocess: no
  chat or model client is imported.


## 8. Risks

- **Alerts reach only listening clients.** With alerts over MCP only (3.6), nobody gets them unless a client that listens is running:
  signalbot, which reads the feeds, or another.
- **Charts only where a client can draw.** A client that neither draws the chart data nor lets its model draw gets the figures without
  a picture.
- **Models other than ours.** A client's model may be weaker, or tuned differently, than the ones the descriptions were tried on; some clients
  never pass the instructions on. The descriptions and results as the contract, and the example calls the tests check them against,
  are the defence, and every answer still rests on figures the model quotes, not works out.
- **Every question through a model.** Without shortcuts in code each question costs at least one model call; that is the client's
  choice and cost. A chart that answers the question costs only one where the client ends the turn on a complete result (3.7).
- **A chart sent without the answer.** If a model sets `chart_answers_it` on a question that needed words, the person gets the chart
  and its figures but not the answer: the description limits it to a question that is only the chart, the eval pair checks both
  sides, and a follow-up goes to the model as usual.
- **A protocol still settling.** `subscriptions/listen` is the 2026-07-28 revision; listening clients pin it, and the SDK is upgraded
  on both sides together.
