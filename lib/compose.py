"""Charts across both sources, composed by the model from a fixed list of series and styles.

The model names what to plot (`plot_chart`: up to four panels, each a series and a style); this module loads the data
from the caches, puts it on one time axis and hands the chart to the renderer. The model never supplies numbers or
drawing code, so a chart the bot has never drawn before is still made from checked data. Only the cached readings are
used for the weather station (nothing is fetched); AirGradient fills any missing days first, as its own tool does."""

import asyncio
import json
import logging
from datetime import date, datetime, time, tzinfo

from .airgradient.metrics import ALL_METRICS, CHART_UNITS, LABELS, RATINGS, ZONES, zone
from .analysis import scan
from .analysis.pairs import analyse_air
from .charts import COMPOSED_CHART_HINT, wants_chart
from .ecowitt.link import driver_series
from .series import WEATHER
from .specs import Panel, Shares, stack
from .rain import bar_layout, rain_bars, rain_slots
from .timeutil import SLOT, day_bounds, now_local, parse_period
from .tools import Tool, Turn

log = logging.getLogger(__name__)

MAX_PANELS = 4
DEFAULT_DAYS = 30
# Weather station series: name -> (group, field, label, unit); the composer draws the wind as its average speed
ECOWITT = {**{n: (r.group, r.field, r.label, r.unit) for n, r in WEATHER.items()}, "wind": ("wind", "wind_speed", "Wind", "km/h")}
SERIES = [*ECOWITT, *ALL_METRICS]
STYLES = ("line", "bars", "rating")

PLOT_DESCRIPTION = (
    "Plot readings from the weather station and the air-quality sensor together on one time axis, one panel each: "
    "'plot the traffic light rating of air quality against rainfall', 'plot PM2.5 and humidity'. Each panel is a series and "
    "a style. Styles: 'line' (the reading; long periods are averaged, with the day's range shaded), 'bars' (rain only), "
    "'rating' (air-quality series only: the share of time in good, poor and very poor, as traffic-light bars). "
    "Bars and ratings are per hour for a few days, per 6 hours up to a month, per day beyond. Rain defaults to bars and "
    "everything else to line. Default period: the last 30 days up to yesterday.")
PLOT_PARAMETERS = {
    "type": "object",
    "properties": {
        "panels": {"type": "array", "minItems": 1, "maxItems": MAX_PANELS, "description": "Top to bottom.",
                   "items": {"type": "object", "properties": {"series": {"type": "string", "enum": SERIES},
                                                              "style": {"type": "string", "enum": list(STYLES)}},
                             "required": ["series"]}},
        "start_date": {"type": "string", "description": "First day, 'YYYY-MM-DD'. Default: 30 days before the end."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."},
    },
    "required": ["panels"],
}


SCAN_DAYS = 90
SCAN_AIR = ("pm2_5", "pm10", "co2", "voc_index", "nox_index")
# What the scan compares the air with: name -> (group, field, label, unit)
SCAN_WEATHER = {"temperature": ("outdoor", "temperature", "temperature", "°C"), "humidity": ("outdoor", "humidity", "humidity", "%"),
                "dew_point": ("outdoor", "dew_point", "dew point", "°C"), "pressure": ("pressure", "relative", "pressure", "hPa"),
                "wind_speed": ("wind", "wind_speed", "wind speed", "km/h"), "wind_gust": ("wind", "wind_gust", "wind gusts", "km/h")}
SCAN_DESCRIPTION = (
    "Which readings go with air quality? Scans every air-quality metric (PM2.5, PM10, CO2, VOC, NOx) against the weather "
    "(temperature, humidity, dew point, pressure and its 3-hour change, wind speed, gusts, wind direction, rain), allowing for "
    "the daily cycle and for chance, and reports only the relationships that stand out, with a plain verdict. Use it for 'is "
    "there a correlation between air quality and other metrics', 'what affects air quality'. For rain alone use air_link. "
    "Default period: the last 90 days.")
SCAN_PARAMETERS = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": ["all", *SCAN_AIR], "description": "One air reading, or all (default)."},
        "start_date": {"type": "string", "description": "First day, 'YYYY-MM-DD'. Default: 90 days before the end."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."},
        "chart": {"type": "boolean", "description": "Set true for a chart of the strongest relationship found."},
    },
}
LINK_DAYS = 60
LINK_METRICS = ("pm2_5", "pm10")
LINK_DESCRIPTION = (
    "Does rain go with cleaner air? Reads the air-quality sensor (PM2.5 by default) and the weather station's rain together at "
    "30-minute resolution and reports whether the air was cleaner in the hours after rain than before it, its level in wet and "
    "dry periods, and wet days against dry days, with a plain verdict. Use it for 'is there a correlation between air quality "
    "and rainfall', 'does rain clear the air'. Default period: the last 60 days.")
