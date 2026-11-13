"""Charts as data (specs.to_json and from_json, schemas/chart.json): a chart written as JSON comes back the same and is drawn the
same, the schema holds every chart the bot draws, and a line for a model to read is bucketed to its budget with its true lowest and
highest kept. (Every chart any test builds also goes through JSON and back: conftest.charts_come_back_from_json.)

jsonschema isn't one of the requirements, so the schema is checked here by a small checker of the keywords it uses (`errors`);
a test makes sure it uses no others."""

import copy
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from lib import charts
from lib.specs import CHART_SOURCE, Bars, Chart, Compass, Line, Panel, Shares, from_json, stack, to_json
from tests import chart_samples, imagecmp
from tests.fakes import TZ

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "chart.json").read_text())
SAMPLES = [name for name, make in chart_samples.SAMPLES.items() if isinstance(make(), Chart)]   # (not the gauges)
T0 = int(datetime(2026, 9, 1, tzinfo=TZ).timestamp())


def sent(chart: Chart, **kw) -> dict:
    """The chart's JSON as a client gets it: written out as text and read back."""
    return json.loads(json.dumps(to_json(chart, TZ, **kw), allow_nan=False))


# ---------- a small JSON Schema checker: the keywords schemas/chart.json uses, as draft 2020-12 reads them ----------
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
CHECKED = {"$ref", "type", "const", "enum", "properties", "required", "additionalProperties", "items", "prefixItems", "minItems",
           "maxItems", "oneOf", "pattern", "minimum"}
NOTES = {"$schema", "$id", "$defs", "title", "description", "format"}   # annotations: nothing to check


def is_type(value, name: str) -> bool:
    if name in ("number", "integer"):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and (name == "number" or float(value).is_integer())
    return isinstance(value, TYPES[name])


