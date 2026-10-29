# Storage redesign plan

The plan for replacing the bot's five SQLite files with one, laid out so that a chart, a report or a day count reads what it needs in one
or two queries, and does not then sort it out row by row in Python. It turns the requirements in [TODO.md](../TODO.md) into a design, a
migration and a sequence of steps, each shipped on its own.

## 1. Where the time goes now

What typical requests cost on the bot's own caches in October 2026 (`ecowitt_cache.sqlite` 27.6 MB, 1.71 million readings, 116, 391, 755
and 1457 days held at 5 minutes, 30 minutes, 4 hours and a day; `airgradient_cache.sqlite` 3.9 MB, 53,000 readings; the live ranges
0.3 MB; the minutes 0.5 MB), measured by `scripts/bench_storage.py` in the container: each request run three times on a copy, the median
shown, nothing asked of the network. A time is the data and the answer, or the chart's spec; drawing and sending come after it (the DEBUG
`Chart ms` line has those).

| Request | SQL statements | Thread hops | Time |
|---|---|---|---|
| Temperature chart, 1D | 6 | 6 | 20 ms |
| Temperature chart, 7D | 31 | 23 | 29 ms |
| Temperature chart, 1M | 42 | 30 | 107 ms |
| Temperature chart, 3M | 53 | 39 | 272 ms |
| Temperature chart, 1Y | 86 | 76 | 1,205 ms |
| Rain chart, 3M | 39 | 29 | 218 ms |
| History of outdoor and indoor, 7 days, no chart | 51 | 24 | 46 ms |
| `weather_days`: days over 30°C, 4 years | 13 | 1 | 725 ms |
| `weather_days`: rain per month, a year | 9 | 1 | 609 ms |
| `weather_link`: pressure and rain, 90 days, chart | 4 | 1 | 98 ms |
| The alerts' last 3 hours | 16 | 3 | 6 ms |
| `/dashboard`, first time in a day | 122 | 3 | 3,146 ms |
| `/dashboard`, Refresh | 73 | 2 | 35 ms |
| Air chart (PM2.5), 1D | 4 | 3 | 12 ms |
| Air chart (PM2.5), 7D | 4 | 3 | 55 ms |
| Air chart (PM2.5), 1M | 4 | 3 | 200 ms |
| `air_days`: days PM2.5 over 25, 4 years | 3 | 1 | 662 ms |

Each chart also asked once for its newest hour, which is still settling and which the running bot holds in memory. The minutes play no
part here: the bot keeps only the last 9 hours of them, and only a chart of up to 8 hours reads them (the benchmark has had a 3-hour chart
since, which reads them in two statements).

Between the owner's runs, a step is measured on `tests/bench_cache.py`: a data folder shaped like this one, built from the fake station
in 20 seconds. Its numbers are of the same size: the 1Y chart 1.9 s, the first dashboard 2.9 s, `weather_days` 0.6 s, and the rain chart
0.45 s, because its station reports more rain counters than the owner's. A profile of the slow requests on it:
- **The 1Y chart** is read from the 30-minute readings, now that the cache holds more than a year of them (`HistoryQuery.held`): every
  half hour of every outdoor reading with its low and high (in the sample 18 series, 315,000 values). Each value is filed into dicts
  (`load_fields`, `collect`), folded into its month (`_monthly_extremes`) and boiled down to a point a day for the line (`build_line`).
  Then each month's record is looked for again in finer readings (`_refine`), a coverage check and a read at a time: most of its 76 hops.
  A cache with less than a year of 30-minute readings answers from the daily records instead, in about 30 ms.
- **The first `/dashboard` of a day** works out every day of the year so far for 12 readings, from the 5-minute readings (116 days) and
  the 30-minute ones: about 800,000 readings, each turned into its local date (`days.day_figures`, `local_date`) to find its day's high
  and low. Refresh keeps those days and reads only yesterday and today.
- **`weather_days` and `air_days`** do the same over four years (143,000 local dates for "days over 30°C", which shows each day's
  temperature and rain), and `air_days` runs the spike filter over every reading first (`metrics.without_spikes`).
- **The 3M and 1M charts and the air charts** read every reading of the period, at 30 or 5 minutes, before the line is boiled down to its
  500 points.

In all of them SQLite itself, running the queries and handing back their rows, takes a fifth of the time or less. The rest is Python handling
the rows one at a time: the text of each value turned into a float, a dict entry per value, a local date per reading.

What makes the query count high is that a chart's parts live apart:
- **Three files.** The values are in `ecowitt_cache.sqlite`, the bot's own ranges in `ecowitt_live.sqlite`, the minutes in
  `ecowitt_minutes.sqlite`. The newest readings are only in memory (`HotStore`), because the file holds only settled readings.
- **A reading per row.** Each field is a series of its own, and its low and high are two more (`temperature_low`, `temperature_high`).
  A group is read with two queries (units, then values), so a group of 6 fields with ranges is 18 series.
- **Coverage first.** Each read asks the coverage table what is held before it reads the readings.
- **Bands from elsewhere.** A 30-minute reading with no range of its own gets one from the 5-minute readings at read time
  (`slot_ranges`), and the live ranges are read per series (`LiveRanges.between`, `rolled`).
