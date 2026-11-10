"""The `days` tool (lib/service/days.py): its arguments and defaults, its result, its refusals, its periods, its charts and their
`drawn_by`, the merged rule over both sensors, part of each day (`hours`), and its answer line, against today's weather_days and air_days
(which it replaces) and lib/report.py's days lookup (whose words it keeps), on the fake station's archive with an air sensor added."""

import copy
import json
import shutil
from datetime import date, datetime, time, timedelta

import pytest

from tests import report_words as report   # (the bot's words before step 2: the oracle)
from lib.airgradient.days import find_air_days
from lib.airgradient.readings import AirReadings
from lib.ecowitt.days import find_days
from lib.partday import over, parse_stretch
from lib.service import days as days_tool
from lib.service.common import CHART_NOTE, FRESH
from lib.service.context import Context
from lib.service.daylook import LOOKS
from lib.storage import Storage
from lib.storage.catalogue import CLOUD
from lib.timeutil import day_bounds, day_text, local_epoch, now_local
from lib.tools import Turn
from tests.fakes import TZ, archived_station, rain_day, temp
from tests.test_chart_json import errors as schema_errors

LOC = "42"
AIR_DAYS = 40            # the air sensor's 5-minute readings: the last 40 days
OLD_AIR = (390, 375)     # ... and hourly averages of days this long ago, before the station's 30-minute archive (its daily records)


def pm(ts: int) -> float:
    """PM2.5: a level for each day (2 to 32 µg/m³), a microgram higher from noon."""
    local = datetime.fromtimestamp(ts, TZ)
    return 2.0 + local.toordinal() % 7 * 5 + (local.hour >= 12)


def air_row(ts: int) -> dict:
    v = pm(ts)
    return {"ts": ts, "pm2_5": v, "pm10": v * 1.5, "pm1": v * 0.7, "co2": 420.0, "voc_index": 100.0, "nox_index": 1.0,
            "air_temperature": 18.0, "air_humidity": 60.0}


@pytest.fixture(scope="module")
def air_cache(archived_cache, tmp_path_factory):
    """The archive with an air sensor's readings added (built once for these tests): the last AIR_DAYS days at 5 minutes, and OLD_AIR's
    days as hourly averages."""
    path = tmp_path_factory.mktemp("days") / "ecobot.sqlite"
    shutil.copy(archived_cache, path)
    storage = Storage(path, TZ)
    today = datetime.now(TZ).date()

    def write(w):
        sensor = w.air_sensor(LOC)
        start, end = day_bounds(today - timedelta(days=AIR_DAYS), TZ)[0], int(datetime.now(TZ).timestamp()) // 300 * 300
        w.put(sensor, "5min", [air_row(t) for t in range(start, end, 300)], CLOUD)
        w.cover(sensor, "5min", start, end - 1)
        start, end = day_bounds(today - timedelta(days=OLD_AIR[0]), TZ)[0], day_bounds(today - timedelta(days=OLD_AIR[1]), TZ)[1]
        w.put(sensor, "1hour", [air_row(t) for t in range(start, end, 3600)], CLOUD)
        w.cover(sensor, "1hour", start, end - 1)
    storage.writer.run(write, ())
    storage.close()
    return path


class Air:
    """The part of the air source the tool uses: its readings in the file."""
    tz = TZ

    def __init__(self, storage):
        self.store = AirReadings(storage, LOC, TZ)


@pytest.fixture
async def world(tmp_path, air_cache):
    eco, fake = await archived_station(tmp_path, air_cache)
    yield Context(eco=eco, air=Air(eco.storage))
    await eco.close()
    assert fake.calls == []   # answered from the file alone


def tool(ctx):
    (found,) = days_tool.tools(ctx)
    return found


async def ask(ctx, meta=None, **args) -> dict:
    return json.loads(await tool(ctx).call(args, meta))


def ago(n: int) -> date:
    return datetime.now(TZ).date() - timedelta(days=n)


def label(d: date) -> str:
    return day_text(d)


def truth(d: date) -> tuple[float, float]:
    """(temp_max, rain) of a local day, from the fake's functions alone."""
    ticks = range(day_bounds(d, TZ)[0], day_bounds(d, TZ)[1], 300)
    return max(temp(t) for t in ticks), max(rain_day(t) for t in ticks)


