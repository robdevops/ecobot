"""Counting, ranking and summing days, whichever device the readings came from (weather_days and air_days share this).

A caller works out one value per day for each reading it offers (a day's highest, lowest or mean) and hands them over; this
finds the days that meet the conditions, ranks them, counts them, and can total, average or take the highest or lowest of a
reading per month or year. A count or figure per month or year is drawn as a bar chart (a number above each bar), not listed.
"""

import logging
import operator
import re
from dataclasses import dataclass
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from statistics import fmean

from .captions import COUNT_CHART_HINT
from .series import AIR
from .specs import Chart, Panel, calendar_bars
from .timeutil import day_text

log = logging.getLogger(__name__)

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq}
SYMBOL = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "="}
STATS = ("count", "max", "min", "avg", "sum")
MAX_LIMIT = 20
PRETTY = {"temp": "temperature", "uv": "UV index", "dew_point": "dew point", "feels_like": "feels-like", "vpd": "vapour pressure deficit",
          "wind_gust": "wind gust", "wind_speed": "wind speed", "rain_rate": "rain rate", "indoor_temp": "indoor temperature",
          "indoor_humidity": "indoor humidity", "solar": "solar radiation", **{name: m.label for name, m in AIR.items()}}
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
            "sort_by": {"type": "string", "description": "Rank matching days by this: a field from the where.field list. Default: the first condition's field."},
            "order": {"type": "string", "enum": ["desc", "asc"], "description": "desc = highest first (default), asc = lowest first."},
            "count_only": {"type": "boolean", "description": "true for \"how many days\": returns just the counts, no list of days (much smaller). Use it whenever the days themselves aren't asked for."},
            "group_by": {"type": "string", "enum": ["month", "year"], "description": "Break the result down per month or year, months with none included. It is drawn as a bar chart."},
            "stat": {"type": "string", "enum": list(STATS), "description": "What each month or year (or the whole period, with no group_by) shows: count (default: matching days), or the max, min, avg or sum of the field `of` over the matching days. E.g. rain total per month: stat sum, of rain, group_by month."},
            "of": {"type": "string", "description": "The field to take max, min, avg or sum of (a field from the where.field list; not needed for count)."},
            "limit": {"type": "integer", "description": f"How many days to list (default 5, at most {MAX_LIMIT}). The total is always counted."},
            **(extra or {}),
        },
        "required": ["start_date", "end_date"],
    }


GROUPING = re.compile(r"\b(?:per|by|each|every)\s+(year|month)\b|\b(yearly|annual\w*)\b|\b(monthly)\b", re.I)


def grouping_in(text: str) -> str | None:
    """"month" or "year" when the words ask for figures by or per month or year, else None."""
    found = GROUPING.search(text or "")
    if not found:
        return None
    return "month" if (found.group(1) or "").lower() == "month" or found.group(3) else "year"


def grouped_as_asked(args: dict, turn) -> dict:
    """The arguments with group_by filled in from the person's words when the model left it out ("... by year", "per month"):
    a count by year or month is a bar chart, never one overall number."""
    if args.get("group_by") or turn is None or not getattr(turn, "text", ""):
        return args
    by = grouping_in(turn.text)
    if not by:
        return args
    log.info("group_by %s added: the question asks for figures by %s", by, by)
    return {**args, "group_by": by}


def validate(args: dict, fields: dict[str, str]) -> str | None:
    """An error message for arguments that can't be used, else None. `fields` maps each field name to its unit."""
    where = args.get("where") or []
    bad = [c for c in where if c.get("field") not in fields or c.get("op") not in OPS or not isinstance(c.get("value"), int | float)]
    if bad:
        return f"unusable condition(s) {bad}; fields are {', '.join(fields)}, ops {' '.join(OPS)}"
    if args.get("sort_by") and args["sort_by"] not in fields:
        return f"sort_by must be one of {', '.join(fields)}"
    if args.get("stat") not in (None, *STATS):
        return f"stat must be one of {', '.join(STATS)}"
    if args.get("stat") not in (None, "count") and args.get("of") not in fields:
        return f"stat {args['stat']} needs `of`: one of {', '.join(fields)}"
    if args.get("of") and args["of"] not in fields:
        return f"of must be one of {', '.join(fields)}"
    if args.get("group_by") not in (None, "month", "year"):
        return "group_by must be month or year"
    return None


@dataclass(frozen=True)
class Asked:
    """A days question, checked: the period (up to yesterday), the arguments with sort_by filled in, and the fields a listed
    day shows, each with its unit."""
    first: date
    last: date
    args: dict
    units: dict[str, str]

    @property
    def days(self) -> list[date]:
        return [self.first + timedelta(days=k) for k in range((self.last - self.first).days + 1)]

    @property
    def shown(self) -> list[str]:
        return list(self.units)

    @property
    def where(self) -> list[dict]:
        return self.args.get("where") or []

    @property
    def sort_by(self) -> str:
        return self.args["sort_by"]


