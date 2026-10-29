# Production code review: size, dead code, special handling (October 2026)

Scope: `ecobot.py`, `lib/*.py`, `lib/*/*.py`: 15,420 lines, of which 11,150 are code (12% docstrings, 1% comment-only lines, 15% blank).
Five reviewers read every file; every "dead" claim was checked by grep across `lib/`, `ecobot.py`, `scripts/` and `tests/`, including
dynamic use (tool registries, Telegram handler registration, `getattr`). Confidence is the reviewer's; "owner decides" means the code is a
feature whose value only the owner knows.

Where the lines are (code lines, not counting docstrings and blanks): bot/Telegram/LLM 2,500; intent and charts 2,800; Ecowitt 2,480;
storage and AirGradient 2,700; alerts, pollen, forecast, irrigation 1,500. Prose constants (the system prompt, caption hints) are ~160 of
them.

## 1. Dead code (removed in the commit after this review, except `Bars.values` and `Compass.speed_steps`, which tests and a golden sample use)

| where | lines | what |
|---|---|---|
| lib/ecowitt/fetch.py:19-25, 161-164 | 15 | `series_in`; `Fetcher.read` (its docstring still says "as the old cache's") |
| lib/ecowitt/extremes.py:18-23 | 8 | `low_of`, `high_of` |
| lib/ecowitt/source.py:152,164,322,325,346,349 | 6 | `_minutes_ok`, `_poll_ok`: written, never read |
| lib/ecowitt/query.py:88,184 | 2 | `by_minute`: written, never read |
| lib/ecowitt/hass_api.py:5-6,30,55-56; fallback.py:44 | 6 | `fast_live`, `minute_lines` flags never read; `STALE_SECONDS` shadowed by fallback.py's |
| lib/ecowitt/api.py:62-64 | 3 | `UNITS`: only tests/old_cache.py uses it; move there. Stale comments at api.py:44, 62-63 describe the old cache |
| lib/storage/read.py:122-123, 196-202, 204-214, 255-268 | 35 | `Reader.gaps` (no caller), `extra` (tests only), `rain_slots` and `records` (scripts/migrate_storage.py and tests only; production uses lib/rain.py and period.py) |
| lib/storage/__init__.py:54-56; catalogue.py:225 | 5 | `Sensors.get`, `RES` |
| lib/storage/write.py:154-172 | 8 | nested-transaction depth: nothing nests |
| lib/warm.py:29-31 | 3 | stray blank lines |
| lib/specs.py:57, 128 | 4 | `Bars.values` (implied by `per`), `Compass.speed_steps` default (always passed) |