def engine_part(out: dict) -> dict:
    """A days result as weather_days and air_days had it: without the service's head, its answer line, charts, looks and sources."""
    out = {k: v for k, v in out.items() if k not in ("period", "as_of", "fresh", "answer", "chart_note", "charts", "reply", "complete",
                                                    "note", "chart")}
    for key in ("days", "trace_rain_days"):
        if key in out:
            out[key] = [{k: v for k, v in row.items() if k not in ("day_look", "source")} for row in out[key]]
    return out


# ---------- the tool ----------
async def test_the_tool_offers_the_fields_of_the_sensors_there_are_and_says_what_it_is_for(world):
    t = tool(world)
    assert t.name == "days" and t.annotations == {"readOnlyHint": True, "openWorldHint": False}
    props = t.parameters["properties"]
    assert set(props) == set(days_tool.ARGUMENTS)
    fields = props["where"]["items"]["properties"]["field"]["enum"]
    assert {"temp_max", "rain", "indoor_humidity_min", "uv_max", "pm2_5_max", "co2_avg"} <= set(fields)
    assert props["sort_by"]["enum"] == props["of"]["enum"] == fields
    assert "on record" in props["period"]["description"] and "last 30 days" in props["period"]["description"]
    assert "Whole days only" in props["period"]["description"] and "night (22:00-06:00" in props["hours"]["description"]
    d = t.description
    assert len(d) < 1500 and all(word in d for word in ("summary", "readings", "relate", "day_look", "hours", "only", "count_only"))
    # the live evals: "days with most rain" sent no sort_by (ranked by temp_max), "average temperature per year" no group_by
    assert 'sort_by is the field the days are ranked by: the wettest days, most rain {sort_by: "rain"}' in d
    assert '"per year", "each year", "by month"), group_by' in d and '{stat: "avg", of: "temp_avg", group_by: "year"}' in d
    assert '"most rain"' in props["sort_by"]["description"] and '"each year"' in props["group_by"]["description"]
    station_only = days_tool.tools(Context(eco=world.eco))[0].parameters["properties"]
    assert "pm2_5_max" not in station_only["where"]["items"]["properties"]["field"]["enum"]
    assert "Air sensor" not in station_only["where"]["description"]
    air_only = days_tool.tools(Context(air=world.air))[0].parameters["properties"]
    assert air_only["where"]["items"]["properties"]["field"]["enum"] == list(days_tool.AIR_UNITS)
    assert days_tool.tools(Context()) == []


async def test_with_nothing_sent_it_ranks_every_day_on_record_by_the_high_and_answers_ready_to_pass_on(world):
    out = await ask(world)
    assert list(out)[:4] == ["period", "as_of", "fresh", "answer"] and out["fresh"] == FRESH
    first = await world.storage.read(lambda r: min(date.fromisoformat(d) for (d,) in r.db.execute("SELECT day FROM climate_day")))
    assert out["period"]["asked"] == "on record" and out["period"]["start"].startswith(first.isoformat())
    assert out["period"]["cut"] == f"to {day_text(ago(1), year=False)}: today isn't over"
    assert out["days_checked"] == out["matching_days"] == (ago(1) - first).days + 1   # (no condition: every day held matches)
    rows = out["days"]
    assert len(rows) == 5 and [r["temp_max"] for r in rows] == sorted((r["temp_max"] for r in rows), reverse=True)
    assert set(rows[0]) >= {"date", "day_look", "source", "temp_max", "temp_min", "rain"}
    assert all(r["day_look"]["emoji"] == LOOKS[r["day_look"]["word"]] for r in rows)
    assert out["answer"].startswith(f"{out['matching_days']:,} of {out['days_checked']:,} days (")
    assert "charts" not in out and "complete" not in out and "note_daily" in out   # (its oldest days are Ecowitt's daily records)


