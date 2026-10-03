"""Counting, ranking and summing days, whichever device the readings came from (weather_days and air_days share this).

A caller works out one value per day for each reading it offers (a day's highest, lowest or mean) and hands them over; this
finds the days that meet the conditions, ranks them, counts them, and can total, average or take the highest or lowest of a
reading per month or year. A count or figure per month or year is drawn as a bar chart (a number above each bar), not listed.
"""

import operator
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Callable

from .captions import COUNT_CHART_HINT
from .specs import Bars, Chart, Panel
from .timeutil import day_bounds

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq}
SYMBOL = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "="}
STATS = ("count", "max", "min", "avg", "sum")
MAX_LIMIT = 20
PRETTY = {"temp": "temperature", "uv": "UV index", "dew_point": "dew point", "feels_like": "feels-like", "vpd": "VPD",
          "wind_gust": "wind gust", "wind_speed": "wind speed", "rain_rate": "rain rate", "indoor_temp": "indoor temperature",
          "indoor_humidity": "indoor humidity", "solar": "solar radiation", "pm2_5": "PM2.5", "pm10": "PM10", "pm1": "PM1",
          "co2": "CO₂", "voc_index": "VOC index", "nox_index": "NOx index"}
SUFFIXES = ("_max", "_min", "_avg")


def noun(name: str) -> str:
    """"uv_max" -> "UV index", "rain" -> "rain"."""
    base = name[:-4] if name.endswith(SUFFIXES) else name
    return PRETTY.get(base, base.replace("_", " "))


def parameters(fields: list[str], what: str, extra: dict | None = None) -> dict:
    """The tool's JSON schema for these fields (`what` names the unit of a day's figure for the descriptions)."""
    return {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "First day, 'YYYY-MM-DD'. Use the start of the 'on record' range for all time."},
            "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (today's readings aren't final, so up to yesterday)."},
            "where": {"type": "array", "description": f"Conditions that must ALL hold on the same day, e.g. {what}.",
                      "items": {"type": "object", "properties": {
                          "field": {"type": "string", "enum": fields},
                          "op": {"type": "string", "enum": list(OPS)},
                          "value": {"type": "number"}}, "required": ["field", "op", "value"]}},
            "sort_by": {"type": "string", "enum": fields, "description": "Rank matching days by this. Default: the first condition's field."},
            "order": {"type": "string", "enum": ["desc", "asc"], "description": "desc = highest first (default), asc = lowest first."},
            "count_only": {"type": "boolean", "description": "true for \"how many days\": returns just the counts, no list of days (much smaller). Use it whenever the days themselves aren't asked for."},
            "group_by": {"type": "string", "enum": ["month", "year"], "description": "Break the result down per month or year, months with none included. It is drawn as a bar chart."},
            "stat": {"type": "string", "enum": list(STATS), "description": "What each month or year (or the whole period, with no group_by) shows: count (default: matching days), or the max, min, avg or sum of the field `of` over the matching days. E.g. rain total per month: stat sum, of rain, group_by month."},
            "of": {"type": "string", "enum": fields, "description": "The field to take max, min, avg or sum of (not needed for count)."},
            "limit": {"type": "integer", "description": f"How many days to list (default 5, at most {MAX_LIMIT}). The total is always counted."},
            **(extra or {}),
        },
        "required": ["start_date", "end_date"],
    }


def validate(args: dict, fields: dict[str, str]) -> str | None:
    """An error message for arguments that can't be used, else None. `fields` maps each field name to its unit."""
    where = args.get("where") or []
    bad = [c for c in where if c.get("field") not in fields or c.get("op") not in OPS or not isinstance(c.get("value"), (int, float))]
    if bad:
        return f"unusable condition(s) {bad}; fields are {', '.join(fields)}, ops {' '.join(OPS)}"
    if args.get("sort_by") and args["sort_by"] not in fields:
        return f"sort_by must be one of {', '.join(fields)}"
    if args.get("stat") not in (None, *STATS):
        return f"stat must be one of {', '.join(STATS)}"
    if args.get("stat") not in (None, "count") and args.get("of") not in fields:
        return f"stat {args['stat']} needs `of`: one of {', '.join(fields)}"
    if args.get("group_by") not in (None, "month", "year"):
        return "group_by must be month or year"
    return None


def conditions_text(where: list[dict], units: dict[str, str]) -> str:
    """"UV index ≥ 10", "rain ≥ 1 mm and lowest humidity < 40 %": the conditions as a chart's title says them."""
    parts = []
    for c in where:
        unit = units.get(c["field"], "")
        parts.append(f"{'lowest ' if c['field'].endswith('_min') else ''}{noun(c['field'])} {SYMBOL[c['op']]} {c['value']:g}" + (f" {unit}" if unit else ""))
    return " and ".join(parts)