def resolve(ref: str) -> dict:
    node = SCHEMA
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def errors(value, schema: dict | bool = SCHEMA, at: str = "$") -> list[str]:
    """What is wrong with `value` against `schema`: [] when it conforms."""
    if schema is False:
        return [f"{at}: not allowed"]
    if schema is True:
        return []
    out = errors(value, resolve(schema["$ref"]), at) if "$ref" in schema else []
    if "type" in schema and not any(is_type(value, t) for t in ([schema["type"]] if isinstance(schema["type"], str) else schema["type"])):
        return out + [f"{at}: not of type {schema['type']}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{at}: not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{at}: not one of {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        out.append(f"{at}: {value!r} doesn't match {schema['pattern']}")
    if "minimum" in schema and is_type(value, "number") and value < schema["minimum"]:
        out.append(f"{at}: under {schema['minimum']}")
    if "oneOf" in schema and sum(not errors(value, s, at) for s in schema["oneOf"]) != 1:
        out.append(f"{at}: not exactly one of its kinds")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        out += [f"{at}: no {key!r}" for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in props:
                out += errors(item, props[key], f"{at}.{key}")
            elif "additionalProperties" in schema:
                out += errors(item, schema["additionalProperties"], f"{at}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            out.append(f"{at}: {len(value)} items")
        first = schema.get("prefixItems", [])
        for i, item in enumerate(value):
            out += errors(item, first[i] if i < len(first) else schema.get("items", True), f"{at}[{i}]")
    return out


def keywords(schema, under_names: bool = False) -> set[str]:
    """Every keyword the schema uses (the names under properties and $defs are names, not keywords)."""
    if isinstance(schema, list):
        return set().union(*(keywords(s) for s in schema))
    if not isinstance(schema, dict):
        return set()
    if under_names:
        return set().union(*(keywords(s) for s in schema.values()))
    return set(schema) | set().union(*(keywords(v, k in ("properties", "$defs")) for k, v in schema.items()))


def test_the_schema_uses_only_keywords_the_checker_reads_and_is_versioned():
    assert keywords(SCHEMA) <= CHECKED | NOTES, keywords(SCHEMA) - CHECKED - NOTES
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema" and SCHEMA["properties"]["version"]["const"] == 1


# ---------- every sample chart, through JSON and back ----------
@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_chart_comes_back_the_same_and_is_drawn_the_same(name):
    chart, dark = chart_samples.SAMPLES[name](), name.endswith("_dark")
    data = sent(chart)
    back = from_json(data)
    assert back == chart                     # field by field: the tuples (period, zones, records, speed steps) come back tuples
    assert to_json(back, TZ) == data
    assert charts.render_data(data, TZ, dark=dark) == charts.render(chart, TZ, dark=dark)   # the same PNG, byte for byte
    assert not errors(data), errors(data)


@pytest.mark.golden
@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_chart_drawn_from_its_json_matches_its_reference_image(name):
    assert imagecmp.check(name, from_json(sent(chart_samples.SAMPLES[name]()))) is None


def every_field() -> list[Chart]:
    """Charts with what the samples leave out: a rating, bars with their numbers, a line on a flipped right-hand axis, a smoothed
    indoor line marked on itself, zones, a rose with no speeds, and a line whose only record is a low its reading never shows."""
    xs = [T0 + i * 3600 for i in range(8)]
    ys = [3.0, 5.5, 4.0, 9.25, 7.0, 6.0, 2.0, 4.5]
    rating = Panel("PM2.5 rating", "%", shares=Shares("PM2.5", xs, 3600, [80.0] * 8, [15.0] * 8, [5.0] * 8, "hour"))
    months = Panel("Days", "", bars=Bars("Days", "days", xs[:3], [1, 0, 4], 28 * 86400, "month", values=True))
    twin = Panel("UV and Solar", "", [Line("UV", xs, ys, reading="uv"),
                                      Line("Solar", xs, [y * 100 for y in ys], [y * 90 for y in ys], [y * 110 for y in ys],
                                           smoothed=True, reading="solar", indoor=True, right=True, on_line=True)],
                 flip_right=True, peaks="none")
    rated = Panel("PM2.5", "µg/m³", [Line("PM2.5", xs, ys, reading="pm2_5")], zones=(12.0, 35.5), reading="pm2_5", peaks="aside")
    calm = Panel("Wind", "km/h", [Line("Wind", xs, ys, records={"low": (xs[6], 2.0)})], reading="wind")
    return [stack([rating, months], date(2026, 9, 1), date(2026, 9, 1)), Chart("Two scales", "s", [twin]),
            Chart("Rated", "s", [rated], source="Somewhere"), Chart("Wind", "s", [calm], Compass([[1, 2, 3]] * 16, False, (5, 15)))]


@pytest.mark.parametrize("i", range(len(every_field())))
def test_every_field_the_renderer_reads_comes_back(i):
    chart = every_field()[i]
    data = sent(chart)
    assert from_json(data) == chart and charts.render_data(data, TZ) == charts.render(chart, TZ)
    assert not errors(data), errors(data)


def test_a_line_drawn_without_records_comes_back_without_them():
    chart = every_field()[3]
    assert chart.panels[0].lines[0].records == {}   # wind's low isn't labelled, so its only record went
    assert from_json(sent(chart)).panels[0].lines[0].records == {}   # not its own high and low, as a chart would give it


def test_the_source_is_the_charts_own_not_the_tool_call_it_is_read_in():
    data = sent(every_field()[1])
    token = CHART_SOURCE.set("Ecowitt weather station")
    try:
        assert data["source"] == "" and from_json(data).source == ""
    finally:
        CHART_SOURCE.reset(token)


def test_times_are_local_with_their_offset_and_give_the_same_epochs_back_across_both_clock_changes():
    forward = int(datetime(2026, 10, 4, 1, tzinfo=TZ).timestamp())   # Melbourne: 2am is 3am
    back = int(datetime(2026, 4, 5, 1, 30, tzinfo=TZ).timestamp())   # 3am is 2am again: 2 to 3am happens twice
    for start, want in ((forward, ["01:00:00+10:00", "01:30:00+10:00", "03:00:00+11:00", "03:30:00+11:00"]),
                        (back, ["01:30:00+11:00", "02:00:00+11:00", "02:30:00+11:00", "02:00:00+10:00", "02:30:00+10:00"])):
        xs = [start + i * 1800 for i in range(len(want))]
        chart = Chart("T", "s", [Panel("Temperature", "°C", [Line("Outdoor", xs, [float(i) for i in range(len(xs))])])])
        data = sent(chart)
        assert [p[0][11:] for p in data["panels"][0]["series"][0]["points"]] == want
        assert from_json(data).panels[0].lines[0].x == xs
        assert data["newest"] == data["panels"][0]["series"][0]["points"][-1][0]


def test_a_chart_says_its_period_source_and_the_kind_of_each_series():
    data = sent(chart_samples.humidity_rain())
    assert data["version"] == 1 and data["period"] == ["2026-09-28", "2026-10-04"] and data["source"] == ""
    assert [(s["name"], s["kind"]) for s in data["panels"][0]["series"]] == [("Outdoor", "line"), ("Indoor", "line"), ("Rain", "bars")]
    wind = sent(chart_samples.wind())["panels"][0]
    assert wind["series"][0]["kind"] == "band" and len(wind["series"][0]["points"][0]) == 4 and len(wind["compass"]["rose"]) == 16
    assert sent(chart_samples.quarter())["period"] is None


# ---------- what isn't a chart ----------
def broken(change) -> dict:
    data = copy.deepcopy(sent(chart_samples.humidity_rain()))
    change(data)
    return data


def series(data: dict, i: int = 0) -> dict:
    return data["panels"][0]["series"][i]


BROKEN = {   # name: (the change, whether the schema sees it: the rest only building the chart can tell)
    "another version": (lambda d: d.update(version=2), True),
    "no title": (lambda d: d.pop("title"), True),
    "a time without its offset": (lambda d: series(d)["points"][0].__setitem__(0, "2026-09-28T00:00:00"), True),
    "a kind of series there isn't": (lambda d: series(d).update(kind="pie"), True),
    "a band's point without its range": (lambda d: series(d).update(kind="band"), True),
    "a value that isn't a number": (lambda d: series(d)["points"][3].__setitem__(1, "62"), True),
    "a line of one point": (lambda d: series(d).update(points=series(d)["points"][:1]), True),
    "a period of three days": (lambda d: d["period"].append("2026-10-05"), True),
    "a period of none": (lambda d: d.update(period=[]), True),
    "two series of bars in a panel": (lambda d: d["panels"][0]["series"].append(series(d, 2)), False),
    "a rose beside a stack": (lambda d: d["panels"].append({**d["panels"][0], "compass": sent(chart_samples.wind())["panels"][0]["compass"]}),
                              False),
}


@pytest.mark.parametrize("name", BROKEN)
def test_what_is_not_a_chart_is_refused_when_it_is_read_and_by_the_schema_where_it_can_tell(name):
    change, schema_sees = BROKEN[name]
    data = broken(change)
    with pytest.raises(ValueError):
        from_json(data)
    with pytest.raises(ValueError):
        charts.render_data(data, TZ)
    assert bool(errors(data)) == schema_sees, errors(data)


def test_the_checker_finds_what_it_should():
    assert not errors(sent(chart_samples.wind()))
    assert errors({"version": 1}) and errors(broken(lambda d: d["panels"][0].update(extra=1)))


# ---------- a line for a model to read ----------
def test_a_long_line_is_bucketed_to_the_budget_keeping_its_true_lowest_and_highest():
    xs = [T0 + i * 300 for i in range(2016)]                                       # a week of 5-minute readings
    ys = [12 + 6 * math.sin(i / 40) for i in range(len(xs))]
    ys[300], ys[1500] = 41.5, -3.25                                                # one reading far above, one far below
    xs, ys = xs[:600] + xs[888:], ys[:600] + ys[888:]                              # and a day missing
    rain = Bars("Rain", "mm", [T0 + i * 3600 for i in range(168)], [0.2] * 168, 3600, "hour")
    chart = Chart("T", "s", [Panel("Temperature", "°C", [Line("Outdoor", xs, ys, reading="temperature")], bars=rain)])
    whole = sent(chart)
    data = sent(chart, max_points=150)
    line, bars = series(data), series(data, 1)
    assert line["kind"] == "band" and 100 < len(line["points"]) <= 150
    times, means, lows, highs = zip(*line["points"])
    assert min(lows) == -3.25 and max(highs) == 41.5 and all(lo <= y <= hi for _, y, lo, hi in line["points"])
    assert list(times) == sorted(set(times)) and line["records"] == series(whole)["records"]   # the true records, with when
    assert bars == series(whole, 1)                                                # bars aren't lines: as they are
    assert not errors(data) and len(from_json(data).panels[0].lines[0].x) == len(line["points"])
    assert to_json(chart, TZ) == to_json(chart, TZ, max_points=None) and from_json(whole) == chart


def test_a_band_keeps_its_own_extremes_and_a_short_line_is_left_as_it_is():
    wind = chart_samples.wind()
    line = series(sent(wind, max_points=40))
    assert len(line["points"]) <= 40 and max(p[3] for p in line["points"]) == max(wind.panels[0].lines[0].high)
    assert min(p[2] for p in line["points"]) == min(wind.panels[0].lines[0].low)
    quarter = chart_samples.quarter()                                              # 90 days: within a budget of 150
    assert sent(quarter, max_points=150) == sent(quarter)
    with pytest.raises(ValueError):
        to_json(quarter, TZ, max_points=1)
