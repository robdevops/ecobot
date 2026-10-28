# Storage redesign plan

The plan for replacing the bot's five SQLite files with one, laid out so that a chart, a report or a day count reads what it needs in one
or two queries, and does not then sort it out row by row in Python. It turns the requirements in [TODO.md](../TODO.md) into a design, a
migration and a sequence of steps, each shipped on its own.

## 1. Where the time goes now

What typical requests cost, measured on the fake station's archived cache (`tests/fakes.py`, 1.07 million readings: 88, 363, 548 and 546
days at 5 minutes, 30 minutes, 4 hours and a day), all from the cache with no request to Ecowitt. The real cache (27 MB) has more fields
per group and more old days, so its numbers will be higher; step 0 measures them.

| Request | SQL statements | Thread hops | Time |
|---|---|---|---|
| Temperature chart, 1D | 5 | 5 | 7 ms |
| Temperature chart, 7D | 15 | 12 | 14 ms |
| Temperature chart, 1M | 29 | 21 | 73 ms |
| Temperature chart, 3M | 31 | 25 | 183 ms |
| Temperature chart, 1Y | 22 | 17 | 32 ms |
| History of outdoor and indoor, 7 days, no chart | 24 | 13 | 20 ms |
| `weather_days`: days over 20°C in the whole record | 13 | 1 | 543 ms |
| `/dashboard`, first time in a day | 128 | 4 | 1,444 ms |
| `/dashboard`, Refresh | 73 | 2 | 11 ms |
| The alerts' look at the last 3 hours | 15 | 2 | 2 ms |

A profile of the three slowest shows the queries are not most of it: SQLite takes 15 to 25% of the time, and the rest is Python handling
the rows one at a time. `HistoryCache.load_fields` turns every value from text into a float and files it in a dict of dicts, and
`days.day_figures` works out the local date of every reading (half a million `local_date` calls for one dashboard) to find each day's
high and low. The dashboard is slow because it works out a year of daily highs and lows from the 30-minute readings the first time each
day.

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
| `coverage` | which times are held, per sensor and resolution |
| `climate`, `pressure`, `wind`, `rain`, `light`, `air` | the readings, one table per kind of sensor (3.3) |
| `climate_day` ... `air_day` | each sensor's local days: mean, lowest and highest and when (3.5) |
| `extra` | anything a source sends that the catalogue does not know yet (3.3) |
| `snapshots` | the pollen page and the forecast, as now (`lib/snapshots.py`), moved in unchanged |

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
sensor per Home Assistant entity prefix) is a later step (section 6, step 5).

### 3.3 The readings tables

One table per kind, keyed by sensor, resolution and time, `WITHOUT ROWID`, so each sensor's readings at one resolution are one run of
pages in time order, and a new reading goes on the end of its run:

```sql
CREATE TABLE climate (
    sensor INTEGER NOT NULL, res TEXT NOT NULL, ts INTEGER NOT NULL,
    temperature REAL, temperature_lo REAL, temperature_hi REAL,
    humidity    REAL, humidity_lo    REAL, humidity_hi    REAL,
    dew_point   REAL, dew_point_lo   REAL, dew_point_hi   REAL,
    feels_like  REAL, feels_like_lo  REAL, feels_like_hi  REAL,
    vpd         REAL, vpd_lo         REAL, vpd_hi         REAL,
    src  INTEGER NOT NULL,   -- where the values came from: the cloud, Home Assistant, the bot's own readings (which win)
    final INTEGER NOT NULL,  -- 0 while the readings may still change (newer than the cycle's settle horizon)
    PRIMARY KEY (sensor, res, ts)) WITHOUT ROWID;
```

- **The catalogue.** The columns come from one table in code, which grows out of `lib/series.py` and `lib/ecowitt/hass_map.py`: each
  reading's kind, column, unit, decimals and how a stretch of it is told as one (mean, highest, last). The DDL is generated from it, and a
  reading added to it later is an `ALTER TABLE ... ADD COLUMN`, which SQLite does at once.
- **Kinds.** climate (temperature, humidity, dew point, feels like, VPD), pressure (relative, absolute), wind (speed, gust, direction), rain
  (rate, daily, event, hourly, weekly, monthly, yearly), light (solar, UV), air (PM1, PM2.5, PM10, CO₂, VOC, NOx, the sensor's own
  temperature and humidity). Ecowitt's own air sensors (WH45 and the like) would be air sensors too.