def phrase(stat: str, of: str) -> str:
    """"Highest UV index", "Lowest temperature", "Average daily high", "Total rain": what a figure is, for a title."""
    kind = of[-3:] if of.endswith(SUFFIXES) else ""
    word = noun(of)
    if stat == "sum":
        return f"Total {word}"
    if stat == "avg":
        return f"Average daily {'high' if kind == 'max' else 'low'} {word}" if kind in ("max", "min") else f"Average {word}"
    if stat == "max":
        return f"Highest daily low {word}" if kind == "min" else f"Highest {word}"
    return f"Lowest daily high {word}" if kind == "max" else f"Lowest {word}"


def aggregate(stat: str, numbers: list[float]) -> float | None:
    if not numbers:
        return None
    return round({"max": max, "min": min, "sum": sum, "avg": lambda n: sum(n) / len(n)}[stat](numbers), 1)


def bars_chart(figures: dict[str, float | None], by: str, title: str, subtitle: str, label: str, unit: str, tz: tzinfo) -> Chart:
    """One bar per month or year (those with no figure left out), the number above each."""
    shown = {k: v for k, v in figures.items() if v is not None}
    starts = [datetime.strptime(k, "%Y-%m" if by == "month" else "%Y").date() for k in shown]
    bars = Bars(label, unit, [day_bounds(d, tz)[0] for d in starts], [float(v) for v in shown.values()],
                28 * 86400 if by == "month" else 365 * 86400, by, values=True)
    return Chart(title, subtitle, [Panel(label, "" if unit == "days" else unit, bars=bars)])


@dataclass
class Analysis:
    out: dict
    matches: list
    ranked: list
    rank: Callable
    holds: Callable
    row: Callable
    rows: list = field(default_factory=list)


def analyse(values: dict[str, dict[date, float]], shown: list[str], units: dict[str, str], args: dict, checked: list[date],
            first: date, last: date, tz: tzinfo, turn=None, row_extra: Callable[[date], dict] | None = None,
            scope: str = "") -> Analysis:
    """Find, rank, count and group the checked days. `values` has {day: figure} for every field in `shown`."""
    where = args.get("where") or []
    sort_by = args.get("sort_by") or (where[0]["field"] if where else shown[0])
    limit = max(1, min(int(args.get("limit") or 5), MAX_LIMIT))
    label = lambda d: f"{d:%a} {d.day} {d:%b %Y}"
    holds = lambda d, conds: all(d in values[c["field"]] and OPS[c["op"]](values[c["field"]][d], c["value"]) for c in conds)
    rank = lambda ds: sorted((d for d in ds if d in values[sort_by]), key=lambda d: values[sort_by][d],
                             reverse=args.get("order", "desc") != "asc")
    row = lambda d: {"date": label(d), **(row_extra(d) if row_extra else {}),
                     **{n: round(values[n][d], 1) for n in shown if d in values[n]}}
    matches = [d for d in checked if holds(d, where)]
    ranked = rank(matches)
    out = {"period": f"{label(first)} - {label(last)}", "days_checked": len(checked), "matching_days": len(matches),
           "units": {n: units[n] for n in shown}}
    stat, of, by = args.get("stat") or "count", args.get("of"), args.get("group_by")
    key = (lambda d: f"{d:%Y-%m}") if by == "month" else (lambda d: f"{d:%Y}")
    if stat != "count":
        out["stat"] = {"what": phrase(stat, of), "of": of, "unit": units[of]}
        figure = lambda ds: aggregate(stat, [values[of][d] for d in ds if d in values[of]])
        if by:
            buckets = dict.fromkeys(sorted({key(d) for d in checked}))
            for k in buckets:
                buckets[k] = figure([d for d in matches if key(d) == k])
        else:
            out["value"] = figure(matches)
    elif by:
        buckets = dict.fromkeys(sorted({key(d) for d in checked}), 0)
        for d in matches:
            buckets[key(d)] += 1
    if by and buckets:
        out[f"by_{by}"] = buckets
        if turn is not None:   # a count or figure per month or year is a bar chart, not a list
            on = f" on days with {conditions_text(where, units)}" if where and stat != "count" else ""
            if stat == "count":
                title, name, unit = (f"Days with {conditions_text(where, units)}" if where else "Days checked"), "Days", "days"
                detail = f"{len(matches):,} of {len(checked):,} days"
            else:
                title, name, unit = phrase(stat, of) + on, noun(of).capitalize(), units[of]
                detail = f"{len(matches):,} days counted"
            detail += f"  ·  {scope}" if scope else ""
            turn.charts.append(bars_chart(buckets, by, title, f"{label(first)} – {label(last)}  ·  {detail}  ·  per {by}",
                                          name, unit, tz))
            out["chart"] = COUNT_CHART_HINT
    rows = [] if args.get("count_only") or stat != "count" and by else [row(d) for d in ranked[:limit]]
    if not args.get("count_only") and not (stat != "count" and by):
        out["days"] = rows
    return Analysis(out, matches, ranked, rank, holds, row, rows)