def prepare(args: dict, fields: dict[str, str], shown: tuple[str, ...], today: date) -> Asked | str:
    """The question checked (an error text when it can't be used) and filled in. `fields` maps each field the tool offers to
    its unit; `shown` are the ones every listed day shows, before those the arguments name. Days are ranked by sort_by, else
    the first condition's field, else `of`, else the first of `shown`."""
    try:
        first = date.fromisoformat(str(args["start_date"])[:10])
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1))
    except (KeyError, ValueError) as e:
        return f"bad date ({e}); use 'YYYY-MM-DD'"
    if first > last:
        return "start_date must be before end_date (today isn't final, so the latest day is yesterday)"
    if problem := validate(args, fields):
        return problem
    where = args.get("where") or []
    sort_by = args.get("sort_by") or (where[0]["field"] if where else args.get("of") or shown[0])
    named = dict.fromkeys([*shown, sort_by, *(c["field"] for c in where), *([args["of"]] if args.get("of") else [])])
    return Asked(first, last, {**args, "sort_by": sort_by}, {n: fields[n] for n in named})


def lists_days(args: dict) -> bool:
    """Does the answer list the days? Not when only the counts are asked for, nor a figure per month or year."""
    return not (args.get("count_only") or (args.get("stat") not in (None, "count") and args.get("group_by")))


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
    return round({"max": max, "min": min, "sum": sum, "avg": fmean}[stat](numbers), 1)


def bars_chart(figures: dict[str, float | None], by: str, title: str, subtitle: str, label: str, unit: str, tz: tzinfo,
               period: tuple[date, date]) -> Chart:
    """One bar per month or year (those with no figure left out), the number above each; `period` is the first and last day counted."""
    shown = {datetime.strptime(k, "%Y-%m" if by == "month" else "%Y").date(): v for k, v in figures.items() if v is not None}
    return Chart(title, subtitle, [Panel(label, "" if unit == "days" else unit, bars=calendar_bars(label, unit, shown, by, tz))],
                 period=period)


@dataclass
class Analysis:
    out: dict
    matches: list
    ranked: list
    rank: Callable
    holds: Callable
    row: Callable


def analyse(values: dict[str, dict[date, float]], asked: Asked, checked: list[date], tz: tzinfo, turn=None,
            row_extra: Callable[[date], dict] | None = None, scope: str = "") -> Analysis:
    """Find, rank, count and group the checked days. `values` has {day: figure} for every field the question shows."""
    args, where, shown, units, sort_by = asked.args, asked.where, asked.shown, asked.units, asked.sort_by
    first, last = asked.first, asked.last
    limit = max(1, min(int(args.get("limit") or 5), MAX_LIMIT))
    holds = lambda d, conds: all(d in values[c["field"]] and OPS[c["op"]](values[c["field"]][d], c["value"]) for c in conds)
    rank = lambda ds: sorted((d for d in ds if d in values[sort_by]), key=lambda d: values[sort_by][d],
                             reverse=args.get("order", "desc") != "asc")
    row = lambda d: {"date": day_text(d), **(row_extra(d) if row_extra else {}),
                     **{n: round(values[n][d], 1) for n in shown if d in values[n]}}
    matches = [d for d in checked if holds(d, where)]
    ranked = rank(matches)
    out = {"period": f"{day_text(first)} - {day_text(last)}", "days_checked": len(checked), "matching_days": len(matches),
           "units": dict(units)}
    if where:
        out["condition"] = conditions_text(where, units)
    stat, of, by = args.get("stat") or "count", args.get("of"), args.get("group_by")
    key = (lambda d: f"{d:%Y-%m}") if by == "month" else (lambda d: f"{d:%Y}")
    if stat != "count":
        out["stat"] = {"what": phrase(stat, of), "of": of, "unit": units[of]}
        figure = lambda ds: aggregate(stat, [values[of][d] for d in ds if d in values[of]])
        out["value"] = figure(matches)   # over the whole period
        if by:
            buckets = dict.fromkeys(sorted({key(d) for d in checked}))
            for k in buckets:
                buckets[k] = figure([d for d in matches if key(d) == k])
    elif by:
        buckets = dict.fromkeys(sorted({key(d) for d in checked}), 0)
        for d in matches:
            buckets[key(d)] += 1
    if by and buckets:
        out[f"by_{by}"] = buckets
        if turn is not None and any(v is not None for v in buckets.values()):   # a bar chart, not a list (unless no bucket has a figure)
            on = f" on days with {conditions_text(where, units)}" if where and stat != "count" else ""
            if stat == "count":
                title, name, unit = (f"Days with {conditions_text(where, units)}" if where else "Days checked"), "Days", "days"
                detail = f"{len(matches):,} of {len(checked):,} days"
            else:
                title, name, unit = phrase(stat, of) + on, noun(of).capitalize(), units[of]
                detail = f"{len(matches):,} days counted"
            detail += f"  ·  {scope}" if scope else ""
            turn.charts.append(bars_chart(buckets, by, title, f"{day_text(first)} – {day_text(last)}  ·  {detail}  ·  per {by}",
                                          name, unit, tz, (first, last)))
            out["chart"] = COUNT_CHART_HINT
    if lists_days(args):
        out["days"] = [row(d) for d in ranked[:limit]]
    return Analysis(out, matches, ranked, rank, holds, row)
