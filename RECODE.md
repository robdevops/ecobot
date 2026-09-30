# Recode baseline (before the recode, at 98ebf4c)

```
  5505 total
   699 lib/ecowitt/history.py
   476 lib/charts.py
   381 lib/airgradient/source.py
   349 lib/ecowitt/link.py
   339 lib/intent.py
   332 lib/bot.py
   286 lib/compose.py
   267 lib/alerts/weather.py
   258 lib/correlate.py
   227 lib/ecowitt/store.py
   203 lib/ecowitt/station.py
   200 lib/prompt.py
   190 lib/ecowitt/days.py
   160 envirobot.py
   137 lib/ecowitt/archive.py
   121 lib/llm.py
   113 lib/timeutil.py
   113 lib/alerts/notify.py
   112 lib/ecowitt/api.py
    88 lib/airgradient/metrics.py
    83 lib/ecowitt/direction.py
    71 lib/airgradient/store.py
    59 lib/alerts/air.py
    58 lib/config.py
    54 lib/tools.py
    51 lib/warm.py
    37 lib/ecowitt/calendar.py
    28 lib/ecowitt/glance.py
     5 lib/alerts/__init__.py
     4 lib/ecowitt/__init__.py
     4 lib/airgradient/__init__.py
     0 lib/__init__.py
```

# Outcome (recode branch)

The behaviour, tool contracts and cache schemas are unchanged (245 tests and the 42 routing cases pass; old-version cache
fixtures still load). The core did not get smaller: 5,505 -> 5,602 lines, because the pieces that now exist once
(lines, series, rain, Reading, Turn) replaced repeats that were short, while the structure got clearer.

What did change:
- per-question state travels in a `Turn`, not module-level ContextVars;
- one line builder (`lines.build_line`) replaced five copies of "raw / bucket / daily band / smooth";
- `history.py` (699) is `fetch`, `extremes` and `query`; `link.py` (349) is the tool only, with the analysis in
  `analysis/pairs.py` and the rain helpers in `rain.py`; `correlate.py` is `analysis/scan.py`;
- one weather-series table (`series.py`) feeds the stack chart, the composer and the question recogniser;
- `intent.read` returns one `Reading` used by the bot, the evals and `show_request`;
- the station no longer imports the alert module (the rain outlook lives in `ecowitt/outlook.py`);
- the renderer draws bands and lines in one place.

Left as they were, on purpose: typed chart specs (dict specs are read by many tests and the tools), the alert rules
(already one small method each on a shared rain helper), the SQLite layout (only the reads were tidied).

## Charts (second pass)
Every chart is now a `specs.Chart` of `Panel`s on one shared time axis; the three layouts (single, stack, per-panel grid)
are one renderer. Rain sits behind the first line panel on its own right-hand axis (a rain panel stays separate only when
no line is there to sit behind). A panel can carry a second y axis for a reading in another unit. Air quality is three
panels: CO2 with VOC, PM1/PM2.5/PM10, NOx. Line data is unchanged: on the test fixture caches, every x/y/low/high/bars array
in every chart matches the pre-change dumps exactly (`scripts/dump_specs.py`).

Air quality is four panels, top to bottom: the particles, CO2, VOC and NOx, each on its own scale (`AIR_PANELS` in `airgradient/metrics.py`). Charts render at 1280x720.
Air-quality lines get the shaded low-to-high range when the points go daily, in single and multi-panel charts alike.
On an averaged air chart the labelled peak is the highest average, so the tool result adds `chart_peak` (value, time, averaging) beside the true `high`; `AIR_CHART_HINT` has the caption quote `chart_peak` and mention `high` only as a brief spike well above it.