- **Refining.** A record (the period's high and low) found in 30-minute or daily readings is looked for again in finer readings, a window
  at a time (`HistoryQuery._refine`), each window a coverage check and a read.

## 2. What it has to do

From [TODO.md](../TODO.md):
- **Quicker, with fewer queries:** most requests one query, a chart a handful at most, and one thread hop.
- **Alike data together:** a sensor's readings that go together in one row per time, so one range scan returns all of them.
- **Samey data contiguous:** a sensor's readings at one resolution next to each other, in time order.
- **Short-lived minute data:** its own retention, deleted in batches, `auto_vacuum` off.
- **Several sensors of one kind:** two stations, an indoor and an outdoor AirGradient, Ecowitt's extra channels.
- **Rollback:** the old files are copied, never changed or deleted, so the previous version still runs on them.
- **Nothing visible changes:** the same answers, the same charts (`tests/charts` and the evals pass unchanged), measured before and after.

## 3. Design

### 3.1 One file

`ecobot.sqlite` in `DATA_DIR`, in WAL mode, with `synchronous=NORMAL` (in WAL mode a power cut can lose the last few commits but never
damages the file, and a commit no longer waits for the disk). Its tables:

| Table | Holds |
|---|---|
| `meta` | the schema version (`PRAGMA user_version` too), the station's time zone, what was migrated from where |
| `sensors` | one row per sensor: what it is and where its readings come from |
| `fields` | the fields each sensor has sent at each resolution and their units, as the source writes them (the old `series` table without its ids) |
| `coverage` | which times are held, per sensor and resolution |
| `climate`, `pressure`, `wind`, `rain`, `light`, `air` | the readings, one table per kind of sensor (3.3) |
| `climate_day` ... `air_day` | each sensor's local days: mean, lowest and highest and when (3.5) |
| `extra` | anything a source sends that the catalogue does not know yet (3.3) |
| `snapshots`, `snapshot_meta` | the pollen page and the forecast, as now (`lib/snapshots.py`), moved in unchanged |

`bot_state.json` stays as it is: it is the chats' settings, not readings.

### 3.2 Sensors

```sql
CREATE TABLE sensors (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,   -- climate, pressure, wind, rain, light, air
    role     TEXT NOT NULL,   -- the name the code and the model use: outdoor, indoor, piezo, ch1, air ...
    label    TEXT NOT NULL,   -- shown on charts and in answers: "Outdoor", "Indoor", "Garage", "AirGradient"
    device   TEXT NOT NULL,   -- the device: the station's MAC, the AirGradient location or serial
    external TEXT NOT NULL,   -- the source's name for it: Ecowitt's group ("outdoor", "rainfall_piezo", "temp_and_humidity_ch1"),
                              -- a Home Assistant entity prefix
    UNIQUE (device, external));
```

Ecowitt's groups are sensors: `outdoor` and `indoor` are two climate sensors of one device, `rainfall` and `rainfall_piezo` two rain
sensors, and the extra channels (`temp_and_humidity_ch1` ...) are more climate sensors. An AirGradient is an air sensor. The code keeps
naming series `role.field` ("outdoor.temperature"), so most of `HistoryQuery`, the charts and the captions are unchanged by this; a second
station or AirGradient gets roles of its own ("garage", "bedroom"). The schema supports several sensors from the start. The product side
(settings for a second AirGradient, finding Ecowitt's channels, a `sensor` argument for the tools, the prompt, alerts per sensor, one
sensor per Home Assistant entity prefix) is a later step (section 6, step 6).

### 3.3 The readings tables

One table per kind, keyed by sensor, resolution and time, `WITHOUT ROWID`, so each sensor's readings at one resolution are one run of
pages in time order, and a new reading goes on the end of its run:

```sql
CREATE TABLE climate (
    sensor INTEGER NOT NULL, res TEXT NOT NULL, ts INTEGER NOT NULL,
    src  INTEGER NOT NULL,   -- where the values came from: the cloud, Home Assistant, the bot's own readings (which win)
    final INTEGER NOT NULL,  -- 0 while the readings may still change (newer than the cycle's settle horizon)
    "temperature" REAL, "temperature_lo" REAL, "temperature_hi" REAL,
    "humidity"    REAL, "humidity_lo"    REAL, "humidity_hi"    REAL,
    "dew_point"   REAL, ..., "feels_like" ..., "app_temp" ..., "vpd" ...,
    PRIMARY KEY (sensor, res, ts)) WITHOUT ROWID;
```

- **The catalogue** (`lib/storage/catalogue.py`). The columns come from one table in code: each reading's kind, column, decimals, how a
  stretch of it is told as one (mean, highest, last) and whether it takes a band. The DDL is generated from it, and a reading added to it
  later is an `ALTER TABLE ... ADD COLUMN` at the next start, which SQLite does at once. A column is named as the source names the field
  (`temperature`, `1_hour`: quoted in the SQL), so the code keeps naming a series `outdoor.temperature`; indoor's `app_tempin` shares the
  `app_temp` column.
- **Kinds.** climate (temperature, humidity, dew point, feels like, apparent temperature, VPD), pressure (relative, absolute), wind (speed,
  gust, direction), rain (rate, daily, event, 1_hour, weekly, monthly, yearly), light (solar, UV), air (PM1, PM2.5, PM10, CO₂, VOC, NOx,
  the sensor's own temperature and humidity). Ecowitt's own air sensors (WH45 and the like) would be air sensors too. A group the catalogue
  does not know (a channel sensor) is a sensor of kind `other`, all its readings in `extra` until it has columns.
- **Values are REAL**, written back out with the reading's decimals (as the owner's station writes them: humidity and UV as whole
  numbers), so an answer shows "12.3" as it does now. No text to parse on reading. The migration counts the values whose text would come
  back otherwise, per field, so the catalogue's decimals are checked against the real cache before anything uses them.
- **A reading's low and high are in its row** (`_lo`, `_hi`), not series of their own: Ecowitt's 30-minute and coarser lows and highs, and
  the bands the bot and Home Assistant add (3.6). A null costs a byte, so a row without a range costs almost nothing.
- **`res`** is the resolution: `1min`, `5min`, `30min`, `4hour`, `1day` (Ecowitt's UTC days) and `1hour` (AirGradient's older history).
- **`src`** says which source a row is from, so the bot's own AirGradient readings are never replaced by the cloud's (today's `recorded`
  table), and a cloud reading fills only what the bot lacks.
- **`extra`** (`sensor, res, ts, field, value`) keeps a field Ecowitt sends that the catalogue has no column for, so nothing is lost
  before the catalogue learns it (today's cache keeps any field it is sent).

### 3.4 What each resolution holds

| `res` | From | Kept |
|---|---|---|
| `1min` | Home Assistant's states (the station, the AirGradient), the bot's own minute reads of the AirGradient | 10 days (6.1) |
| `5min` | Ecowitt's cloud, Home Assistant, the bot's own AirGradient averages; bands from the live readings and Home Assistant | for ever (the archive) |
| `30min`, `4hour`, `1day` | Ecowitt's cloud (lows and highs included), Home Assistant | for ever |
| `1hour` | AirGradient's cloud, for the days it no longer has at 5 minutes | for ever |

### 3.5 Days: each sensor's local days, worked out once

Most of the slow requests ask about days: the dashboard's daily, monthly and yearly highs and lows, `weather_days`, the daily breakdown of a
history answer, and the records of a period. So each sensor's local days are kept, worked out from the finest readings held for each day:

```sql
CREATE TABLE climate_day (
    sensor INTEGER NOT NULL, day TEXT NOT NULL,   -- the local date, 2026-10-29
    source TEXT NOT NULL,      -- the resolution the figures come from: 5min, 30min, or 1day (Ecowitt's daily record: approximate)
    timing TEXT NOT NULL,      -- the resolution the times of the lowest and highest come from (4hour for most daily records)
    complete INTEGER NOT NULL, -- the source holds the whole local day (not so today, or at the edge of what was fetched)
    n INTEGER NOT NULL,        -- the readings counted
    temperature_mean REAL, temperature_lo REAL, temperature_lo_ts INTEGER, temperature_hi REAL, temperature_hi_ts INTEGER,
    humidity_mean REAL, ...,
    PRIMARY KEY (sensor, day)) WITHOUT ROWID;
```

- **What is done now, once** (`lib/storage/days.py`). The source follows `days.day_values`: the 5-minute readings where the whole local
  day is held at 5 minutes (even if the station sent none: that day has no row), else the 30-minute ones, else Ecowitt's daily record
  labelled with the local date its 10am start falls on. A day none of those hold (today) is worked out from what there is and marked
  incomplete. A day's highest is the highest of each reading's own range where it has one, else of its value (the rain counter's highest
  is the day's total); its mean is the mean of its half hours' means, as the charts' daily points have it. A day of 5-minute readings
  takes in its 30-minute readings' lows and highs too (Ecowitt's, from readings finer than its 5 minutes, which its 5-minute ones miss:
  what refining a period's records kept), never in its mean. The tests check every day of the fake station's archive against
  `day_values` (over 5,000 figures, all equal: the fake's half hours hold nothing its 5 minutes miss).
- **When the low and high happened.** A day row keeps the time of its low and high, so a period's records come from its day rows (the
  first time, where one is reached on several days or several times in a day). In a day of 5-minute readings it is the first half hour
  that holds the day's high, at its own highest 5-minute reading. A daily record's day takes the times from the 4-hour readings of its UTC
  day where they are held ("sometime between 2pm and 6pm"), its figures staying the record's. The part-days at either end of a period are
  worked out from the finest readings held for them by the same SQL (`Reader.part_days`, its windows given as `VALUES`: one statement).
  This replaced refining (step 2d): a record found in coarse readings was looked for again in finer ones, a window at a time and only the
  first four, where now every record has its day's time (`ecowitt/period.py`).
- **Kept up to date as readings arrive.** A write notes the local days it touched, and their rows are worked out again in the same
  transaction: Python chooses each day's resolution and window and puts them in a temporary table, and one `INSERT ... SELECT` per kind
  works them out (a `GROUP BY` each half hour, a window over each day's half hours for its lowest and highest, and a look at the few
  readings of the half hour that holds them, for when). The temporary table is the outer loop (`CROSS JOIN`: the planner, with no
  statistics on it, scanned every reading of the kind for each work-out). One day of every sensor, as one fetch's write, takes about
  13 ms on the fake station's archive (it took 170 ms with the scan), all its 3,300 day rows 3 s (123 s). A new time zone (the station
  moved) works them all out again.
- **The AirGradient's days** wait for its step (3), since they need the spike filter.
- **Months and years** are a `GROUP BY substr(day, 1, 7)` or `substr(day, 1, 4)` on the day rows: a year is 365 rows, not 17,520.

### 3.6 Bands

Every band is in the rows, so a chart's band comes with its line:
- **Ecowitt's own lows and highs** (30 minutes and coarser) go in `_lo` and `_hi` when the readings are written.
- **The bot's live readings** (every few seconds from Home Assistant, once a minute from the cloud) are gathered per 5 minutes in
  memory, as the AirGradient's are now (`AirGradient._average`), and each 5 minutes with two or more readings widens that 5-minute
  row's range when the bucket closes. This replaces `LiveRanges.add` and its `ranges` table.
- **Home Assistant's ranges**, the station's and the AirGradient's, widen the same 5-minute rows.
- **The 30-minute rows' ranges** are widened whenever 5-minute or 30-minute rows are written (`Writer.roll_up`, one `UPDATE ... FROM`):
  by the ranges of the 5-minute rows inside them (the bot's and Home Assistant's), which replaces the `slots` table, and by the 5-minute
  values themselves where a half hour has two or more, which replaces `HistoryCache.slot_ranges` (worked out at read time now). For a
  reading Ecowitt gives its own 30-minute range for, the 5-minute values lie inside it, so that changes nothing; a compass point and the
  rain counters take no band.
- **Widening is an upsert:** `_lo = MIN(_lo, new)`, `_hi = MAX(_hi, new)`, with the row's value always inside its range. A range is never
  narrowed, and doing it twice changes nothing, as now. A band that comes before its reading (the latest 5 minutes, not fetched yet) is a
  row with only the band, provisional, which no read counts as a reading; the reading fills it in when it comes.

Retention: the bot's ranges are kept for 31 days now (`RANGE_KEEP_DAYS`). In the rows they cost a null when absent and two numbers when
present, so they are kept for good, with no table to prune (6.1).

Which charts they show on: a line drawn reading by reading at 5 minutes (a chart of up to about a day and a half, the 1D button) has a
band only from them, because Ecowitt's 5-minute readings have no range. From the 7D button on, the line is drawn from 30-minute readings
or wider buckets, whose band is Ecowitt's own lows and highs (kept for ever), which the bot's only widen a little. A 3M or 1Y chart is
drawn a point a day, so each band is that day's whole swing from its lowest to its highest: that is why the band looks so much bigger
from 3M on, and it never depended on the bot's 31 days. Keeping the bot's ranges for good gives a 1D chart of any past day its band too.

### 3.7 Coverage in memory

`coverage` is a few thousand rows of merged intervals per sensor and resolution. It is read once at start, kept in memory, and updated by
every write in the same transaction, so a request never asks it a question: "is this held?" is a lookup in a list. What is recorded as
covered follows today's rules (`HistoryCache.store`: up to the last reading returned, a stretch older than two days in full, an empty
answer from Home Assistant not at all).

### 3.8 The newest readings on disk too

The newest readings (newer than the settle horizon, still changing as late uploads arrive) are written with `final = 0` and replaced by the
next fetch, so every request reads the file alone. `HotStore` goes, and so does merging memory with the file. Coverage is still recorded
only for the final part, so the archive asks for the rest again once it has settled. What stays in memory is a note of the newest fetch
of each station, cycle and group and when it was made (`fetch.Recent`): for 5 minutes a question reads those provisional readings from
the file instead of asking again, and one fetch at a time per station and cycle (its lock, as `HotStore.lock` was).

What replaces what, a write's rules (`Writer.put`): a reading replaces the one held for the same sensor, resolution and time when it is as
final (a provisional one never replaces a final one) and from a source ranked as high (the bot's own over Home Assistant's over the
cloud's); otherwise it only fills in what the row lacks. `Writer.put_ecowitt` stores a response by `HistoryCache.store`'s rules (what
counts as fetched, an old empty stretch, a group the station lacks, a field Ecowitt adds later); the tests are `test_store.py`'s, on the
new storage.

### 3.9 The minute readings

The minutes go in the same tables, at `res = '1min'`, so a chart of a few hours is the same query as any other. They are pruned hourly by
`DELETE ... WHERE res = '1min' AND ts < ?`, a run of rows at the front of each sensor's minutes. The freed pages are reused by the next
minutes, so the file settles at its busiest size; `auto_vacuum` stays off (it moves pages about and can make fragmentation worse), and a
plain `VACUUM` in the night job is the remedy if `dbstat` ever shows a table badly scattered. Kept 10 days, the minutes are about 115,000
rows (8 sensors), some 8 MB; 31 days would be about 25 MB and 90 days about 75 MB.

What uses them: by the charts' rule (`lines.bucket_width`: the finest readings that keep a line within 500 points), a reading a minute is
drawn only for a stretch of up to about 8 hours, so no period button ever draws them (1D is 288 five-minute readings). They are drawn for
a question about a short stretch ("the last 3 hours", "yesterday from 2 to 6"), and they give a day's records their time to the minute.
The bands do not need them kept: each 5 minutes' range is written into its 5-minute row as the minutes come in. Ten days is as long as
Home Assistant keeps its own (so after the bot was down the gap can always be read back from it), and covers nearly every question about
a few hours.

### 3.10 Connections and threads

Two connections to the one file: one that writes (the archive, the refreshes, the live readings, the migration) and one that reads (the
questions, the reports, the alerts), each on a single thread of its own. In WAL mode a read never waits for a write, a request makes one hop
to the reader's thread for all its queries, and the default thread pool (which also draws the charts) is left alone. `busy_timeout` covers
the rare write that meets another. `PRAGMA optimize` runs at close.

As built (`lib/storage/__init__.py`): `await storage.read(fn)` runs `fn(reader)` on the reader's thread in one read transaction, so its
queries see one moment of the file, and `await storage.write(fn)` runs `fn(writer)` on the writer's in one transaction; each is one hop,
and its statements count towards the request that made it (the DEBUG `Chart ms` line). The sensors and the coverage are in memory; a
write's changes to them are seen once it is committed, so a reader never sees coverage for readings that were rolled back.

### 3.11 What the database does, and what is left to Python

SQLite does the work wherever the work is the same for every row:
- **Finding the rows:** a range of the primary key, `sensor, res, ts`, and nothing else is read.
- **Buckets for long lines:** `GROUP BY ts / width` with `AVG`, `MIN(COALESCE(_lo, value))`, `MAX(COALESCE(_hi, value))`, so a year
  of 5-minute readings comes back as 500 rows, not 105,120. `lines.bucket_summary` stays as the reference the SQL is tested against.
- **The days:** `INSERT ... SELECT ... GROUP BY`, written as the readings arrive (3.5).
- **Months and years:** `GROUP BY` on the day rows.
- **Records:** `ORDER BY ... LIMIT 1` on the day rows.
- **`weather_days`:** its condition ("rain >= 1 and temp_max > 30"), ranking and counting by month or year become the `WHERE`,
  `ORDER BY` and `GROUP BY` of one query on the day rows (`daytable` builds them from the same fields it checks now).
- **Rain per slot:** the rise of the day's rain counter, with its midnight reset and the gap rule of `rain.rain_slots`, is a window
  function (`LAG(daily) OVER (PARTITION BY sensor ORDER BY ts)`), so rain bars and spells read their amounts directly.
- **The wind rose:** counts per compass point and speed band are a `GROUP BY` of the direction's sector and the speed's band.
- **The AirGradient's spike filter:** a reading far above both its neighbours is a `LAG`/`LEAD` comparison, so the filter can run in the
  query instead of over every row in Python (`metrics.without_spikes`, with the same setting).

Left to Python on purpose:
- **Time zones.** SQLite knows only UTC and the computer's own zone, so local days come from Python as a small table of each day's
  start and end (which also gets the 23- and 25-hour days of the clock changes right), joined in the query.
- **The statistics** of `weather_link` and `air_scan` (correlations, the share of rain while a reading falls): numpy over the arrays one
  query returns.
- **Drawing, captions and answers:** from the few hundred rows a query returns, handled as columns (lists of numbers), not a dict per
  reading.

### 3.12 The AirGradient's sources: the monitor, then Home Assistant, then the cloud

`AIRGRADIENT_SOURCE` does for the air monitor what `ECOWITT_SOURCE` does for the station. `auto` (the default) uses each source that is
set, in this order: the monitor's own server (`AIRGRADIENT_LOCAL_URL`), Home Assistant (when it has the monitor, found by its entities'
names as its ranges are now: `airgradient.homeassistant.find_prefix`), and AirGradient's cloud (its token and location). `local`,
`homeassistant` or `cloud` is that one alone. Each source fills only what the ones before it lack, which the write rules already do (3.8):
the bot's own readings rank above Home Assistant's, Home Assistant's above the cloud's, and a lower one only fills a cell a higher one left
empty, so a row can take its PM2.5 from the bot and a reading the bot lacked from Home Assistant.
- **The monitor itself.** As now, the bot reads its `/measures/current` every minute while it runs (`SAMPLE_SECONDS`), and each 5 minutes'
  readings are a row with `src` the bot: their mean, and their lowest and highest as its band. New: each reading is also kept as a minute
  (`res = '1min'`, 10 days), so a chart of a few hours is drawn a reading a minute, as the station's are. Its latest reading is the live one.
- **Home Assistant: what the bot did not read.** Its AirGradient integration reads the same server every minute and keeps each change (10
  days unless its recorder is set otherwise), so it holds the times the bot was down, or the monitor did not answer the bot (a `.local`
  name Docker cannot look up, say). At start, and every few minutes while the monitor does not answer, the gaps of those 10 days are read
  back as the station's are (`hass_history.rollup`: each 5 minutes' mean, with its lowest and highest as the band), with `src` Home
  Assistant, and its states at the end of each minute are the minutes. While the monitor does not answer, its current states are the live
  reading.