# ---------- the same answers as weather_days and air_days ----------
STATION_CASES = [
    {"where": [{"field": "rain", "op": ">", "value": 0}], "sort_by": "temp_max", "limit": 3},
    {"where": [{"field": "rain", "op": ">=", "value": 1}], "sort_by": "temp_max", "limit": 4},       # a trace day ranks higher
    {"where": [{"field": "temp_max", "op": ">", "value": 18}], "count_only": True},
    {"sort_by": "temp_min", "order": "asc", "limit": 2},
    {"stat": "sum", "of": "rain", "group_by": "month"},
    {"where": [{"field": "humidity_min", "op": "<", "value": 60}], "stat": "avg", "of": "humidity_avg"},   # note_averaged
    {"where": [{"field": "rain", "op": "=", "value": 0}], "only": "weekend", "limit": 20},
]


@pytest.mark.parametrize("case", STATION_CASES)
async def test_station_fields_answer_as_weather_days_did(world, case):
    first, last = ago(80), ago(2)
    out = await ask(world, start=str(first), end=str(last), **case)
    theirs = await find_days(world.storage, world.eco.mac, TZ, {"start_date": str(first), "end_date": str(last), **case}, now_local(TZ),
                             Turn())
    assert engine_part(out) == engine_part(theirs)
    assert all(r["source"] == "exact" for r in out.get("days", []))
    assert out["answer"] == report.lookup("days", json.dumps(theirs), [], [], ago(0))   # the bot's words for the same data


async def test_the_older_days_say_they_come_from_daily_records_as_weather_days_did(world):
    args = {"where": [{"field": "temp_max", "op": ">", "value": 0}], "limit": 20}
    out = await ask(world, start=str(ago(540)), end=str(ago(2)), **args)
    theirs = await find_days(world.storage, world.eco.mac, TZ, {"start_date": str(ago(540)), "end_date": str(ago(2)), **args},
                             now_local(TZ), Turn())
    assert engine_part(out) == engine_part(theirs) and out["exact_from"] and "10am to 10am" in out["note_daily"]
    old = await ask(world, start=str(ago(540)), end=str(ago(510)), **args)
    assert old["days"] and all(r["source"] == "daily" for r in old["days"])


AIR_CASES = [
    {"where": [{"field": "pm2_5_max", "op": ">", "value": 20}], "limit": 3},
    {"stat": "max", "of": "pm2_5_max", "group_by": "month"},
    {"sort_by": "pm10_avg", "order": "asc", "count_only": True},
]


@pytest.mark.parametrize("case", AIR_CASES)
async def test_air_fields_answer_as_air_days_did(world, case):
    first, last = ago(AIR_DAYS + 10), ago(2)
    out = await ask(world, start=str(first), end=str(last), **case)
    theirs = await find_air_days(world.air.store, TZ, {"start_date": str(first), "end_date": str(last), **case}, now_local(TZ), Turn())
    assert engine_part(out) == engine_part(theirs) and out["note_missing"] == theirs["note_missing"]   # (the 10 days before the sensor)
    assert out["answer"] == report.lookup("days", json.dumps(theirs), [], [], ago(0))
    for row in out.get("days", []):
        assert row["source"] == "exact" and "day_look" in row   # each day's weather, from the station


async def test_an_air_question_shows_and_ranks_by_pm2_5_and_its_days_carry_the_weather(world):
    out = await ask(world, period="last 20 days", where=[{"field": "co2_avg", "op": ">", "value": 0}])
    assert out["units"] == {"pm2_5_max": "µg/m³", "co2_avg": "ppm"} and out["days_checked"] == 20   # (ranked by co2_avg, as named)
    assert "temp_max" not in out["days"][0] and "day_look" in out["days"][0]
    air_only = json.loads(await days_tool.tools(Context(air=world.air))[0].call({"period": "last 20 days"}))
    assert [r["pm2_5_max"] for r in air_only["days"]] == sorted((r["pm2_5_max"] for r in air_only["days"]), reverse=True)
    expected = max(pm(t) for d in (ago(n) for n in range(1, 21)) for t in range(*day_bounds(d, TZ), 300))
    assert air_only["days"][0]["pm2_5_max"] == expected and air_only["units"] == {"pm2_5_max": "µg/m³"}
    assert "day_look" not in air_only["days"][0]   # (no station: no weather to look at)