About 95 lines. Test-only seams that are not dead but exist for tests: `particulates.check(latest, now)`, the `requests` counters on every
source (only `warm()`'s unread return string uses them), the six `getattr`/`hasattr` shims in bot.py:334-335,342,932,1013,1090,1151 that
let test fakes lack attributes. Replacing the shims with plain attribute access and giving the fakes the attributes is small but tightens
the contracts.

## 2. One-off and measurement code that can retire (both retired on 29 October 2026, after the dead code)

| where | lines | what | recommendation |
|---|---|---|---|
| lib/storage/migrate.py (whole), write.py `exact=` 219-227, __init__.py `bulk` 82-109/162-163, ecobot.py 76-79, `open_storage` in both sources | ~510 (+122 scripts/migrate_storage.py, +300 tests, conftest `old_archived_cache`) | the one-off move of the old files into ecobot.sqlite, its checks, and the merge of a part into an existing file | Retire once the old files have been moved on every deployment and deleted. Until then it still fires when a part is enabled later (POLLEN switched on with conditions_cache.sqlite still present). Carries a second upsert SQL and a second "hourly day" rule that go with it |
| lib/counts.py (whole), ecobot.py:225, bot.py:1071/1199/1025, warm.py:256, storage `counts.connect`/`uncounted` (6 sites), tools timing fields | ~110 | SQL-statement and thread-hop counters via a sqlite3 Connection subclass and a monkeypatched `asyncio.to_thread`, for the DEBUG `Chart ms` line and the bench script | The redesign it measured is done. Remove from the production path (the monkeypatch of `asyncio.to_thread` at start is the riskiest line in the repo for its value); keep wall-clock timing in scripts/bench_storage.py |
| lib/config.py:190-192, 290-291 | 6 | the three old file paths on Config | go with the migration |
| ecobot.py:252-253 | 2 | "XAI_API_KEY is no longer used" hint | removed |

## 3. Optional features: keep or drop is the owner's call

| feature | lines | where | note |
|---|---|---|---|
| ADMIN_ONLY | ~120 | lib/admins.py, bot.py:53-54,334-335,364-394 and the `_allowed` calls, ecobot.py:255-259, config.py:109-119,221-222 | documented and tested. If kept, `_is_group_admin` (bot.py:390-394) should use `Admins`' cache (one `get_chat_member` call per press saved) |
| TELEGRAM_PRELOAD_CHAT | ~95 | bot.py:339-348,997-1063,1203-1204; periods.py:80,112-115,130-132; config.py:142-150,224 | self-contained; `_draw_ahead`, `has_copy`, PRELOAD_TTL have no other caller |
| "Thinking..." draft preview | ~45 | bot.py:207-241,50-51,987,1089-1091,1124; llm.py streaming branch | the newest Telegram feature; without it `keep_typing` suffices and llm.py loses a code path (always stream: -8) |
| AirGradient's own temperature/humidity on charts | ~60 | compose.py:60-68,33,192-208; panels.py:49-69; series.py:64-69; intent.py:382-389; tools.py `asked=`/`schemas_for`; airgradient extras | one eval case; the `Tool.asked` mechanism exists for it |
| mDNS for a `.local` AIRGRADIENT_LOCAL_URL | 33 + the zeroconf dependency | lib/airgradient/local.py:32-64 | drop if the URL is an IP or a DNS name |
| PM2.5 correction note on the start-up line | 28 | airgradient/source.py:176-185,71,167-168,202-203; local.py:126-138 | only when the monitor is the sole source; informational |
| Home Assistant's bands via `import_ranges` | ~55 | ecowitt/homeassistant.py:29-30,52-67,156-167; ecowitt/source.py:86-87,276-293; ecobot.py:172-173 | only needed for ECOWITT_SOURCE=cloud with HOMEASSISTANT_URL set; in auto/homeassistant modes `hass_api.history` already writes the same bands, with a different bucket alignment (mark-centred vs bucket-start). Pick one alignment, then gate or drop |
| `invert_second_axis` | ~10 | compose.py:52-53,197-202; panels two_axes; specs Panel.flip_right; charts `_right_axis(flip)` | no eval, one test |
| FEELS_LIKE_IN_ALL / VPD_IN_ALL | ~10 | intent.py:362-368, config.py:214-215, ecobot.py:232-233 | runtime flags for which panels "weather all week" draws |
| Several AirGradients in Home Assistant | ~12 | airgradient/homeassistant.py:38-57; source.py:217,225-227 | the nearest-reading tie-break; one monitor in practice |
| `/alerts on|off` text form | 8 | bot.py:472-479 | pre-menu; the menu does it |
| watchdog stack dump, `on_other` draft diagnostic, heavy-question warning | 44 | bot.py:57,101-109,1092,1122; 865-871,358; 941-946 and llm.py:17-20,104-113,124 | diagnostics from recent debugging; drop when no longer chased |
| trace-rain footnote in weather_days | 22 | ecowitt/days.py:128-149 | "days that would rank higher with a trace of rain" |
| pollen ETag/304 conditional request | ~10 | pollen/source.py:54,84,90,114-121 | polite to a scraped site; cheap |
| irrigation menu text re-parse | 10 | irrigation/menu.py:38-47, bot.py:637-638 | saves one Tuya request per open/close press |

About 450 lines if all were dropped; realistically the owner keeps most.

## 4. Duplication and simplification (no behaviour change)

*Done on 29 October 2026, except: the Help text and the prompt's capabilities list stay two hand-written lists (they say different things), and the cloud-only AirGradient day stays on its in-memory cache (the store's gap-filling is tuned for a monitor that writes its own readings: through it, a chart of today could run a bucket further behind). The start-up `refresh_keyboards` went on the owner's say-so (the next reply brings the new buttons); the `getattr` shims stay (dozens of tests build a Bot from a bare namespace).*