- **The cloud: what neither has.** The history before Home Assistant's 10 days (the archive, back to the monitor's first reading), the
  times both were down, and the monitor's name. Its current reading is the live one only when neither answers.

The PM2.5, and the other readings the monitor corrects:
- **What each gives.** The monitor's `/measures/current` has its raw PM2.5 (`pm02`) and, from firmware 3.1.4, its corrected one
  (`pm02Compensated`), corrected as set on the monitor: the AirGradient dashboard's correction when the monitor takes its configuration
  from there, else its own (`PUT /config`, `corrections`: `none` by default, `epa_2021`, or one of AirGradient's calibrations of its
  PMS5003 sensors). Home Assistant's `sensor.<device>_pm2_5` is that same corrected one (the raw one is `sensor.<device>_raw_pm2_5`, off by
  default). Temperature and humidity are likewise the corrected ones where the monitor sends them; PM1 and PM10 are as measured. The
  cloud gives `pm02_corrected`, corrected as the dashboard says, which the bot keeps now (`metrics.METRICS` takes it first). So the
  three agree when the monitor takes the dashboard's correction; when the monitor is set to none, it and Home Assistant give the raw PM2.5
  and the cloud the corrected one.
- **The bot checks, it does not guess.** As it does for Home Assistant's ranges now (`airgradient.homeassistant.checked`), each metric's
  five-minute values from a source are compared with those of the source ranked above it, over the time both hold: within 2 µg/m³ or 15%,
  in four in five of them, is the same reading. While the bot reads the monitor it has little from the cloud to compare, so the cloud is
  asked once a day for the newest finished day (one request; its rows fill only what the bot lacked).