# ---------- the answer line ----------
def test_the_answer_line_is_the_bots_days_lookup_word_for_word():
    data = {"period": "Tue 29 Sep 2026 - Tue 6 Oct 2026", "days_checked": 1454, "matching_days": 137, "condition": "UV index ≥ 10"}
    for out in (data, {**data, "note_averaged": "..."}, {**data, "stat": {"what": "Total rain", "of": "rain", "unit": "mm"}, "value": 49.8},
                {**data, "stat": {"what": "Highest UV index", "of": "uv_max", "unit": ""}, "value": 11.0},
                {**data, "stat": {"what": "Total rain", "of": "rain", "unit": "mm"}, "value": None}, {**data, "condition": None}):
        mine = days_tool.answer_line({k: v for k, v in out.items() if k != "period"}, out["period"])
        assert mine == report.lookup("days", json.dumps(out), [], [], date(2026, 11, 10))
    assert days_tool.answer_line(data, data["period"]) == "137 of 1,454 days with UV index ≥ 10 (Tue 29 Sep – Tue 6 Oct 2026)."
    night, three = parse_stretch("night"), parse_stretch("15:00")
    assert days_tool.answer_line(data, data["period"], night).startswith("Each night (22:00–06:00): 137 of 1,454 days")
    assert days_tool.answer_line(data, data["period"], three).startswith("Each day at 15:00: 137 of")
    assert days_tool.answer_line(data, data["period"], parse_stretch("15:00-16:00")).startswith("Each day 15:00–16:00: 137 of")


# ---------- refusals ----------
async def test_a_call_it_cant_use_is_refused_with_what_to_send_instead(world):
    bad = await ask(world, where=[{"field": "temperature", "op": ">", "value": 30}])
    assert "Did you mean temp_max, temp_min, temp_avg?" in bad["error"] and "pm2_5_max" in bad["fields"]
    assert "pm2_5_max, pm2_5_min, pm2_5_avg" in (await ask(world, sort_by="pm2.5"))["error"]
    assert (await ask(world, where=[{"field": "rain", "op": "gt", "value": 1}]))["ops"] == [">", ">=", "<", "<=", "="]
    assert "a number" in (await ask(world, where=[{"field": "rain", "op": ">", "value": "lots"}]))["error"]
    assert "rain" in (await ask(world, where=["rain > 1"]))["error"]
    assert "needs `of`" in (await ask(world, stat="sum"))["error"]
    assert "month or year" in (await ask(world, group_by="week"))["error"]
    assert "desc" in (await ask(world, order="up"))["error"]
    assert "public_holiday or weekend" in (await ask(world, only="holiday"))["error"]
    old = await ask(world, start_date="2026-01-01", end_date="2026-01-31")
    assert "`start_date` is `start`" in old["error"] and "period" in old["arguments"]
    hours = await ask(world, hours="dawn")
    assert "isn't a part of the day" in hours["error"] or "not a part of the day" in hours["error"]
    assert "night (22:00-06:00)" in hours["hours"]
    for words in ("today", "last 5 hours", "this morning"):   # whole days only: today isn't over
        assert "use summary or readings" in (await ask(world, period=words))["error"], words
    assert "use forecast" in (await ask(world, period="next week"))["error"]
    forms = await ask(world, period="the other week")
    assert "isn't a period" in forms["error"] and "on record, all time, ever (from the first reading held)" in forms["forms"]
    assert "today" not in forms["forms"]   # (a whole-day tool lists only the forms it reads)
    long = await ask(world, hours="night", period="on record")
    assert "at most 400 days" in long["error"] and long["fits"] == "past year"
    no_air = days_tool.tools(Context(eco=world.eco))[0]
    refused = json.loads(await no_air.call({"where": [{"field": "pm2_5_max", "op": ">", "value": 1}]}))
    assert "this service has no air sensor" in refused["error"]


async def test_a_period_before_the_record_says_where_the_record_starts(world):
    first = await world.storage.read(lambda r: min(date.fromisoformat(d) for (d,) in r.db.execute("SELECT day FROM air_day")))
    out = await ask(world, start=str(first - timedelta(days=30)), end=str(first - timedelta(days=1)), sort_by="pm2_5_max")
    assert out["days_checked"] == 0 and out["note"] == f"No readings are held for this period: the record of what was asked about " \
                                                       f"starts {day_text(first)}."
    assert out["answer"].startswith("0 of 0 days (") and "days" in out and out["days"] == []