- **Values are REAL**, rounded on write to the reading's decimals (`hass_map.written` does this now for Home Assistant), and written back
  out with them, so an answer shows "12.3" as it does now. No text to parse on reading.
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
| `1min` | Home Assistant's states (the station, the AirGradient), the bot's own minute reads of the AirGradient | 9 hours (a decision: 6.1) |
| `5min` | Ecowitt's cloud, Home Assistant, the bot's own AirGradient averages; bands from the live readings and Home Assistant | for ever (the archive) |
| `30min`, `4hour`, `1day` | Ecowitt's cloud (lows and highs included), Home Assistant | for ever |
| `1hour` | AirGradient's cloud, for the days it no longer has at 5 minutes | for ever |

### 3.5 Days: each sensor's local days, worked out once

Most of the slow requests ask about days: the dashboard's daily, monthly and yearly highs and lows, `weather_days`, the daily breakdown of a
history answer, and the records of a period. So each sensor's local days are kept, worked out from the finest readings held for each day:

```sql
CREATE TABLE climate_day (
    sensor INTEGER NOT NULL, day TEXT NOT NULL,   -- the local date, 2026-10-29
    source TEXT NOT NULL,                         -- the resolution it was worked out from: 1min, 5min, 30min, or 1day (approximate)
    temperature_mean REAL, temperature_lo REAL, temperature_lo_ts INTEGER, temperature_hi REAL, temperature_hi_ts INTEGER,
    humidity_mean REAL, ...,
    PRIMARY KEY (sensor, day)) WITHOUT ROWID;
```

