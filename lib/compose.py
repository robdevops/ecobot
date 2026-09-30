"""Charts across both sources, composed by the model from a fixed list of series and styles.

The model names what to plot (`plot_chart`: up to four panels, each a series and a style); this module loads the data
from the caches, puts it on one time axis and hands the chart to the renderer. The model never supplies numbers or
drawing code, so a chart the bot has never drawn before is still made from checked data. Only the cached readings are
used for the weather station (nothing is fetched); AirGradient fills any missing days first, as its own tool does."""

import json
import logging
from datetime import date, datetime, time, timedelta, tzinfo

from .airgradient.metrics import ALL_METRICS, CHART_UNITS, LABELS, RATINGS
from .charts import CHART_REQUESTS, COMPOSED_CHART_HINT
from .ecowitt.link import bar_layout, driver_series, rain_bars, rain_slots
from .timeutil import now_local
from .tools import Tool

log = logging.getLogger(__name__)

SLOT = 1800                   # both sources are lined up on 30-minute slots
MAX_PANELS = 4
DEFAULT_DAYS = 30
# Weather station series: name -> (group, field, label, unit)
ECOWITT = {"temperature": ("outdoor", "temperature", "Temperature", "°C"), "humidity": ("outdoor", "humidity", "Humidity", "%"),
           "pressure": ("pressure", "relative", "Pressure", "hPa"), "wind": ("wind", "wind_speed", "Wind", "km/h"),
           "rain": ("rainfall", "daily", "Rain", "mm")}
SERIES = [*ECOWITT, *ALL_METRICS]
STYLES = ("line", "bars", "rating")
RATING_KEYS = ("good", "poor", "very poor")

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


def _error(text: str) -> str:
    return json.dumps({"error": text}, ensure_ascii=False)


def _class(value: float, limits: tuple[float, float]) -> int:
    """0 good, 1 poor, 2 very poor."""
    return 0 if value <= limits[0] else 1 if value <= limits[1] else 2


def rating_shares(values: dict[int, float], limits: tuple[float, float], tz: tzinfo, first: date, last: date) -> dict:
    """Per bar (see bar_layout): the percentage of its 30-minute readings that were good, poor and very poor."""
    origin, width, per = bar_layout(tz, first, last)
    counts: dict[int, list[int]] = {}
    for t, v in values.items():
        k = origin + (t - origin) // width * width
        counts.setdefault(k, [0, 0, 0])[_class(v, limits)] += 1
    xs = sorted(counts)
    total = {k: sum(counts[k]) for k in xs}
    return {"x": xs, "width": width, "per": per,
            **{name: [round(100 * counts[k][i] / total[k], 1) for k in xs] for i, name in enumerate(RATING_KEYS)}}