async def test_the_holiday_and_weekend_filter_is_the_models_own(world):
    out = await ask(world, period="last 8 weeks", only="weekend", limit=20)
    days = [datetime.strptime(r["date"], "%a %d %b %Y").date() for r in out["days"]]
    assert out["days_checked"] == 16 and all(d.weekday() >= 5 for d in days)
    holidays = await ask(world, period="past year", only="public_holiday", sort_by="rain")
    assert 5 <= holidays["days_checked"] <= 15 and all(r["holiday"] for r in holidays["days"])


# ---------- both sensors ----------
async def test_a_question_over_both_sensors_counts_the_days_both_have(world):
    first, last = ago(AIR_DAYS + 10), ago(2)
    where = [{"field": "rain", "op": ">=", "value": 1}, {"field": "pm2_5_max", "op": ">", "value": 10}]
    out = await ask(world, start=str(first), end=str(last), where=where, sort_by="temp_max", limit=20)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    both = [d for d in days if d >= ago(AIR_DAYS)]
    wet = [d for d in both if truth(d)[1] >= 1 and max(pm(t) for t in range(*day_bounds(d, TZ), 300)) > 10]
    assert out["days_checked"] == len(both) and out["matching_days"] == len(wet)
    assert sorted(r["date"] for r in out["days"]) == sorted(label(d) for d in wet)
    assert [r["temp_max"] for r in out["days"]] == sorted((r["temp_max"] for r in out["days"]), reverse=True)
    assert out["units"] == {"temp_max": "°C", "temp_min": "°C", "rain": "mm", "pm2_5_max": "µg/m³"}   # both sensors' shown fields
    row = out["days"][0]
    assert row["source"] == {"station": "exact", "air": "exact"} and row["day_look"]["word"] in ("wet", "showery", "stormy")
    assert out["condition"] == "rain ≥ 1 mm and PM2.5 > 10 µg/m³" and out["note_missing"].startswith("10 day(s)")
    when = days_tool.period_words(f"{day_text(first)} - {day_text(last)}")
    assert out["answer"] == f"{len(wet)} of {len(both)} days with rain ≥ 1 mm and PM2.5 > 10 µg/m³ ({when})."


async def test_days_whose_station_figures_are_daily_records_are_left_out_of_a_mixed_question_and_counted(world):
    first, last = ago(OLD_AIR[0]), ago(OLD_AIR[1])
    out = await ask(world, start=str(first), end=str(last), where=[{"field": "pm2_5_max", "op": ">", "value": 0}], sort_by="rain")
    assert out["days_checked"] == 0 and out["daily_left_out"] == OLD_AIR[0] - OLD_AIR[1] + 1
    assert "daily records, which run 1" in out["note_daily_left_out"] and "note_daily" not in out and "note" in out
    alone = await ask(world, start=str(first), end=str(last), where=[{"field": "pm2_5_max", "op": ">", "value": 0}])
    assert alone["days_checked"] == OLD_AIR[0] - OLD_AIR[1] + 1 and "note_hourly" in alone   # the air sensor alone: every day
    assert all(r["source"] == "hourly" for r in alone["days"])


# ---------- part of each day ----------
async def test_hours_works_out_each_days_figures_over_the_stretch(world):
    afternoon = await ask(world, hours="afternoon", period="last 30 days", where=[{"field": "rain", "op": ">=", "value": 1}],
                          sort_by="rain", limit=20)
    morning = await ask(world, hours="morning", period="last 30 days", where=[{"field": "rain", "op": ">", "value": 0}])
    whole = await ask(world, period="last 30 days", where=[{"field": "rain", "op": ">=", "value": 1}], limit=20)
    assert morning["matching_days"] == 0                                    # the fake's rain falls from 2pm
    assert afternoon["matching_days"] == whole["matching_days"] > 0        # ... all of it in the afternoon
    assert {r["date"]: r["rain"] for r in afternoon["days"]} == {r["date"]: r["rain"] for r in whole["days"]}
    assert afternoon["hours"]["text"] == "afternoon (12:00–18:00)" and "mm that fell in it" in afternoon["hours"]["how_to_read"]
    assert afternoon["answer"].startswith("Each afternoon (12:00–18:00): ")
    assert afternoon["period"]["asked"] == "last 30 days"