- **A metric that disagrees fills no gaps.** It keeps coming from the source ranked first, as the order says, and a lower source does not
  fill its gaps (they are drawn as gaps), so no line joins two variants over the time the bot reads the monitor. The start-up line says once
  which metric differs, with the fix: set the monitor's PM2.5 correction to the dashboard's, or have it take its configuration from the
  dashboard. The history from before stays as it came from the cloud, so a chart across the bot's first days can show the step until the
  fix. The check runs again each day on the newest day, so a correction changed on the monitor or the dashboard is followed.
- **With nothing to check against** (one source alone), its values are taken as they are, and the start-up line says which correction the
  monitor applies, from its `/config` when `AIRGRADIENT_LOCAL_URL` is set ("PM2.5: the monitor's EPA 2021 correction", "PM2.5: as
  measured, no correction").

## 4. How each request reads

| Request | Queries |
|---|---|
| A chart of one reading | the line and its band at the resolution its length calls for, bucketed in SQL where it is too long to draw reading by reading (`GROUP BY ts / width`), lows and highs included; the records from the day rows, and the part-days at either end from the fine rows |
| A chart of several readings | the same, one query per kind table (temperature and humidity are one) |
| A history answer | the period's rows of the asked sensors, one query per kind (outdoor and indoor are one); its daily breakdown and records from the day rows |
| `weather_days`, `air_days` | one query on the day rows for the whole period |
| `/dashboard` | one query per kind on the day rows (today, this month, this year) with the live reading |
| `weather_link`, `/compose` | one query per reading at 30 minutes |
| Rain bars | one query: the counter's rises per slot (`LAG`), summed per bar |
| The wind rose | one query: counts per compass point and speed band |
| The alerts' last 3 hours | one query per kind at 5 minutes |

Aims, against section 1: a chart under 50 ms whatever its length (the 1Y chart is 1.2 s now), `/dashboard` and `weather_days` over
the whole record under 50 ms (3.1 s and 0.7 s now), and every request one thread hop.

The reads themselves, on the owner's cache moved into the new storage (`scripts/migrate_storage.py` in the container, October 2026): the
data a request needs, before step 2 builds its answer and chart on it. Against section 1's table: the 1Y chart's data in 2.4 ms where
the request took 1,205 ms, the first dashboard's in 22 ms where it took 3,146.

| Read on the new file | SQL statements | Thread hops | Time |
|---|---|---|---|
| Temperature line, 1D (5-minute rows) | 1 | 1 | 1.3 ms |
| Temperature line, 7D (30-minute rows) | 1 | 1 | 1.5 ms |
| Temperature line, 1M (2-hour buckets) | 1 | 1 | 5.5 ms |
| Temperature line, 3M (day rows) | 1 | 1 | 0.9 ms |
| Temperature line, 1Y (day rows) | 1 | 1 | 2.4 ms |
| Records: each month's hottest day, a year | 1 | 1 | 1.2 ms |
| `weather_days`: 4 years of temperature and rain (day rows) | 2 | 1 | 16 ms |
| Rain per half hour, 3M | 1 | 1 | 23 ms |
| `/dashboard`: the year's day rows of every sensor | 5 | 1 | 22 ms |
| Air line (PM2.5), 1M (2-hour buckets) | 1 | 1 | 18 ms |

## 5. Migration

- **Automatic, at start** (from step 2). When the old files hold what `ecobot.sqlite` does not yet, the bot moves it across before the
  station starts, logging "Moving the history to ecobot.sqlite (once)". Nothing is fetched: Ecowitt's cloud keeps only 90 days of
  5-minute readings, so the history is the bot's to keep. `scripts/migrate_storage.py` does the same by hand on a copy of the files, with
  the bot running, and times the new storage's reads (section 4). `lib/storage/migrate.py` does it (`open_storage` at start).
- **In parts.** Each part moves when its code does, and the file notes it (`migrated.<part>` in `meta`): the station's three files in
  step 2, the AirGradient's in step 3, the snapshots in step 5. A part moved into a file that is there already is merged into it: its
  sensors found or added, the file's own readings kept and the old ones filling in what they lack, each band the wider of the two, the
  coverage of both, and the days of what came in worked out again.
- **What goes where.**
  - Ecowitt `series` and `points`: each `(mac, cycle, group)` becomes a sensor and a resolution; `field`, `field_low` and `field_high`
    become one row's value, low and high; the text becomes REAL; a field the catalogue does not know, or a value that is not a number,
    goes in `extra` as it came (both counted in the report). A VPD still in inHg (a cache from before the unit fix) is converted as
    `HistoryCache` does.
  - Ecowitt `coverage` becomes `coverage` for that sensor and resolution.
  - `ecowitt_live.sqlite`: the `ranges` with two or more readings widen the 5-minute rows; `slots` widen the 30-minute rows.
  - `ecowitt_minutes.sqlite` becomes `1min` rows.
  - `airgradient_cache.sqlite`: `readings` become air rows (`src` the bot's where `recorded` says so) at `5min`, or `1hour` on a
    finished day of 30 readings or fewer (as the AirGradient code tells them), `spreads` their lows and highs, and `days` the coverage.
  - `conditions_cache.sqlite`: `snapshots` and `meta`, as they are.
  - Then the 30-minute bands are widened from the 5-minute readings, and every day row is worked out once.
- **Checked before it counts.** It is built as `ecobot.sqlite.part`, in one transaction; for every sensor, resolution and cell (a value,
  a low, a high) the number of values and their sum are compared with the old files' before the bands are widened, and so are the
  coverage, that every range is inside its row's band, the minutes, the AirGradient's readings and its own, and the snapshots. Any
  difference and the built file is deleted and the log says why; the bot starts with what its sources still have (a new `ecobot.sqlite`,
  filled by the archive from what Ecowitt's cloud still keeps), and the move is tried again at the next start, into that file. Only a
  move that adds up is renamed `ecobot.sqlite` (or merged into it). It needs twice the old files' size free on the disk, and says so if
  there is not.
- **What it says.** On the owner's cache: "Moved the history to ecobot.sqlite in 15.5s: 1,713,215 station readings, 16,589 bands,
  11,880 minutes, 423,571 AirGradient readings, 312 snapshots; 32.7 MB of old files, 28.6 MB now", every count and sum matching. Then,
  with an example of each, the fields it has no column for, the values that are not numbers (1,046 of them there, in five fields that
  have columns: indoor `app_tempin`, `dew_point` and `feels_like`, outdoor `vpd`, rain `yearly`; kept in `extra`, read by no answer),
  and the values that would be written back with other digits. That run found the UV written two ways, the cloud's
  whole numbers ("3") and Home Assistant's one decimal ("1.4"): UV is now written with one decimal unless it is whole, so both come back
  as they were. The only other change is a zero's minus ("-0.0" is "0.0", 10 values).
- **The old files are not touched.** They stay where they are, so going back is the previous commit. That version fetches again whatever
  its files lack (the cloud still has the last 90 days at 5 minutes), so a few days on the new storage cost nothing but the live bands of
  those days. Deleting them is left to a later release, after a few weeks.
- **Size.** Smaller than the old ones together: a reading's key is written once per row, not once per value (the stand-in's 45.8 MB of old
  files are 27.2 MB). Both are on disk until the old ones go.

## 6. Steps

Each step is a commit (or a few) pushed to the dev branch with the tests and evals passing, and the benchmark run before and after.

0. **Measure (done).** `scripts/bench_storage.py` (`docker compose exec ecobot python scripts/bench_storage.py`) gave the baseline in
   section 1, the yardstick for the rest; `tests/bench_cache.py` is the stand-in for the real caches between the owner's runs; the DEBUG
   `Chart ms` line counts each chart's queries and thread hops.
1. **The new storage on its own (done).** `lib/storage/`: the catalogue, the schema and its versions, the two connections, writing
   (readings, ranges, minutes, coverage, day rows, Ecowitt's responses by the old cache's rules) and reading (rows, buckets, days, records,
   rain per reading). The migration, with tests on old-format files built by the tests themselves and on the fake station's whole archive
   (`tests/test_storage.py`, `tests/test_storage_migrate.py`), and `scripts/migrate_storage.py` to check it on a copy of the real cache.
   Nothing uses it yet. On the way: the statement count of step 0 used SQLite's trace hook, for which Python writes out every row of an
   `executemany` with its values, six times the cost of the insert; it now counts at the connection's own calls (`counts.Counted`).
2. **The station on it.** `Fetcher`, `HistoryQuery`, `days`, `link`, `dashboard`, `compose`, the alerts, the archive, the live readings, the
   minutes and Home Assistant's ranges read and write the new tables; `HotStore`, `LiveRanges`, `MinuteStore` and the refining go. The bot
   migrates at start. While this is built, a temporary test runs the same questions on the same fixture through the old code and the new
   and compares the answers, so any difference is seen and judged (`tests/test_old_against_new.py`: the old code is taken from git).
   In progress: the station writes and reads `ecobot.sqlite` (moved in at start), the readers not moved yet reading it through
   `ecowitt/stored.py` as they read the old cache. `weather_days` and the dashboard read the day rows (one read each; the dashboard's
   Daily row is today's day row, its readings so far with their bands). A history answer's records, its rows by day or by month and its
   averages come from the period's days (`ecowitt/period.py`: the day rows, and the part-days at either end from their own readings),
   and refining is gone; a day of 5-minute readings takes in its half hours' own lows and highs, as refining kept them. Next, the
   history answer's readings and charts read natively (rows, and buckets or day rows for long charts).
3. **The AirGradient on it.** Its readings, the bot's own, its days, its spreads and Home Assistant's ranges in the `air` tables;
   `AirStore` goes.
4. **The AirGradient from the monitor, Home Assistant and the cloud** (3.12). `AIRGRADIENT_SOURCE`; its readings, minutes and live
   reading from the monitor itself, Home Assistant filling what the bot did not read and the cloud what neither has, each metric checked
   against the source ranked above it before it fills a gap; the settings table and the start-up line. After step 3, since it writes the
   `air` table's rows, minutes and bands by the same rules as the station's, and its readings back to Home Assistant's first state need the
   archive's code the station uses.
5. **Tidying.** The snapshots move; the old store code goes; the scripts that read the files (`cache_status`, `check_rain`,
   `show_request`, `dump_specs`, `refill_airgradient`) read the new one; `migrate_cache.py` retires; the docs say how the storage works.
   And a look for dead code left from when the period buttons' charts stopped being drawn ahead on a timer (`92d71df`): what only that
   timer used, in the image cache, the bot and the tests.
6. **Several sensors in use (later, when there is a second sensor).** Settings for more than one AirGradient, the station's extra
   channels found from its live reading, a `sensor` argument for the tools and the words for it in the prompt, labels, alerts per sensor,
   and a sensor per Home Assistant prefix (today it takes the first and warns).

### 6.1 Decisions

- **The minutes are kept 10 days.** Only charts of up to about 8 hours draw them (3.9), so more than that holds readings that are
  almost never drawn; 10 days is what Home Assistant keeps, so the bot can always read a gap back. A constant, if that changes.
- **The bot's own bands are kept for good.** They are two numbers in a row that is kept anyway, and a 1D chart of any past day keeps
  its band (3.6).
- **Several sensors: the schema now, step 6 later**, when there is a second sensor.
- **A day's highest and lowest include the bands** (step 1, to be judged in step 2's comparison). The 5-minute rows carry the bot's and Home
  Assistant's ranges, so a day's figures from them are the extremes the bot saw, as the dashboard already counts them; `weather_days` and a
  history answer's records took the 5-minute values alone. Where the bot has no ranges (before it ran, or a day from 30-minute readings) the
  figures are the old ones, as the test against `day_values` shows.
- **A daily record's day keeps its own figures**, and takes only the times from the 4-hour readings. Refining took the more extreme of the
  two as well; on Ecowitt's data the two agree (a 4-hour high is inside its UTC day's), and `day_values` uses the record's.

## 7. Testing

- **The answers do not change:** the golden chart images and the evals unchanged; the old-against-new comparison of step 2; the
  dashboard's and `weather_days`' figures compared day by day on the fixture (step 1 does it for the day rows against `day_values`, and
  the buckets against `lines.bucket_summary`, rain per reading against `rain.rain_slots`).
- **The stores' own tests** (`test_store.py`, `test_live_ranges.py`, `test_migrate_cache.py`, and the dozen tests that write SQL into the old
  tables) are rewritten for the new tables: writing and reading, widening, day rows, coverage, pruning, the migration and its check.