| where | lines saved | what | recommendation |
|---|---|---|---|
| lib/intent.py:336-452 | ~35 | three fast-path deciders (`weather_chart`, `weather_average`, `weather_period`) repeat the same guard chain; `weather_groups` re-implements `_sides`; `EXTREMES` and `HILO` near-identical; six overlapping "not simple" word lists | one `_simple_weather(text, now)` with one guard chain and one base blacklist plus per-path extras; the 17 fast-path tests still apply |
| lib/intent.py:620-716 | ~25 | `count_days_call` and `period_figures_call` repeat the reading resolution and date formatting; `_by` duplicates `daytable.grouping_in` | one shared resolver; call `grouping_in` |
| lib/intent.py (20 patterns) | ~15 of regex text | alternations no eval or test exercises: OR_LESS variants, AT_LEAST "hit", HIGHER/LOWER "calmest/smallest/biggest/strongest", DASHBOARD "dials", OPINION extras, DESCRIBE "pretend/imagine", ANALYSIS "coincid/same day", LINK "cross-check", PER_DAY "breakdown/day by day", COUNTING "top \d/annual", TWO_AXES "twin/double", GRAPH "visuali", AIR "smok/pollut", WEATHER "stuffy/frost/hail/snow/fog/drizzle/showers", TIME_WORDS "ytd", COMMAND extras | the review burden, not the lines, is the cost: either add one eval each or prune the ones in deciders that force a code answer |
| lib/bot.py | ~45 | four copies of "not modified" suppression (575,694,727,774) and four of `suppress(TelegramError)`+`answer_query` (383,411,428,437); two `gather(_written_in_code, _status_gauges)` (704-705, 1143-1144) with `_nothing`; `Tasks` in ecobot.py duplicates `Bot.background`; cached-period path (748-760) parallel to `_redraw` (762-780); three `IrrigationError` handlers (641-669); startup `refresh_keyboards` (820-835) redundant with the lazy re-send; `report_caption` double trim (257-266) | helpers `_edit_quietly`, `_report_with_gauges`; pass `Tasks` into Bot; route the cached press through `_redraw` |
| lib/ecowitt/query.py:420-463; link.py:56-58 | ~20 | three copies of "shade wind up to the gusts"; `_drawing`/`_day_line` restate `lines.build_line`'s width decision and `_daily`'s 10am rule so the database can bucket | one `lines.plan(span, resolutions, average)` shared by `build_line` and `_drawing`; one band-merge helper |
| lib/ecowitt/query.py:46-47,163-169,39-42,125 | ~11 | `MAX_ROWS` row budget and `DETAILED_DAYS`: old-cache memory guards. The budget still bites: three or more groups over a year fall back to 10am daily records although charts use database buckets now; the 31-93 day gap in `detailed` loses the monthly note | `_held_locally` = one `covered` call; `detailed = held`. A behaviour fix as well as a cut |
| lib/ecowitt (small) | ~50 | fallback.py:61-79 once-per-outage logging done two ways (give the cloud a `Trouble`); `live_rain` (source.py:351-361) has its own live fetch beside `_live_data`; `start()` three identical try/excepts (192-203); `_log_empty` 12 lines; `hass_api._joined` = `hass_history.merged`; inline `_settled`, `covered`, `point`; fold minutes.py's three constants | |
| lib/storage | ~25 | `_number` duplicated in write.py:45-50 and migrate.py:322-327; band MIN/MAX/COALESCE formula hand-written three times (write.py:93-94,107-108,124-126); `fine_of`/`FINE`/`DEFAULT_FINE` with one caller (days.py:37-43); schema version kept twice (PRAGMA and `meta.schema`); `DAY_KINDS` alias; `MIN_READINGS`, `SLACK_SECONDS`, `BUCKET` re-declared instead of imported | |
| lib/airgradient | ~40 | per-day "held/hourly/count" computed three ways (`_counts_on`, `hourly_days`, `day_figures` in readings.py:85-113,250-257; `_held`/`_unstored` in source.py) with two read hops per `rows()`; `checked` and `gaps` in homeassistant.py:138-165 walk the same data twice; `history()` builds the lo/hi/avg entry twice (source.py:695-712); `_recent` cache plus `_with_spreads` (source.py:140,416,431-436,521,668-672) serve only the cloud-only path | one `AirReadings.held(days)` read; one function returning bands, fills and verdicts; route the cloud-only day through the store |
| lib/alerts, pollen, forecast | ~60 | `Press` dataclass round-trip (alerts/menu.py:47-89) vs the irrigation menu's f-string callbacks; BOM-era `EMOJI` table, `forecast_emoji`, `decorate`, `source` attribute (forecast/source.py:37-58,98-221); `kind=None` paths in notify.py used only by tests; `confirm.py`+`backoff.py` could be one module; `lib/alerts/__init__.py` re-export shim | emoji into the WMO table; required `kind` |
| lib/config.py:72-81,103-106,160-161 | ~10 | `_source`/`_air_source` docstring-only wrappers over `_one_of`; `_hour`, `_decimal` wrappers | `functools.partial` |
| lib/templates.py:192-204 vs prompt.py:174-211 | ~10 | two hand-written "what the bot measures" lists (Help button, model prompt) | one table |
| charts, daytable, gauges, rain | ~30 | `_time_axis` five-tier ladder as a table (286-302); `_setup_fonts` three globs and six-name chain (116-134); `daytable.prepare` re-parses dates as `timeutil.parse_period` does (136-142); `_mix` in charts and gauges; `plain_lookup` if-chain as a table (intent.py:862-885); `numbered_span` three guards as one (intent.py:271-283); Tile/Dashboard `__post_init__` checks of code-built values (specs.py:215-232) | |