async def test_a_moment_is_the_reading_at_that_time_each_day(world):
    out = await ask(world, hours="15:00", start=str(ago(10)), end=str(ago(2)), stat="avg", of="temp_avg")
    at_three = [round(temp(local_epoch(datetime.combine(d, time(15)), TZ)), 1) for d in (ago(n) for n in range(2, 11))]
    assert out["days_checked"] == 9 and out["value"] == round(sum(at_three) / len(at_three), 1)
    assert out["answer"] == f"Each day at 15:00: Average temperature: {out['value']:g} °C over 9 days " \
                            f"({days_tool.period_words(f'{day_text(ago(10))} - {day_text(ago(2))}')})."


async def test_a_night_across_midnight_belongs_to_the_day_it_ends_on_and_today_counts_once_it_is_over(world):
    out = await ask(world, hours="night", period="this week", sort_by="temp_min", order="asc", limit=20)
    over_now = over(parse_stretch("night"), ago(0), TZ, datetime.now(TZ).timestamp())
    days = [datetime.strptime(r["date"], "%a %d %b %Y").date() for r in out["days"]]
    assert out["days_checked"] == 7 and (ago(0) in days) == over_now and (ago(7) in days) == (not over_now)
    assert ("cut" in out["period"]) == (not over_now)
    d = days[0]
    night = range(local_epoch(datetime.combine(d - timedelta(days=1), time(22)), TZ), local_epoch(datetime.combine(d, time(6)), TZ), 300)
    assert out["days"][0]["temp_min"] == pytest.approx(min(temp(t) for t in night), abs=0.06)


async def test_a_whole_day_stretch_gives_the_day_rows_figures_and_leaves_out_days_held_only_as_daily_records(world):
    first, last = ago(374), ago(356)   # across the start of the station's 30-minute archive: every day listed
    args = {"start": str(first), "end": str(last), "sort_by": "temp_max", "limit": 20, "where": [{"field": "rain", "op": ">=", "value": 0}]}
    whole, stretch = await ask(world, **args), await ask(world, hours="00:00-24:00", **args)
    figures = lambda rows: {r["date"]: (r["temp_max"], r["temp_min"], r["rain"]) for r in rows}
    exact = figures(r for r in whole["days"] if r["source"] == "exact")
    assert whole["days_checked"] == 19 and 0 < len(exact) < 19
    assert figures(stretch["days"]) == exact and stretch["days_checked"] == len(exact)
    assert stretch["left_out"] == {"not held finely enough": 19 - len(exact)} and "note_left_out" in stretch and "note_daily" not in stretch
    assert stretch["hours"]["text"] == "00:00–24:00"


# ---------- charts ----------
def chart_of(out: dict) -> dict:
    (chart,) = out["charts"]
    return chart


async def test_a_figure_per_month_is_drawn_and_its_drawn_by_redraws_it_over_each_period_it_lists(world):
    out = await ask(world, period="last 6 months", stat="sum", of="rain", group_by="month", chart_answers_it=True)
    chart = chart_of(out)
    assert out["chart_note"] == CHART_NOTE and "chart" not in out and out["by_month"]
    assert chart["title"] == "Total rain" and chart["source"] == "Ecowitt weather station"
    assert schema_errors(chart) == []
    drawn = chart["drawn_by"]
    assert drawn == {"tool": "days", "arguments": {"where": [], "sort_by": "rain", "order": "desc", "count_only": False, "group_by": "month",
                                                   "stat": "sum", "of": "rain", "limit": 5, "period": "last 6 months", "chart_answers_it": True},
                     "periods": [{"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"}, {"period": "on record", "label": "All"}]}
    again = await ask(world, **drawn["arguments"])
    assert chart_of(again) == chart
    for p in drawn["periods"]:
        redrawn = await ask(world, **{**drawn["arguments"], "period": p["period"]})
        assert "error" not in redrawn and chart_of(redrawn)["drawn_by"]["arguments"]["period"] == p["period"], p
        assert chart_of(redrawn)["title"] == chart["title"]
    assert out["reply"] == out["answer"] and out["complete"] is True   # the chart is the whole answer (3.7)
    assert out["answer"].startswith("Total rain: ") and " mm over 182 days (" in out["answer"]


