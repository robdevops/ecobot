# To do

Things to check or finish that need something this repo can't do on its own.

## Check when PM2.5 is high

Does the sensor's `pm02Compensated` equal the cloud's `pm02_corrected`? Both read 0 on the day it was tried, so it is not known. The bot records
`pm02Compensated` (preferring it to the plain `pm02`) as its PM2.5. If the two differ, a chart would show a step wherever the bot's own readings
meet the cloud's.

When the air has real particles in it (smoke, incense, a dusty day), run these within a minute of each other, with `TOKEN` set to the
`AIRGRADIENT_API_TOKEN` and the sensor's address in place of the name if `.local` does not resolve:

```
curl -s "https://api.airgradient.com/public/api/v1/locations/153523/measures/current?token=$TOKEN" | jq '{timestamp, pm02, pm02_corrected}'
curl -s http://airgradient_d83bda1c1e08.local/measures/current | jq '{pm02, pm02Compensated}'
```

Equal (or within a few percent): nothing to do. Different: compare over a longer stretch with the `measures/past` call (5-minute rows, stamped with the start of
the interval) before deciding which to trust.

## Try on the real machines

- The sensor's own server: `AIRGRADIENT_LOCAL_URL` on a bot on the sensor's network. Check the log says "live readings from the sensor itself", that "air now" is
  fresh, and that a chart of today has no step where the bot's readings begin.
- Without a cloud account (no `AIRGRADIENT_API_TOKEN`): nothing should be asked of the cloud, and the first charts only reach back to when the bot began recording.
- `ecobot.service` as a real unit (`MemoryDenyWriteExecute`, `AF_NETLINK` for finding the sensor by name, `StateDirectory`, `CacheDirectory`): start it, then ask for a chart, an
  analysis question and something that reads the caches.
- The Docker image with `zeroconf` in it: the `image` check on a pull request builds it.
- `ALERT_COOLDOWN_MINUTES` in the production env file: the default is now 60; a line setting it to 30 keeps 30.

## Try Home Assistant as the weather station's source, then consider one table for the history

Set `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` (and leave `ECOWITT_SOURCE` at `auto`, or try `homeassistant` with no Ecowitt keys) and run it for a week or two.
It has only been run against a fake Home Assistant. Check in the log and the charts:
- the start-up line says where the readings come from, and the live poll and minute pulls don't warn (or warn once and recover);
- a chart of a few hours is drawn a reading a minute, a day or a week looks right, and a chart past Home Assistant's 10 days still has its bands (from the cache);
- a rain alert comes within seconds of the gauge's report; "weather now" is fresh;
- with both set, nothing is asked of the cloud except what Home Assistant lacks.

Then decide whether to simplify the storage. The Ecowitt history is spread over `ecowitt_cache.sqlite` (the cycles), `ecowitt_live.sqlite` (5-minute ranges and
30-minute slots, for the bands) and `ecowitt_minutes.sqlite` (the last 9 hours of minutes), merged at query time (`_live_bands`, `_widen`), with resolution picked in
several places (`HistoryQuery._fetch_period`). The idea: one `samples` table (series, resolution, time, value, low, high, count) with a retention per resolution, so a band is
just the low and high columns, and the cloud and Home Assistant are written the same way. Write the design first, then a one-off migration that copies the old files across
without deleting them (so it can be rolled back). Not urgent: nothing is wrong, it is only more complicated than it needs to be.

The aim of the restructure is fewer queries, so a chart is quicker. Today a chart can read the coverage table, then the cycle's points, then the live ranges and slots (a
second file), and the minutes (a third), each in its own thread hop. Measure first (the reply's timing line already splits cache, memory and Ecowitt time; add the
number of queries and thread hops), then design for one read per chart: one table, one query over the series and window that gives value, low and high together, with the coverage
kept in the same file and checked in the same query. Compare the same charts before and after (the golden images must not change).

### What the new storage has to do

- **Alike data together, so most reports are one query.** A chart or report asks for readings that go together (outdoor temperature, humidity, dew point, feels like ...; the particles; the pressures). Store a sensor's alike readings as columns of one row per time (value, low and high of each), not a row per reading, so one range scan returns all of them, and the time is written once per row, not once per reading. A null column costs a byte, so a sensor that lacks a reading costs nothing. Check that the reports' usual questions (a day, a week, a month, a year, one reading or a pair, with bands) are each one query.
- **Samey data contiguous.** One table per resolution (1 minute, 5 minutes, 30 minutes, 4 hours, a day), keyed `(sensor id, time)` and stored `WITHOUT ROWID`, so a sensor's readings sit next to each other in time order and a chart reads a run of pages. Use an integer sensor id, not text, in the key.
- **Several sensors of one kind** (two Ecowitt stations, two AirGradient monitors, an indoor and an outdoor one). A `sensors` table (id, kind, name, source, its address: the station's MAC, the AirGradient location) and every key, cache entry and coverage record leads with the sensor id. What follows from that and has to be designed with it: the model's tools and the prompt need a way to say which sensor ("the garage sensor"), charts and the report need to label them, alerts are per sensor, and the Home Assistant lookup (which today picks the first station or sensor it finds and warns) must map each entity prefix to a sensor.
- **Short-lived data (1 minute, kept ~9 hours to 10-31 days).** Keeping it in the same file will not make the file grow without limit: SQLite puts the pages freed by deleting old rows on a free list and the new rows reuse them, so a file with steady deleting and inserting settles at its busiest size. It does not shrink by itself, though, and the minute data would be most of the file (about 22 readings x 44,640 minutes = 1 million rows a month, more than all the longer history held now), so keep it in its own table with its own retention. Expire it by deleting the oldest rows in small batches (hourly, as the live ranges are pruned now): about 33,000 rows a day is milliseconds of work, the freed pages are reused, and every chart stays one query whatever its length. `auto_vacuum=INCREMENTAL` (one `VACUUM` in the migration, then `PRAGMA incremental_vacuum` after a prune) is only for giving the space back to the disk. One table per day (`minutes_2026_10_28`, expired with `DROP TABLE`) frees whole pages with no fragmentation, but it needs a new table and a rebuilt view every day, table names built into the SQL, and a 31-way `UNION ALL` for a month's chart, which works against one query per report: use it only if measuring a month of real data shows the deleting causing stalls or a file that will not settle. Measure the key order too: `(sensor, time)` keeps one sensor's readings contiguous for charts; `(time, sensor)` makes expiry a clean sweep from the front; with one row per sensor and time and a handful of sensors either is cheap. A separate file joined with `ATTACH` is the fallback if its churn gets in the way of the long history's backups or checkpoints (one connection can still query across them).