About 380 lines, none changing an answer or a picture.

Simplifications that change the golden chart images (owner's call, look at the result first):

| where | lines | what |
|---|---|---|
| charts.py:421-470, specs Panel.peaks "aside", LEADER_CLEARANCE | ~60 | `_mark_highs`: peak pills in empty columns with leader lines; only the three-line particles panel uses it. Pills may crowd on three lines, which is why it exists |
| charts.py:644-690 and two exactness tests | ~45 | hand-written greedy 256-colour quantiser so flat colours stay exact; PIL's `quantize` shifts a few edge levels |
| charts.py:366-375 and a 25-line test | 8 | `_rose_radius` reproduces matplotlib's sticky-edge autoscale |

## 5. Special handling that should stay (verified, with the reason)

Each handles a real path; most have a test. Line counts are approximate.

Telegram and the bot (bot.py, notify.py, logs.py): `answer_query` swallowing "query is too old" after a restart (11); `ChatMigrated` on a press under an old alert (4); `_press` refusing callback data not on the message's own keyboard (18, security); messages older than 10 minutes ignored (4); the `_send` error ladder in notify.py:252-283 (ChatMigrated, Forbidden and "chat not found" marking a chat gone, RetryAfter and NetworkError one retry, catch-all so other chats still get the alert; ~30, all tested); photo refused falls back to text (7); file-id reuse so a chart is uploaded once (15); the `_flood_wait` int-or-timedelta shim (4, until the PTB upgrade settles); state file moved aside as `.damaged-…` when unreadable (5); group-to-supergroup baseline move (3); reply-keyboard versioning so a changed keyboard reaches phones (~30; only the startup `refresh_keyboards` is redundant); the after-timeout retry at one reasoning step lower (19); `shorten_old` and the identical-tool-call cache in llm.py (37); topic memory (33, what keeps the prompt short and cacheable); `commit_in` reading .git by hand (15); TZ and token/network exits in ecobot.py (11); journald priorities and the httpx line strip (22).

Alerts: quiet hours across midnight with the overnight summary (~50, configured by RAIN_QUIET_HOURS); "rain ended while not watching" closing a spell after downtime (3, required: without it `wet_times[0]` raises; worth a test); `_fell` re-reading a long spell's start (6); gusts only newer than the last check (2); `check_rain` on the live reading with its lock (13, Home Assistant only); irrigation's 24-hour delay renewal (7, needed: the check runs when the delay lapses); stale forecast and controller-unreachable rules (14); the watchdog closing a valve after a run (47, safety); Tuya signing, token retry and shape checks (20).

Ecowitt: `_TurnLock` one request at a time with an urgent lane (31; "Operation too frequent" is real); retries with backoff and key redaction (38); `UNIT_FIXES` for VPD in inHg (27, about the live API, not only old files); `daily_run` DST wording (7); archive probing groups singly after a refusal and dropping the piezo gauge (20); `OUTAGE_STOP` and the settle edge snapped to whole buckets (13); `Recent` freshness and provisional/final handling in fetch.py (~45); the HA-cloud fallback itself (~70, this is auto mode); `_look` for HA's first state (11); unit conversion from HA (19); DNS-failure hint (9); the bot's own 5-minute bands from live polling (~70, the only band source in cloud-only mode); parts of days at the finest held resolution and `_exact` (27); `only=` dropped when the question never said holiday (6); `DERIVED` fields hidden unless asked (12); `_in_focus`, `rain_from_daily`, `monthly_note` (30); HA-only mode adopting the file's MAC and archive pace (8).

Storage and AirGradient: `NewerFile` refusing a newer schema (6); ALTER TABLE for new catalogue columns (8); unknown time zone name (7); unknown Ecowitt group kept in `extra` (8); UV written whole or with one decimal (3); refetch when a group gains a field (6); the coverage rules of a fetch (10); day rows recomputed when the zone changes (15); a band arriving before its reading (10); daily records taking lo/hi times from the 4-hour readings (20); 23/25-hour days and UTC-day labels (13); half hours with finer readings bucketed from those alone (3); the spike rule in SQL and in Python (55: the SQL feeds the day rows at write time, the Python filters read-back rows for charts; a test checks they agree); a day refetched at the other resolution (4); cloud 404 as empty (2); backfill empty-day stop rules (14); the per-metric gap rule and the agreement verdicts (~90, plan 3.12); rating on the last two readings and `sustained` for the particulates alert (30); AIRGRADIENT_SOURCE modes (10).

Charts and intent: rain behind the lines, zero floor, negative bars (25); DST-correct time axis, local-midnight bars, clock slots (25); smoothing and the 10am daily records (9); `_pills` collision resolution (14); `model_prefetch` for "will it rain" (28, saves a model round trip); OPINION/RIGHT_NOW (6); plot_chart validation of model-supplied arguments (14); `grouping_in` fallback when the model omits group_by (18).

## 6. Totals and recommendation

| bucket | lines | risk |
|---|---|---|
| dead | ~95 | none |
| one-off and measurement (migration, counts) | ~620 | none once the migration has run everywhere |
| duplication and simplification, no behaviour change | ~380 | low; existing tests cover it |
| golden-image simplifications | ~110 | visual check, regenerate goldens |
| optional features, owner decides | ~450 | drops a feature each |

Realistic: about 1,100 lines (10% of the code) with no feature lost, 1,200 with the picture changes, up to 1,700 if every optional feature
the owner does not use goes. What is left is features the docs advertise: two sensors with three sources each, Home Assistant, the
archive, the alerts, irrigation, pollen, forecast, the charts, the LLM routing. At 11,150 code lines for that scope the code is not bloated
so much as wide; shrinking it further means dropping features, not cleaning code.

Suggested order: (1) the dead batch and the counts instrumentation, one commit, no risk; (2) the no-behaviour-change simplifications in
intent.py, bot.py and query.py, which also fix the `MAX_ROWS` fallback; (3) the owner decides on ADMIN_ONLY, TELEGRAM_PRELOAD_CHAT, the
draft preview, mDNS and the other optional features; (4) retire the migration after the old files are deleted from the data folder.