async def test_a_chart_drawn_with_dates_names_them_as_its_period_and_per_year_it_lists_no_other(world):
    out = await ask(world, start=str(ago(200)), end=str(ago(2)), where=[{"field": "rain", "op": ">=", "value": 1}], group_by="year")
    drawn = chart_of(out)["drawn_by"]
    assert drawn["arguments"]["period"] == f"{ago(200).isoformat()} to {ago(2).isoformat()}" and drawn["periods"] == []
    assert chart_of(await ask(world, **drawn["arguments"])) == chart_of(out)
    assert "complete" not in out   # (chart_answers_it wasn't set)
    for end in ({}, {"end": str(ago(-3))}):   # a start with no end, or an end today or later: "to now", so a replay moves on with the clock
        drawn = chart_of(await ask(world, start=str(ago(200)), where=[{"field": "rain", "op": ">=", "value": 1}], group_by="year", **end))["drawn_by"]
        assert drawn["arguments"]["period"] == f"{ago(200).isoformat()} to now"


async def test_a_period_sent_with_start_and_end_beside_it_wins_and_its_chart_is_drawn_by_the_words(world):
    # the live evals: period with start 'count_only', end 'cancel' (a model filling in every argument)
    out = await ask(world, period="last 30 days", start="count_only", end="cancel", where=[{"field": "rain", "op": ">=", "value": 1}],
                    group_by="month")
    assert "error" not in out and out["period"]["asked"] == "last 30 days" and "were ignored" in out["period"]["note"]
    assert chart_of(out)["drawn_by"]["arguments"]["period"] == "last 30 days"


async def test_a_chart_is_not_the_whole_answer_when_a_note_needs_the_models_words(world):
    out = await ask(world, period="on record", stat="max", of="temp_max", group_by="month", chart_answers_it=True)
    assert "charts" in out and "note_daily" in out and "complete" not in out and "reply" not in out
    averaged = await ask(world, period="last 6 months", where=[{"field": "humidity_max", "op": ">", "value": 70}], group_by="month",
                         chart_answers_it=True)
    assert "note_averaged" in averaged and averaged["complete"] is True   # (the answer line says it)
    assert averaged["reply"].endswith("Older days use averages, so a count near the limit may fall slightly short.")
    plain = await ask(world, period="past year", where=[{"field": "rain", "op": ">=", "value": 1}], chart_answers_it=True)
    assert "charts" not in plain and "complete" not in plain   # no chart, nothing complete


async def test_a_chart_of_part_of_each_day_lists_the_whole_record_only_while_it_is_within_400_days(world):
    out = await ask(world, hours="night", period="3m", where=[{"field": "temp_min", "op": "<", "value": 12}], group_by="month")
    drawn = chart_of(out)["drawn_by"]
    assert drawn["arguments"]["hours"] == "night" and [p["period"] for p in drawn["periods"]] == ["3m", "1y"]   # (548 days held)
    assert "night (22:00–06:00)" in chart_of(out)["subtitle"]
    for p in drawn["periods"]:
        assert "error" not in await ask(world, **{**drawn["arguments"], "period": p["period"]})
    air = await ask(world, hours="afternoon", period="last 30 days", stat="avg", of="pm2_5_avg", group_by="month")
    assert [p["period"] for p in chart_of(air)["drawn_by"]["periods"]] == ["3m", "1y", "on record"]   # the air sensor's record is short
    assert chart_of(air)["source"] == "AirGradient sensor"
    for p in chart_of(air)["drawn_by"]["periods"]:
        assert "error" not in await ask(world, **{**chart_of(air)["drawn_by"]["arguments"], "period": p["period"]})


async def test_charts_are_sent_to_the_requests_budget(world):
    args = {"period": "on record", "stat": "sum", "of": "rain", "group_by": "month"}
    assert chart_of(await ask(world, {"chart_points": 500}, **args)) == chart_of(await ask(world, **args))   # (bars aren't bucketed)
    both = await ask(world, period="last 30 days", where=[{"field": "rain", "op": ">=", "value": 0},
                                                           {"field": "pm2_5_max", "op": ">", "value": 0}], group_by="month")
    assert chart_of(both)["source"] == "Ecowitt weather station and AirGradient sensor"
    replay = copy.deepcopy(chart_of(both)["drawn_by"]["arguments"])
    assert chart_of(await ask(world, **replay)) == chart_of(both)