- **The fixture's archive** (`tests/conftest.py`, `archived_cache`) is built by the new code; one old-format fixture stays for the migration
  tests.
- **The benchmark's data folder** (`tests/bench_cache.py`) is built by the new storage from step 2 on, with 10 days of minutes (the
  owner's file has 9 hours), so the table shows whether the minutes in the same tables slow anything else.

## 8. Risks

| Risk | What is done about it |
|---|---|
| A number written differently in an answer ("12.30" against "12.3") | values written with the catalogue's decimals; the migration counts, per field, the values whose text would come back otherwise (`scripts/migrate_storage.py` on the real cache, before step 2); the evals and the step 2 comparison show any change |
| A record's time differs from what refining found | the day rows' times come from the finest readings the day has, as refining's did; compared in step 2 and each difference judged (`tests/test_old_against_new.py`): ties now go to the first day, and every record has its day's time, where refining narrowed four |
| Ecowitt sends a field or group the catalogue does not know | `extra` keeps the readings; the catalogue adds a column for them later |
| The migration fails part-way or runs out of disk | one transaction, the count and sum check, the old files untouched, and the free space checked first |
| The 5-minute marks: the live ranges are counted around each mark, Home Assistant's from each mark | kept as they are in step 2, so nothing moves on a chart; one rule for both is a later choice |
| Day rows out of date | worked out in the same transaction as the readings that change them; the tests write a day's 30-minute readings, then its 5-minute ones, and check the day follows |