class Composer:
    """Builds cross-source charts from the weather station's cache and the AirGradient sensor."""

    def __init__(self, eco, air):
        self.eco, self.air, self.tz = eco, air, eco.tz
        self.tools = [Tool("plot_chart", PLOT_DESCRIPTION, PLOT_PARAMETERS, self.plot_chart)]

    def period(self, args: dict) -> tuple[date, date] | str:
        """(first, last) day, or an error text. The last day is at most yesterday: today is still settling."""
        yesterday = now_local(self.tz).date() - timedelta(days=1)
        try:
            last = min(date.fromisoformat(str(args["end_date"])[:10]), yesterday) if args.get("end_date") else yesterday
            first = date.fromisoformat(str(args["start_date"])[:10]) if args.get("start_date") else last - timedelta(days=DEFAULT_DAYS - 1)
        except ValueError as e:
            return f"bad date ({e}); use 'YYYY-MM-DD'"
        return (first, last) if first <= last else "start_date must be before end_date (the latest day is yesterday)"

    def _bounds(self, first: date, last: date) -> tuple[int, int]:
        return (int(datetime.combine(first, time()).replace(tzinfo=self.tz).timestamp()),
                int(datetime.combine(last, time(23, 59, 59)).replace(tzinfo=self.tz).timestamp()))

    def weather(self, group: str, field: str, first: date, last: date) -> tuple[dict, dict, dict]:
        """(values, lows, highs) per 30-minute slot from the cache; lows and highs are Ecowitt's own where it gives them."""
        lo, hi = self._bounds(first, last)
        got = self.eco.cache.load_fields(self.eco.mac, "30min", group, [field, field + "_low", field + "_high"], lo, hi)
        pick = lambda f: {int(t): float(v) for t, v in got.get(f, {"list": {}})["list"].items()}
        return pick(field), pick(field + "_low"), pick(field + "_high")

    async def air_slots(self, metric: str, first: date, last: date) -> tuple[dict, dict, dict, dict]:
        """(values, lows, highs, notes): the sensor's readings averaged into 30-minute slots, with each slot's range."""
        rows, hourly, skipped = await self.air.rows(datetime.combine(first, time()), datetime.combine(last, time(23, 59, 59)))
        slots: dict[int, list[float]] = {}
        for r in rows:
            if metric in r:
                slots.setdefault(r["ts"] // SLOT * SLOT, []).append(r[metric])
        notes = {}
        if skipped:
            notes["missing"] = f"{len(skipped)} older day(s) aren't in the bot's cache yet and were left out"
        if hourly:
            notes["resolution"] = f"{hourly} day(s) only exist as hourly averages"
        return ({t: sum(v) / len(v) for t, v in slots.items()}, {t: min(v) for t, v in slots.items()},
                {t: max(v) for t, v in slots.items()}, notes)

    async def plot_chart(self, args: dict) -> str:
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
        per = next((p["bars"]["per"] for p in built if "bars" in p), next((p["shares"]["per"] for p in built if "shares" in p), None))
        for p in built:
            for kind in ("bars", "shares"):
                if kind in p:
                    p[kind].pop("per", None)
        spec = {"kind": "stack", "title": " and ".join(p["label"] for p in built),
                "subtitle": f"{first:%a} {first.day} {first:%b} – {last:%a} {last.day} {last:%b %Y}" + (f"  ·  per {per}" if per else ""),
                "panels": built}
        out = {"period": f"{first} to {last}", "panels": summary, **({"notes": notes} if notes else {})}
        holder = CHART_REQUESTS.get()
        if holder is not None:
            holder.append(spec)
            out["chart"] = COMPOSED_CHART_HINT
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    async def _panel(self, name: str, style: str, first: date, last: date, notes: dict) -> tuple[dict | None, dict]:
        """(the panel, its figures for the caption); the panel is None when there is nothing to draw."""
        if name in ECOWITT:
            group, field, label, unit = ECOWITT[name]
            values, lows, highs = self.weather(group, field, first, last)
            if name == "rain":
                bars = rain_bars(rain_slots(values), self.tz, first, last)
                return ({"label": label, "unit": unit, "bars": bars} if bars["x"] else None,
                        {"series": name, "total_mm": round(sum(bars["y"]), 1), "wet_bars": len(bars["y"])})
            if name == "wind":
                gust, _, gust_high = self.weather(group, "wind_gust", first, last)
                highs = {t: max(gust.get(t, 0.0), gust_high.get(t, 0.0)) for t in {*gust, *gust_high}}
            line = driver_series(values, self.tz, first, last, label, lows, highs, keep_band=name == "wind")
            facts = {"series": name, **self._stats(values, unit)}
            return ({"label": label, "unit": unit, "series": [line]} if line else None), facts
        values, lows, highs, more = await self.air_slots(name, first, last)
        notes.update(more)
        if len(values) < 2:
            return None, {}
        if style == "rating":
            shares = rating_shares(values, RATINGS[name], self.tz, first, last)
            totals = [0, 0, 0]
            for v in values.values():
                totals[_class(v, RATINGS[name])] += 1
            overall = {k: round(100 * n / len(values), 1) for k, n in zip(RATING_KEYS, totals)}
            return ({"label": f"{LABELS[name]} rating", "unit": "%", "shares": shares},
                    {"series": name, "style": "rating", "share_of_time_percent": overall})
        line = driver_series(values, self.tz, first, last, LABELS[name], lows, highs)
        panel = {"label": LABELS[name], "unit": CHART_UNITS[name], "series": [line], "zones": list(RATINGS[name])}
        return panel, {"series": name, **self._stats(values, CHART_UNITS[name])}

    @staticmethod
    def _stats(values: dict, unit: str) -> dict:
        v = list(values.values())
        return {"mean": round(sum(v) / len(v), 1), "min": round(min(v), 1), "max": round(max(v), 1), "unit": unit} if v else {}