- **Exactly what is done now, once.** The figures follow `days.day_values` and `days.day_figures` (5- or 30-minute readings where the
  whole local day is held, else Ecowitt's daily record, labelled approximate), and each reading's mode decides what its figure is (the
  rain counter's highest is the day's total).
- **When the low and high happened.** A day row keeps the time of its low and high, so a period's records come from one query on the
  day rows (`ORDER BY temperature_hi DESC LIMIT 1`), with their times. That replaces most of the refining. The part-days at either
  end of a period are read from the fine rows.
- **Kept up to date as readings arrive.** A write notes the local days it touched, and their rows are worked out again in the same
  transaction, by one `INSERT ... SELECT` with `GROUP BY` per kind, a millisecond for a day of 5-minute readings. A new time zone (the
  station moved) works them all out again.
- **Months and years** are a `GROUP BY substr(day, 1, 7)` or `substr(day, 1, 4)` on the day rows: a year is 365 rows, not 17,520.

### 3.6 Bands

Every band is in the rows, so a chart's band comes with its line:
- **Ecowitt's own lows and highs** (30 minutes and coarser) go in `_lo` and `_hi` when the readings are written.
- **The bot's live readings** (every few seconds from Home Assistant, once a minute from the cloud) are gathered per 5 minutes in
  memory, as the AirGradient's are now (`AirGradient._average`), and each 5 minutes with two or more readings widens that 5-minute
  row's range when the bucket closes. This replaces `LiveRanges.add` and its `ranges` table.
- **Home Assistant's ranges**, the station's and the AirGradient's, widen the same 5-minute rows.
- **The 30-minute rows' ranges** are widened as the 5-minute rows inside them are written: by those rows' own ranges (the bot's and
  Home Assistant's), for every reading, which replaces the `slots` table; and, for the readings Ecowitt gives no 30-minute range for
  (dew point, feels like, VPD, pressure, solar, UV), by the 5-minute values themselves where there are two or more, which replaces
  `HistoryCache.slot_ranges` (worked out at read time now).
- **Widening is an upsert:** `_lo = MIN(_lo, new)`, `_hi = MAX(_hi, new)`, with the row's value always inside its range. A range is never
  narrowed, and doing it twice changes nothing, as now.

Retention: the bot's ranges are kept for 31 days now (`RANGE_KEEP_DAYS`). In the rows they cost a null when absent and two numbers when
present, so they can be kept for good with no table to prune. That is a decision too (6.1).

### 3.7 Coverage in memory

`coverage` is a few thousand rows of merged intervals per sensor and resolution. It is read once at start, kept in memory, and updated by
every write in the same transaction, so a request never asks it a question: "is this held?" is a lookup in a list. What is recorded as
covered follows today's rules (`HistoryCache.store`: up to the last reading returned, a stretch older than two days in full, an empty
answer from Home Assistant not at all).

### 3.8 The newest readings on disk too

The newest readings (newer than the settle horizon, still changing as late uploads arrive) are written with `final = 0` and replaced by the
next fetch, so every request reads the file alone. `HotStore` goes, and so does merging memory with the file. Coverage is still recorded
only for the final part, so the archive asks for the rest again once it has settled. The one-fetch-at-a-time lock per station and cycle
stays (`HotStore.lock`), as a dict of locks of its own.

### 3.9 The minute readings

The minutes go in the same tables, at `res = '1min'`, so a chart of a few hours is the same query as any other. They are pruned hourly by
`DELETE ... WHERE res = '1min' AND ts < ?`, a run of rows at the front of each sensor's minutes. The freed pages are reused by the next
minutes, so the file settles at its busiest size; `auto_vacuum` stays off (it moves pages about and can make fragmentation worse), and a
plain `VACUUM` in the night job is the remedy if `dbstat` ever shows a table badly scattered. At 9 hours the minutes are about 4,000 rows; at
31 days about 360,000 (8 sensors), some 25 MB.

### 3.10 Connections and threads

Two connections to the one file: one that writes (the archive, the refreshes, the live readings, the migration) and one that reads (the
questions, the reports, the alerts), each on a single thread of its own. In WAL mode a read never waits for a write, a request makes one hop
to the reader's thread for all its queries, and the default thread pool (which also draws the charts) is left alone. `busy_timeout` covers
the rare write that meets another. `PRAGMA optimize` runs at close.

## 4. How each request reads

| Request | Queries |
|---|---|
| A chart of one reading | the line and its band at the resolution its length calls for, bucketed in SQL where it is too long to draw reading by reading (`GROUP BY ts / width`), lows and highs included; the records from the day rows, and the part-days at either end from the fine rows |
| A chart of several readings | the same, one query per kind table (temperature and humidity are one) |
| A history answer | the period's rows of the asked sensors, one query per kind (outdoor and indoor are one); its daily breakdown and records from the day rows |
| `weather_days`, `air_days` | one query on the day rows for the whole period |
| `/dashboard` | one query per kind on the day rows (today, this month, this year) with the live reading |
| `weather_link`, `/compose` | one query per reading at 30 minutes |
| The alerts' last 3 hours | one query per kind at 5 minutes |

Aims, to be checked against the real cache: a chart under 50 ms whatever its length, `/dashboard` and `weather_days` over the whole
record under 50 ms, and every request one thread hop.

## 5. Migration

- **Automatic, at start.** When `ecobot.sqlite` does not exist and the old files do, the bot copies them across before anything else starts,
  logging "Moving the history to ecobot.sqlite (once)". Nothing is fetched: Ecowitt's cloud keeps only 90 days of 5-minute readings, so
  the history is the bot's to keep. `scripts/migrate_storage.py` does the same by hand, and checks a copy without the bot.
- **What goes where.**
  - Ecowitt `series` and `points`: each `(mac, cycle, group)` becomes a sensor and a resolution; `field`, `field_low` and `field_high`
    become one row's value, low and high; the text becomes REAL at the reading's decimals; a field the catalogue does not know goes in
    `extra`.
  - Ecowitt `coverage` becomes `coverage` for that sensor and resolution.
  - `ecowitt_live.sqlite`: the `ranges` with two or more readings widen the 5-minute rows; `slots` widen the 30-minute rows.
  - `ecowitt_minutes.sqlite` becomes `1min` rows.
  - `airgradient_cache.sqlite`: `readings` become air rows (`src` the bot's where `recorded` says so), `spreads` their lows and highs,
    and `days` the coverage.
  - `conditions_cache.sqlite`: `snapshots` and `meta`, as they are.
  - Then every day row is worked out once.
- **Checked before it counts.** One transaction; then for every sensor and resolution the number of readings and their sum are compared
  with the old files'. Any difference and the new file is deleted, the bot carries on with the old ones, and the log says why.
- **The old files are not touched.** They stay where they are, so going back is the previous commit. That version fetches again whatever
  its files lack (the cloud still has the last 90 days at 5 minutes), so a few days on the new storage cost nothing but the live bands of
  those days. Deleting them is left to a later release, after a few weeks.
- **Size.** The new file should be no bigger than the old ones together (about 30 MB now), likely smaller: a reading's key is written once
  per row, not once per value. Both are on disk until the old ones go.

## 6. Steps

Each step is a commit (or a few) pushed to the dev branch with the tests and evals passing, and the benchmark run before and after.

0. **Measure.** `scripts/bench_storage.py` (the table in section 1, from any cache file, the real one included:
   `docker compose exec ecobot python scripts/bench_storage.py`), and the number of queries and thread hops in the DEBUG `Chart ms` line.
   The baseline on the real cache is the yardstick for the rest.
1. **The new storage on its own.** `lib/storage/`: the catalogue, the schema and its versions, the two connections, writing (readings,
   ranges, minutes, coverage, day rows) and reading (rows, buckets, days, records). The migration, with tests on old-format files built by
   the tests themselves. Nothing uses it yet.
2. **The station on it.** `Fetcher`, `HistoryQuery`, `days`, `link`, `dashboard`, `compose`, the alerts, the archive, the live readings, the
   minutes and Home Assistant's ranges read and write the new tables; `HotStore`, `LiveRanges`, `MinuteStore` and the refining go. The bot
   migrates at start. While this is built, a temporary test runs the same questions on the same fixture through the old code and the new
   and compares the answers, so any difference is seen and judged.
3. **The AirGradient on it.** Its readings, the bot's own, its days, its spreads and Home Assistant's ranges in the `air` tables;
   `AirStore` goes.
4. **Tidying.** The snapshots move; the old store code goes; the scripts that read the files (`cache_status`, `check_rain`,
   `show_request`, `dump_specs`, `refill_airgradient`) read the new one; `migrate_cache.py` retires; the docs say how the storage works.
5. **Several sensors in use.** Settings for more than one AirGradient, the station's extra channels found from its live reading, a
   `sensor` argument for the tools and the words for it in the prompt, labels, alerts per sensor, and a sensor per Home Assistant
   prefix (today it takes the first and warns).

### 6.1 Decisions for you

- **How long the minutes are kept:** 9 hours (enough for the charts of up to 8 hours, as now) or 31 days (zooming in on any day of the
  last month), about 25 MB more.
- **How long the bot's own bands are kept:** 31 days, as now, or for good (they are two numbers in a row that is kept anyway).
- **Several sensors:** the schema has them from step 1; is step 5 wanted now, or later, when there is a second sensor?

## 7. Testing

- **The answers do not change:** the golden chart images and the evals unchanged; the old-against-new comparison of step 2; the
  dashboard's and `weather_days`' figures compared day by day on the fixture.
- **The stores' own tests** (`test_store.py`, `test_live_ranges.py`, `test_migrate_cache.py`, and the dozen tests that write SQL into the old
  tables) are rewritten for the new tables: writing and reading, widening, day rows, coverage, pruning, the migration and its check.
- **The fixture's archive** (`tests/conftest.py`, `archived_cache`) is built by the new code; one old-format fixture stays for the migration
  tests.

## 8. Risks

| Risk | What is done about it |
|---|---|
| A number written differently in an answer ("12.30" against "12.3") | values written with the catalogue's decimals; the evals and the step 2 comparison show any change |
| A record's time differs from what refining found | the day rows' times come from the finest readings the day has, as refining does; compared in step 2, and any difference judged before it ships |
| Ecowitt sends a field or group the catalogue does not know | `extra` keeps the readings; the catalogue adds a column for them later |
| The migration fails part-way or runs out of disk | one transaction, the count and sum check, the old files untouched, and the free space checked first |
| The 5-minute marks: the live ranges are counted around each mark, Home Assistant's from each mark | kept as they are in step 2, so nothing moves on a chart; one rule for both is a later choice |
| Day rows out of date | worked out in the same transaction as the readings that change them; a test writes readings out of order and checks the days |