LINK_PARAMETERS = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": list(LINK_METRICS), "description": "The air reading. Default pm2_5."},
        "start_date": {"type": "string", "description": "First day, 'YYYY-MM-DD'. Default: 60 days before the end."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."},
        "chart": {"type": "boolean", "description": "Set true for a chart: the reading (with its rating zones) over rain bars."},
    },
}


def _error(text: str) -> str:
    return json.dumps({"error": text}, ensure_ascii=False)


def rating_shares(values: dict[int, float], limits: tuple[float, float], tz: tzinfo, first: date, last: date, label: str) -> Shares:
    """Per bar (see bar_layout): the percentage of its 30-minute readings that were good, poor and very poor."""
    origin, width, per = bar_layout(tz, first, last)
    counts: dict[int, list[int]] = {}
    for t, v in values.items():
        k = origin + (t - origin) // width * width
        counts.setdefault(k, [0, 0, 0])[zone(v, limits)] += 1
    xs = sorted(counts)
    total = {k: sum(counts[k]) for k in xs}
    good, poor, very_poor = ([round(100 * counts[k][i] / total[k], 1) for k in xs] for i in range(3))
    return Shares(label, xs, width, good, poor, very_poor, per)


class Composer:
    """Builds cross-source charts from the weather station's cache and the AirGradient sensor."""

    def __init__(self, eco, air):
        self.eco, self.air, self.tz = eco, air, eco.tz
        self.tools = [Tool("plot_chart", PLOT_DESCRIPTION, PLOT_PARAMETERS, self.plot_chart),
                      Tool("air_link", LINK_DESCRIPTION, LINK_PARAMETERS, self.air_link),
                      Tool("air_scan", SCAN_DESCRIPTION, SCAN_PARAMETERS, self.air_scan)]

    def period(self, args: dict, default_days: int = DEFAULT_DAYS) -> tuple[date, date] | str:
        """(first, last) day, or an error text. The last day is at most yesterday: today is still settling."""
        return parse_period(args, now_local(self.tz).date(), default_days)

    def weather(self, group: str, field: str, first: date, last: date) -> dict[int, float]:
        """One reading per 30-minute slot from the weather station's cache."""
        return self.weather_band(group, field, first, last, bands=False)[0]

    def weather_band(self, group: str, field: str, first: date, last: date, bands: bool = True) -> tuple[dict, dict, dict]:
        """(values, lows, highs) per 30-minute slot; lows and highs are Ecowitt's own where it gives them."""
        lo, hi = day_bounds(first, self.tz)[0], day_bounds(last, self.tz, last_second=True)[1]
        fields = [field, field + "_low", field + "_high"] if bands else [field]
        values, lows, highs = [*self.eco.cache.slots(self.eco.mac, "30min", group, fields, lo, hi), {}, {}][:3]
        return values, lows, highs

    async def air_series(self, metrics: list[str], first: date, last: date) -> tuple[dict[str, tuple[dict, dict, dict]], dict]:
        """({metric: (values, lows, highs)}, notes): the sensor's readings averaged into 30-minute slots, with each slot's
        range, from one load of the period."""
        rows, hourly, skipped = await self.air.rows(datetime.combine(first, time()), datetime.combine(last, time(23, 59, 59)))
        out = {}
        for metric in metrics:
            slots: dict[int, list[float]] = {}
            for r in rows:
                if metric in r:
                    slots.setdefault(r["ts"] // SLOT * SLOT, []).append(r[metric])
            out[metric] = ({t: sum(v) / len(v) for t, v in slots.items()}, {t: min(v) for t, v in slots.items()},
                           {t: max(v) for t, v in slots.items()})
        notes = {}
        if skipped:
            notes["missing"] = f"{len(skipped)} older day(s) aren't in the bot's cache yet and were left out"
        if hourly:
            notes["resolution"] = f"{hourly} day(s) only exist as hourly averages"
        return out, notes

    async def air_slots(self, metric: str, first: date, last: date) -> tuple[dict, dict, dict, dict]:
        """(values, lows, highs, notes) for one air metric; see air_series."""
        series, notes = await self.air_series([metric], first, last)
        return (*series[metric], notes)

    async def plot_chart(self, args: dict, turn: Turn | None = None) -> str:
        turn = turn or Turn()
        panels = args.get("panels")
        if not isinstance(panels, list) or not 1 <= len(panels) <= MAX_PANELS:
            return _error(f"give 1 to {MAX_PANELS} panels, each with a series and a style")
        if isinstance(period := self.period(args), str):
            return _error(period)
        first, last = period
        built, summary, notes = [], [], {}
        for p in panels:
            name, style = p.get("series"), p.get("style") or ("bars" if p.get("series") == "rain" else "line")
            if name not in SERIES or style not in STYLES:
                return _error(f"series must be one of {', '.join(SERIES)} and style one of {', '.join(STYLES)}")
            if (style == "bars") != (name == "rain") or (style == "rating" and name not in ALL_METRICS):
                return _error("'bars' is for rain only, and 'rating' for the air-quality series only; use 'line' otherwise")
            panel, facts = await self._panel(name, style, first, last, notes)
            if panel is None:
                return _error(f"no cached readings of {name} for {first} to {last}")
            built.append(panel)
            summary.append(facts)
        out = {"period": f"{first} to {last}", "panels": summary, **({"notes": notes} if notes else {})}
        self._add_chart(out, built, first, last, turn)
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    def _add_chart(self, out: dict, panels: list[Panel], first: date, last: date, turn: Turn):
        """Send the panels as one chart (rain behind the first line) and tell the model what its reply is for."""
        turn.charts.append(stack(panels, first, last))
        out["chart"] = COMPOSED_CHART_HINT

    async def air_link(self, args: dict, turn: Turn | None = None) -> str:
        turn = turn or Turn()
        metric = args.get("metric") or "pm2_5"
        if metric not in LINK_METRICS:
            return _error(f"metric must be one of {', '.join(LINK_METRICS)}")
        if isinstance(period := self.period(args, LINK_DAYS), str):
            return _error(period)
        first, last = period
        values, lows, highs, notes = await self.air_slots(metric, first, last)
        air_panel, _ = await self._panel(metric, "line", first, last, {}, data=(values, lows, highs))
        rain = rain_slots(self.weather("rainfall", "daily", first, last))
        result = analyse_air(values, rain, RATINGS[metric], self.tz, LABELS[metric])
        out = {"period": f"{first} to {last}", "metric": f"{LABELS[metric]} ({CHART_UNITS[metric]})",
               "resolution": "30-minute slots (air readings averaged, rain from the weather station's cache)",
               "how_to_read": ("Open with `verdict`, then the evidence in `findings`. Rain and clean air share weather (wind, fronts), "
                               "so say the air goes with rain or not, never that rain causes it. A rank correlation near 0 is a "
                               "rough guide, not proof of no link."),
               **(result or {"note": "No overlapping air-quality and rain readings for this period. Say exactly that."}),
               **({"notes": notes} if notes else {})}
        bars = rain_bars(rain, self.tz, first, last)
        if result and air_panel and bars.x and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
            self._add_chart(out, [air_panel, Panel("Rain", "mm", bars=bars)], first, last, turn)
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    async def air_scan(self, args: dict, turn: Turn | None = None) -> str:
        turn = turn or Turn()
        metric = args.get("metric") or "all"
        if metric != "all" and metric not in SCAN_AIR:
            return _error(f"metric must be all or one of {', '.join(SCAN_AIR)}")
        if isinstance(period := self.period(args, SCAN_DAYS), str):
            return _error(period)
        first, last = period
        air, notes = await self.air_series(list(SCAN_AIR) if metric == "all" else [metric], first, last)
        weather = {}
        for name, (group, field, _, _) in SCAN_WEATHER.items():
            if values := self.weather(group, field, first, last):
                weather[name] = values
        if "pressure" in weather:  # falling pressure often comes with stagnant or changing air
            weather["pressure_change"] = {t: v - weather["pressure"][t - 6 * SLOT] for t, v in weather["pressure"].items()
                                          if t - 6 * SLOT in weather["pressure"]}
        rain = rain_slots(self.weather("rainfall", "daily", first, last))
        if rain:
            weather["rain"] = rain
        direction, speed = self.weather("wind", "wind_direction", first, last), weather.get("wind_speed")
        air_names = {m: (LABELS[m], CHART_UNITS[m]) for m in air}
        weather_names = {**{k: (v[2], v[3]) for k, v in SCAN_WEATHER.items()}, "pressure_change": ("3-hour pressure change", "hPa"),
                         "rain": ("rain", "mm")}
        origin, days = day_bounds(first, self.tz)[0], (last - first).days + 1
        result = await asyncio.to_thread(scan.scan, {m: v[0] for m, v in air.items()}, weather, direction, speed, origin, days)
        summary = scan.summarise(result, air_names, weather_names)
        strongest = summary.pop("strongest")
        out = {"period": f"{first} to {last}", "resolution": "30-minute slots; each pair compared within the day and day to day",
               "how_to_read": ("Open with `verdict`, then the relationships in `findings`, strongest first. These go together; they "
                               "do not cause each other (shared weather and season). If nothing stands out, say so and that the "
                               "closest are probably chance."), **summary, **({"notes": notes} if notes else {})}
        if strongest and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
            drawn = [n for n in (strongest["air"], {"wind_speed": "wind"}.get(strongest["weather"], strongest["weather"])) if n in SERIES]
            if len(drawn) == 2:
                panels = [(await self._panel(n, "bars" if n == "rain" else "line", first, last, {}, data=air.get(n)))[0] for n in drawn]
                if all(panels):
                    self._add_chart(out, panels, first, last, turn)
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    async def _panel(self, name: str, style: str, first: date, last: date, notes: dict,
                     data: tuple[dict, dict, dict] | None = None) -> tuple[Panel | None, dict]:
        """(the panel, its figures for the caption); the panel is None when there is nothing to draw. `data` is an air series
        already loaded (values, lows, highs)."""
        if name in ECOWITT:
            group, field, label, unit = ECOWITT[name]
            values, lows, highs = self.weather_band(group, field, first, last)
            if name == "rain":
                bars = rain_bars(rain_slots(values), self.tz, first, last)
                return (Panel(label, unit, bars=bars) if bars.x else None,
                        {"series": name, "total_mm": round(sum(bars.y), 1), "wet_bars": len(bars.y)})
            if name == "wind":
                gust, _, gust_high = self.weather_band(group, "wind_gust", first, last)
                highs = {t: max(gust.get(t, 0.0), gust_high.get(t, 0.0)) for t in {*gust, *gust_high}}
            line = driver_series(values, self.tz, first, last, label, lows, highs, keep_band=name == "wind")
            facts = {"series": name, **self._stats(values, unit)}
            return (Panel(label, unit, [line], reading=name) if line else None), facts
        if data is None:
            *data, more = await self.air_slots(name, first, last)
            notes.update(more)
        values, lows, highs = data
        if len(values) < 2:
            return None, {}
        if style == "rating":
            shares = rating_shares(values, RATINGS[name], self.tz, first, last, f"{LABELS[name]} rating")
            totals = [0, 0, 0]
            for v in values.values():
                totals[zone(v, RATINGS[name])] += 1
            overall = {k: round(100 * n / len(values), 1) for k, n in zip(ZONES, totals)}
            return (Panel(shares.label, "%", shares=shares),
                    {"series": name, "style": "rating", "share_of_time_percent": overall})
        line = driver_series(values, self.tz, first, last, LABELS[name], lows, highs)
        if line is None:
            return None, {}
        line.low = line.high = None  # air-quality lines are plain: no shaded range
        return (Panel(LABELS[name], CHART_UNITS[name], [line], zones=tuple(RATINGS[name]), reading=name),
                {"series": name, **self._stats(values, CHART_UNITS[name])})

    @staticmethod
    def _stats(values: dict, unit: str) -> dict:
        v = list(values.values())
        return {"mean": round(sum(v) / len(v), 1), "min": round(min(v), 1), "max": round(max(v), 1), "unit": unit} if v else {}
