"""The `summary` tool (lib/service/summary.py): its arguments and defaults, the shape of what it answers, the refusals that teach, the
periods it reads, its charts with drawn_by (replayed, and over every period it lists), and the caption the bot writes today for the same
chart (lib/report.py), word for word."""

import json
from datetime import datetime, timedelta

from lib import report
from lib.airgradient import AirGradient
from lib.bot import _as_drawn
from lib.compose import Composer
from lib.ecowitt.days import day_values_on
from lib.service import summary
from lib.service.common import FRESH, ServiceTool
from lib.service.context import Context
from lib.service.daylook import FIGURES, day_look
from lib.tools import Turn
from tests.fakes import TZ, air_transport, archived_station, config
from tests.test_chart_json import errors as schema_errors


async def make(tmp_path, archived_cache, air: bool = False, **air_options):
    """(the tool, its Context, the fake Ecowitt): the archived station, and with `air` an AirGradient on the same file."""
    eco, fake = await archived_station(tmp_path, archived_cache)
    source = None
    if air:
        transport, _ = air_transport(**{"rows_per_day": 288, **air_options})
        source = AirGradient(config(tmp_path), transport=transport, storage=eco.storage)
        await source.start()
    ctx = await Context.open(eco=eco, air=source, composer=Composer(eco, source) if source else None)
    return summary.tools(ctx)[0], ctx, fake


async def close(ctx):
    if ctx.air:
        await ctx.air.close()
    await ctx.eco.close()


async def ask(tool: ServiceTool, meta: dict | None = None, **args) -> dict:
    return json.loads(await tool.call(args, meta))


def without_drawn_by(chart: dict) -> dict:
    return {k: v for k, v in chart.items() if k != "drawn_by"}


def days_ago(n: int) -> str:
    return str(datetime.now(TZ).date() - timedelta(days=n))


# ---------- the tool ----------
async def test_the_tool_says_what_it_answers_what_it_is_not_for_and_how_to_read_it(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    assert tool.name == "summary" and tool.annotations == {"readOnlyHint": True, "openWorldHint": True}
    assert list(tool.parameters["properties"]) == ["readings", "period", "start", "end", "average", "every_day", "chart", "chart_readings",
                                                   "layout", "invert_second_axis", "chart_answers_it"]
    text = tool.description
    assert len(text) <= 1500
    for words in ("{readings: [\"temperature\"], period: \"this week\"}", "chart_answers_it: true", "average: true", "\"all\"", "pm2.5",
                  ": days", ": readings", ": relate", ": current", ": forecast", "low_text", "day_look", "rain_total_mm", "complete"):
        assert words in text, words
    readings = tool.parameters["properties"]["readings"]["description"]
    assert "outdoor.temperature" in readings and "air.pm2_5" not in readings and readings.endswith(   # (what this station holds)
        "Default: outdoor and indoor temperature and humidity.")
    assert tool.parameters["properties"]["period"]["description"].endswith("Default: today; with chart true and no period, the last 24 hours.")
    assert tool.parameters["properties"]["layout"]["enum"] == ["auto", "panels", "two_axes", "same_axis"]
    await close(ctx)


async def test_by_default_it_is_todays_outdoor_and_indoor_temperature_and_humidity_with_no_chart(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool)
    assert list(out)[:4] == ["period", "as_of", "fresh", "series"] and out["fresh"] == FRESH
    assert out["period"]["asked"] == "today" and out["period"]["text"].startswith("today: ")
    assert list(out["series"]) == ["outdoor.temperature", "indoor.temperature", "outdoor.humidity"]   # (the fake's indoor has no humidity)
    assert "charts" not in out and "caption" not in out and "complete" not in out
    await close(ctx)


# ---------- the figures ----------
async def test_each_record_comes_with_its_wording_its_day_and_that_days_look(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["outdoor.temperature", "pressure"], period="this week", chart=False)
    assert list(out["series"]) == ["outdoor.temperature", "pressure.relative"] and "charts" not in out
    values, _ = await ctx.eco.storage.read(day_values_on, ctx.eco.mac, list(FIGURES),
                                           [ctx.now().date() - timedelta(days=n) for n in range(7)])
    today = ctx.now().date()

    def looked(day):
        return day_look({n: values[n].get(day) for n in FIGURES}, latitude=-37.8, day=day, so_far=day == today)
    temp = out["series"]["outdoor.temperature"]
    assert temp["unit"] == "°C" and isinstance(temp["low"], float) and temp["low"] <= temp["high"]
    for want in ("low", "high"):
        assert temp[f"{want}_text"] == f"{temp[want]} °C"
        assert temp[f"{want}_when"].startswith(("at ", "around ")) and temp[f"{want}_date"].endswith(" 2026")
        day = datetime.strptime(temp[f"{want}_date"], "%a %d %b %Y").date()
        assert temp.get(f"{want}_day_look") == looked(day)
    rows = temp["daily"]
    assert len(rows) == 7 and list(rows)[-1] == f"{today:%a} {today.day} {today:%b}"
    for label, row in rows.items():
        day = next(d for d in (today - timedelta(days=n) for n in range(7)) if f"{d:%a} {d.day} {d:%b}" == label)
        assert row.get("day_look") == looked(day) and row["low"] <= row["high"]
    assert any(row.get("day_look", {}).get("word") == "wet" for row in rows.values())   # the fake's every 8th day is wet
    assert "average" not in temp
    await close(ctx)


async def test_an_average_is_by_time_and_comes_with_each_days(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["outdoor.temperature"], period="this week", average=True, chart=False)
    temp = out["series"]["outdoor.temperature"]
    assert temp["low"] < temp["average"] < temp["high"] and temp["average_text"] == f"{temp['average']} °C"
    assert all("average" in row for row in temp["daily"].values())
    old = json.loads(await ctx.eco._history({"start_date": f"{days_ago(6)} 00:00:00", "end_date": datetime.now(TZ).strftime(
        "%Y-%m-%d %H:%M:%S"), "groups": "outdoor", "average": True}))
    assert float(old["series"]["outdoor.temperature"]["average"]) == temp["average"]   # weather_history's own
    await close(ctx)


async def test_a_period_over_a_week_lists_the_days_of_its_records_or_with_every_day_all_of_them(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    some = (await ask(tool, readings=["outdoor.temperature"], period="last 14 days", chart=False))["series"]["outdoor.temperature"]
    assert 1 <= len(some["daily"]) <= 2 and {some["low_date"][:-5], some["high_date"][:-5]} == set(some["daily"])
    every = (await ask(tool, readings=["outdoor.temperature"], period="last 14 days", chart=False, every_day=True))["series"]
    assert len(every["outdoor.temperature"]["daily"]) == 14
    longer = (await ask(tool, readings=["outdoor.temperature"], period="last 40 days", chart=False, every_day=True))["series"]
    assert "monthly" in longer["outdoor.temperature"] and "daily" not in longer["outdoor.temperature"]   # by month, past 31 days
    await close(ctx)


async def test_rain_is_the_mm_that_fell_its_total_and_its_wettest_day(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["rain"], period="last 14 days", chart=False)
    rain = out["series"]["rainfall.rain"]
    assert "charts" not in out and out["rain_total_mm"] == rain["total"] > 0 and rain["total_text"] == f"{rain['total']:.1f} mm"
    assert rain["wettest_day"]["mm"] == 5.0 and rain["wettest_day"]["day_look"]["word"] == "wet"   # the fake's wet day: 5 mm
    assert list(rain["daily"]) == [next(iter(rain["daily"]))] and "low" not in rain              # 14 days: only the wettest listed
    every = await ask(tool, readings=["rain"], period="last 14 days", chart=False, every_day=True)
    assert len(every["series"]["rainfall.rain"]["daily"]) == 14
    assert round(sum(r["total"] for r in every["series"]["rainfall.rain"]["daily"].values()), 1) == rain["total"]
    drawn = await ask(tool, readings=["rain"], period="last 14 days")                             # the chart's total is the same
    old_turn = Turn()
    old = json.loads(await ctx.eco._history({"start_date": f"{days_ago(13)} 00:00:00", "end_date": datetime.now(TZ).strftime(
        "%Y-%m-%d %H:%M:%S"), "groups": "rainfall", "chart_field": "rain", "chart": True}, old_turn))
    assert drawn["rain_total_mm"] == old["rain_total_mm"] == rain["total"]
    long = await ask(tool, readings=["rain"], period="3m", chart=False)
    assert list(long["series"]["rainfall.rain"]["monthly"])[0].endswith(" 2026")
    await close(ctx)


async def test_wind_is_its_speed_and_its_direction_by_compass_point(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["wind", "gust"], period="this week", chart=False)
    assert list(out["series"]) == ["wind.wind_speed", "wind.wind_direction", "wind.wind_gust"]
    direction = out["series"]["wind.wind_direction"]
    assert direction["most_common"].startswith(("N ", "NNW", "NNE")) and direction["steadiness"] and "low" not in direction
    assert "daily" in direction
    await close(ctx)


async def test_the_air_sensors_figures_are_air_qualitys_with_their_wording(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True)
    out = await ask(tool, readings=["pm2.5", "co2", "air.temperature"], period="last 2 days", chart=False)
    assert list(out["series"]) == ["air.pm2_5", "air.co2", "air.temperature"]
    start, end = (datetime.fromisoformat(out["period"][k]).strftime("%Y-%m-%d %H:%M:%S") for k in ("start", "end"))
    old = json.loads(await ctx.air.handle({"start_date": start, "end_date": end, "metrics": ["air_temperature"]}))
    pm = out["series"]["air.pm2_5"]
    for k in ("low", "high", "average", "high_rating", "average_rating", "high_aqi_us", "high_band", "average_aqi_us", "average_band"):
        assert pm[k] == old["pm2_5"][k], k
    at = datetime.strptime(old["pm2_5"]["high_time"], "%a %d %b %Y %I:%M%p")             # "Wed 4 Nov 2026 10:00am"
    assert pm["high_text"] == f"{pm['high']} µg/m³" and pm["high_date"] == f"{at:%a} {at.day} {at:%b %Y}"
    assert pm["high_when"] == f"at {at.hour % 12 or 12}{'' if not at.minute else f':{at:%M}'}{'am' if at.hour < 12 else 'pm'}"
    assert out["series"]["air.temperature"]["average"] == old["air_temperature"]["average"]
    assert "high_rating" not in out["series"]["air.temperature"]                                # its own temperature has no rating
    await close(ctx)


# ---------- periods ----------
async def test_a_chart_comes_for_three_days_or_more_and_chart_true_alone_is_the_last_24_hours(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    assert "charts" not in await ask(tool, readings=["pressure"], period="yesterday")
    assert len((await ask(tool, readings=["pressure"], period="last 3 days"))["charts"]) == 1
    assert "charts" not in await ask(tool, readings=["pressure"], period="last 3 days", chart=False)
    out = await ask(tool, readings=["pressure"], chart=True)
    assert out["period"]["asked"] == "last 24 hours" and len(out["charts"]) == 1
    assert out["charts"][0]["drawn_by"]["arguments"]["period"] == "last 24 hours"
    assert (await ask(tool, readings=["pressure"], chart="true"))["period"]["asked"] == "last 24 hours"   # (as text too)
    await close(ctx)


async def test_on_record_starts_at_the_first_day_held(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["outdoor.temperature"], period="on record", chart=False)
    first = datetime.fromisoformat(out["period"]["start"]).date()
    assert (ctx.now().date() - first).days in range(540, 560)   # the fake station is 548 days old
    assert "monthly" in out["series"]["outdoor.temperature"]
    await close(ctx)


async def test_whole_days_sent_as_dates_are_drawn_by_as_words_and_a_time_lists_no_periods(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    dated = await ask(tool, readings=["pressure"], start=days_ago(9), end=days_ago(5))
    words = dated["charts"][0]["drawn_by"]
    assert words["arguments"]["period"] == f"{days_ago(9)} to {days_ago(5)}" and "start" not in words["arguments"]
    assert [p["period"] for p in words["periods"]] == ["1d", "7d", "1m", "3m", "1y"]
    again = await ask(tool, **words["arguments"])
    assert without_drawn_by(again["charts"][0]) == without_drawn_by(dated["charts"][0]) and again["period"]["text"] == dated["period"]["text"]
    since = await ask(tool, readings=["pressure"], start=days_ago(4))
    assert since["charts"][0]["drawn_by"]["arguments"]["period"] == f"{days_ago(4)} to now"
    timed = await ask(tool, readings=["pressure"], start=f"{days_ago(4)} 06:00", chart=True)
    by = timed["charts"][0]["drawn_by"]
    assert by["arguments"]["start"] == f"{days_ago(4)} 06:00" and "period" not in by["arguments"] and by["periods"] == []
    assert without_drawn_by((await ask(tool, **by["arguments"]))["charts"][0]) == without_drawn_by(timed["charts"][0])
    await close(ctx)


# ---------- charts and drawn_by ----------
async def replays(tool: ServiceTool, out: dict) -> dict:
    """Check the result's chart: drawn_by draws it again, and draws a chart over each period it lists, never refused."""
    (chart,) = out["charts"]
    by = chart["drawn_by"]
    assert by["tool"] == "summary" and set(by) == {"tool", "arguments", "periods"}
    assert schema_errors(chart) == []   # drawn_by and all
    again = await ask(tool, **by["arguments"])
    assert [without_drawn_by(c) for c in again["charts"]] == [without_drawn_by(chart)] and again["charts"][0]["drawn_by"] == by
    for period in by["periods"]:
        other = await ask(tool, **{**by["arguments"], "period": period["period"]})
        assert "error" not in other and len(other["charts"]) == 1, (period, other.get("error"))
        assert other["charts"][0]["title"] == chart["title"], period
    return by


async def test_a_station_chart_says_how_to_draw_it_again_and_over_which_periods(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, period="this week")   # the usual set: a panel each, outdoor and indoor lines in each
    chart = out["charts"][0]
    assert [p["label"] for p in chart["panels"]] == ["Temperature", "Humidity"] and chart["source"] == "Ecowitt weather station"
    assert [s["name"] for s in chart["panels"][0]["series"]] == ["Indoor", "Outdoor"]
    by = await replays(tool, out)
    assert by["arguments"] == {"readings": ["outdoor.temperature", "indoor.temperature", "outdoor.humidity"], "period": "this week",
                               "average": False, "every_day": False, "chart": True,
                               "chart_readings": ["outdoor.temperature", "indoor.temperature", "outdoor.humidity"], "layout": "auto",
                               "invert_second_axis": False, "chart_answers_it": False}
    assert by["periods"] == [{"period": "1d", "label": "1D"}, {"period": "7d", "label": "7D"}, {"period": "1m", "label": "1M"},
                             {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"}]
    assert out["chart_note"] and max(len(s["points"]) for p in chart["panels"] for s in p["series"]) <= 150
    await close(ctx)


async def test_wind_is_drawn_with_its_compass_and_one_reading_named_draws_only_that_one(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["wind"], period="this week")
    chart = out["charts"][0]
    assert chart["title"] == "Wind" and "compass" in chart["panels"][0]
    await replays(tool, out)
    outdoor = await ask(tool, readings=["temperature"], chart_readings=["indoor.temperature"], period="this week")
    assert [s["name"] for s in outdoor["charts"][0]["panels"][0]["series"]] == ["Indoor"]   # figures for both, the line asked for
    assert list(outdoor["series"]) == ["outdoor.temperature", "indoor.temperature"]
    everything = await ask(tool, readings=["all"], period="this week")
    assert [p["label"] for p in everything["charts"][0]["panels"]] == ["Temperature", "Humidity", "Solar radiation", "Pressure",
                                                                     "Dew point", "Wind"]   # (rain behind the humidity)
    await close(ctx)


async def test_an_average_draws_each_days_average_and_lists_no_one_day_period(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["pressure"], period="this week", average=True)
    assert "daily averages" in out["charts"][0]["subtitle"]
    by = await replays(tool, out)
    assert [p["period"] for p in by["periods"]] == ["7d", "1m", "3m", "1y"]
    short = await ask(tool, readings=["pressure"], period="today", average=True, chart=True)
    assert "charts" not in short and "whole day" in short["note_chart"]
    await close(ctx)


async def test_two_axes_and_the_second_axis_upside_down(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    out = await ask(tool, readings=["outdoor.temperature", "pressure"], period="this week", layout="two_axes", invert_second_axis=True)
    (panel,) = out["charts"][0]["panels"]
    assert panel["flip_right"] is True and [s["right"] for s in panel["series"]] == [False, True]
    await replays(tool, out)
    plain = await ask(tool, readings=["outdoor.temperature", "pressure"], period="this week", layout="two_axes")
    assert plain["charts"][0]["panels"][0]["flip_right"] is False
    same = await ask(tool, readings=["outdoor.temperature", "dew point"], period="this week", layout="same_axis")
    (panel,) = same["charts"][0]["panels"]
    assert len(panel["series"]) == 2 and same["charts"][0]["period"][1] == days_ago(1)   # plot_chart's: whole days to yesterday
    await close(ctx)


async def test_an_air_chart_is_the_air_sensors_and_an_average_one_is_each_days(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True, rows_per_day=24)
    out = await ask(tool, readings=["pm2.5", "co2"], period="last 3 days")
    chart = out["charts"][0]
    assert chart["title"] == "Air quality" and [p["label"] for p in chart["panels"]] == ["PM2.5", "CO₂"]
    assert chart["source"] == "AirGradient sensor"
    await replays(tool, out)
    daily = await ask(tool, readings=["pm2.5"], period="last 3 days", average=True)
    assert "daily averages" in daily["charts"][0]["subtitle"] and len(daily["charts"][0]["panels"][0]["series"][0]["points"]) == 2
    await close(ctx)


async def test_a_chart_of_both_sensors_or_a_rating_is_drawn_over_whole_days_and_says_so(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True, rows_per_day=24)
    out = await ask(tool, readings=["pm2.5", "rain"], chart_readings=["air.pm2_5:rating", "rain"], period="this week")
    chart = out["charts"][0]
    assert chart["source"] == "Ecowitt weather station and AirGradient sensor" and chart["period"] == [days_ago(6), days_ago(1)]
    assert chart["panels"][0]["series"][0]["kind"] == "rating" and chart["panels"][1]["series"][0]["kind"] == "bars"
    assert "up to yesterday" in out["notes"]["chart"] and out["notes"]["chart"] in out["caption"]
    assert list(out["series"]) == ["air.pm2_5", "rainfall.rain"] and out["rain_total_mm"] >= 0
    by = await replays(tool, out)
    assert by["arguments"]["chart_readings"] == ["air.pm2_5:rating", "rainfall.rain"]
    await close(ctx)


async def test_a_chart_of_both_sensors_has_each_readings_line_and_the_cut_in_its_caption(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True)
    mixed = await ask(tool, readings=["outdoor.temperature", "pm2.5"], period="last 3 days", chart_answers_it=True)
    assert [p["label"] for p in mixed["charts"][0]["panels"]] == ["Temperature", "PM2.5"]
    lines = mixed["caption"].splitlines()
    assert lines[1].startswith("Temperature: low ") and lines[2].startswith("• PM2.5: peak ") and lines[3].startswith("(The chart draws")
    assert mixed["reply"] == mixed["caption"] and mixed["complete"] is True   # the cut is in the caption: nothing left for the model
    await close(ctx)


# ---------- the caption, as the bot writes it today ----------
async def old_caption(ctx, out: dict, tool: str, args: dict) -> str:
    """lib/report.chart_caption over today's tool (weather_history, air_quality) for the same period, as the bot calls it."""
    start, end = (datetime.fromisoformat(out["period"][k]) for k in ("start", "end"))
    args = {"start_date": start.strftime("%Y-%m-%d %H:%M:%S"), "end_date": (end - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "chart": True, **args}
    turn = Turn()
    handler = ctx.eco._history if tool == "weather_history" else ctx.air.handle
    result = await handler(args, turn)
    drawn_args, drawn = _as_drawn(tool, args, turn)
    return report.chart_caption(tool, drawn_args, result, drawn, TZ)


async def test_the_caption_is_the_one_the_bot_writes_today_word_for_word(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True)
    for args, old in (({"readings": ["pressure"], "period": "this week"}, {"groups": "pressure", "chart_field": "pressure"}),
                      ({"period": "this week"}, {"groups": "outdoor,indoor", "chart_fields": ["temperature", "humidity"]}),
                      ({"readings": ["temperature"], "period": "this week", "average": True},
                       {"groups": "outdoor,indoor", "chart_field": "temperature", "average": True}),
                      ({"readings": ["rain", "wind"], "period": "last 10 days"}, {"groups": "rainfall,wind", "chart_fields": ["rain", "wind"]}),
                      ({"readings": ["wind"], "period": "last 3 days"}, {"groups": "wind", "chart_field": "wind"}),
                      ({"readings": ["rain"], "period": "last 3 days"}, {"groups": "rainfall", "chart_field": "rain"})):
        out = await ask(tool, **args)
        assert out["caption"] == await old_caption(ctx, out, "weather_history", old), args
    out = await ask(tool, readings=["pm2.5", "co2"], period="last 3 days")
    assert out["caption"] == await old_caption(ctx, out, "air_quality", {"metrics": ["pm2_5", "co2"]})
    assert out["caption"].splitlines()[1].startswith("• PM2.5: peak ")
    await close(ctx)


async def test_with_no_chart_the_figures_come_in_the_words_the_bot_writes_for_a_lookup_today(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True)
    for args, old, names in (({"period": "yesterday"}, {"groups": "outdoor,indoor"}, ["temperature", "humidity"]),
                             ({"readings": ["temperature"], "period": "yesterday", "average": True},
                              {"groups": "outdoor,indoor", "average": True}, ["temperature"]),
                             ({"readings": ["wind"], "period": "yesterday"}, {"groups": "wind"}, ["wind"]),
                             ({"readings": ["outdoor.humidity", "pressure"], "period": "yesterday"}, {"groups": "outdoor,pressure"},
                              ["humidity", "pressure"])):
        out = await ask(tool, **args)
        start, end = (datetime.fromisoformat(out["period"][k]) for k in ("start", "end"))
        result = await ctx.eco._history({"start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                                         "end_date": (end - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), **old})
        assert "charts" not in out and out["text"] == report.lookup("extremes", result, names, [], ctx.now().date()), args
    rain = await ask(tool, readings=["rain", "pm2.5"], period="yesterday")
    lines = rain["text"].splitlines()
    assert lines[1].startswith("☔ Rain per hour: least 0 mm") and lines[1].endswith(f" · {rain['rain_total_mm']:.1f} mm in all")
    assert lines[2].startswith("• PM2.5: peak ") and len(lines) == 3
    direction = await ask(tool, readings=["wind direction"], period="yesterday")
    assert direction["text"].splitlines()[1].startswith("🧭 Mostly from the N")
    await close(ctx)


async def test_a_chart_that_answers_the_question_comes_with_its_reply_unless_a_note_needs_words(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True, hourly_before=datetime.now(TZ) - timedelta(days=3))
    out = await ask(tool, readings=["pressure"], period="this week", chart_answers_it=True)
    assert out["reply"] == out["caption"] and out["complete"] is True and list(out)[-2:] == ["reply", "complete"]
    assert out["charts"][0]["drawn_by"]["arguments"]["chart_answers_it"] is True
    assert "complete" not in await ask(tool, readings=["pressure"], period="this week")              # not asked
    assert "complete" not in await ask(tool, readings=["pressure"], period="yesterday", chart_answers_it=True)   # no chart
    hourly = await ask(tool, readings=["pm2.5"], period="last 7 days", chart_answers_it=True)
    assert "resolution" in hourly["notes"]["air"] and "caption" in hourly and "complete" not in hourly   # the model words the note
    assert summary.Summary._needs_words({"warning": "x", "missing": ["y"], "notes": {"air": {"missing": "z"}, "chart": "c"}}) is False
    assert summary.Summary._needs_words({"monthly_note": "x"}) and summary.Summary._needs_words({"note_no_readings": "x"})
    await close(ctx)


# ---------- refusals that teach ----------
async def test_a_call_that_cant_be_answered_is_refused_with_what_to_send_instead(tmp_path, archived_cache):
    tool, ctx, _ = await make(tmp_path, archived_cache, air=True)
    out = await ask(tool, start_date="2026-11-01")
    assert "start_date" in out["error"] and "`start`" in out["error"] and "period" in out["arguments"]
    out = await ask(tool, readings=["sunniness"])
    assert "'sunniness' isn't a reading" in out["error"] and "outdoor.temperature" in out["names"] and out["words"]["rain"]
    out = await ask(tool, period="the other week")
    assert "isn't a period" in out["error"] and "today" in out["forms"]
    assert "use forecast" in (await ask(tool, period="next week"))["error"]
    out = await ask(tool, readings=["pressure"], chart=True, layout="stacked")
    assert out["layouts"] == ["auto", "panels", "two_axes", "same_axis"]
    assert "needs layout two_axes" in (await ask(tool, readings=["pressure"], chart=True, invert_second_axis=True))["error"]
    assert "two readings or more" in (await ask(tool, readings=["pressure"], chart=True, layout="two_axes"))["error"]
    assert "rain (bars)" in (await ask(tool, readings=["pressure", "rain"], chart=True, layout="two_axes"))["error"]
    assert "have a rating" in (await ask(tool, chart_readings=["temperature:rating"], chart=True))["error"]
    out = await ask(tool, readings=["indoor.temperature", "pm2.5"], chart=True, period="this week")
    assert "indoor.temperature can't go on a chart that mixes" in out["error"] and "air.pm2_5" in out["drawable"]
    out = await ask(tool, readings=["pm2.5", "pm10", "co2", "voc", "rain"], chart=True, period="this week")
    assert "at most 4 readings" in out["error"]
    out = await ask(tool, readings=["pm2.5", "rain"], chart=True, period="today")
    assert "whole days" in out["error"] and "'last 7 days'" in out["error"]
    assert "true or false" in (await ask(tool, chart="maybe"))["error"]
    assert "names no reading" in (await ask(tool, chart=True, chart_readings=[":rating"]))["error"]
    every = summary.tools(Context(eco=ctx.eco))[0]   # (every reading the service knows: rain rate is drawn only alone)
    out = await ask(every, readings=["pressure", "rain rate"], chart=True)
    assert "rainfall.rain_rate can only be drawn on a chart of its own" in out["error"]
    assert "the piezo rain gauge" in (await ask(every, readings=["piezo"], chart=True))["error"].lower()
    out = await ask(tool, readings=["pressure"], period="2021")
    assert "goes back 4 years" in out["error"] and out["held_from"]
    await close(ctx)


async def test_with_only_the_air_sensor_its_readings_are_the_default_and_no_day_has_a_look(tmp_path):
    transport, _ = air_transport(rows_per_day=288)
    air = AirGradient(config(tmp_path), transport=transport)
    await air.start()
    ctx = await Context.open(air=air)
    tool = summary.tools(ctx)[0]
    out = await ask(tool, period="last 2 days")
    assert list(out["series"]) == ["air.pm2_5", "air.pm10", "air.pm1", "air.co2", "air.voc_index", "air.nox_index"]
    assert not any("low_day_look" in s or "high_day_look" in s for s in out["series"].values())
    assert out["text"].splitlines()[1].startswith("• PM2.5: peak ") and len(out["text"].splitlines()) == 7
    out = await ask(tool, readings=["pm2.5", "pm10"], chart=True, period="last 2 days", layout="two_axes")
    assert "only where there is a weather station" in out["error"]
    assert "doesn't hold" in (await ask(tool, readings=["temperature"]))["error"]          # a bare temperature is the station's
    await air.close()


async def test_nothing_held_is_refused_with_where_the_record_starts(tmp_path, archived_cache):
    tool, ctx, fake = await make(tmp_path, archived_cache)
    out = await ask(tool, {"fetch": False}, readings=["pressure"], period="June 2024")   # before the fake station, asking nobody
    assert out["error"].startswith("Nothing is held of pressure.relative for June 2024") and out["held_from"]
    assert fake.calls == []
    await close(ctx)


# ---------- the request's metadata ----------
async def test_a_chart_drawn_ahead_asks_after_the_rest_and_a_chart_points_budget_is_kept(tmp_path, archived_cache, monkeypatch):
    tool, ctx, _ = await make(tmp_path, archived_cache)
    urgent = []
    real = summary.Fetcher.__init__

    def recorded(self, *args, **kwargs):
        real(self, *args, **kwargs)
        urgent.append(self.urgent)
    monkeypatch.setattr(summary.Fetcher, "__init__", recorded)
    await ask(tool, {"priority": "background"}, readings=["pressure"], period="this week")
    await ask(tool, readings=["pressure"], period="this week")
    assert urgent == [False, True]
    big = await ask(tool, {"chart_points": 500}, readings=["pressure"], period="this week")
    assert max(len(s["points"]) for s in big["charts"][0]["panels"][0]["series"]) > 150
    await close(ctx)
