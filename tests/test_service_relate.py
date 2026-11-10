"""The service's `relate` tool (lib/service/relate.py): rain against one reading over whole days, with the findings of the bot's
weather_link (pressure, humidity, wind) and air_link (PM2.5, PM10), and a chart that says how to draw it again."""

import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

from lib.airgradient.readings import NOT_HELD, Held
from lib.compose import Composer
from lib.ecowitt.link import link
from lib.service import relate
from lib.service.common import CHART_NOTE
from lib.service.context import Context
from lib.timeutil import local_date
from lib.tools import Tools, Turn
from tests.fakes import TZ, archived_station
from tests.test_chart_json import errors
from tests.test_compose import StubAir


class HeldStubAir(StubAir):
    """The air sensor as the composer reads it (StubAir), its readings also held in its file (`store`), as AirGradient's are: what a
    request that may not fetch reads. `fetched` counts the reads that may fetch."""

    def __init__(self, days=40):
        super().__init__(days)
        self.fetched = 0
        self.store = SimpleNamespace(held=self._held, load=self._load)

    async def rows(self, t0, t1):
        self.fetched += 1
        return await super().rows(t0, t1)

    async def _held(self, days):
        have = {local_date(r["ts"], TZ) for r in self.readings}
        return {d: Held(288, False) if d in have else NOT_HELD for d in days}

    async def _load(self, lo, hi):
        return [r for r in self.readings if lo <= r["ts"] < hi]


async def world(tmp_path, archived_cache):
    """(the context over the archived station and a stand-in air sensor, at one moment; the station)."""
    eco, _ = await archived_station(tmp_path, archived_cache)
    sensor, now = HeldStubAir(), int(time.time())
    ctx = Context(eco=eco, air=sensor, composer=Composer(eco, sensor), tz=TZ, clock=lambda: now)
    return ctx, eco


async def ask(ctx, meta: dict | None = None, **args) -> dict:
    (made,) = relate.tools(ctx)
    return json.loads(await made.call(args, meta))


def days_of(out: dict) -> tuple[date, date]:
    """The first and last day a result's period covers."""
    start, end = (date.fromisoformat(out["period"][k][:10]) for k in ("start", "end"))
    return start, end - timedelta(days=1)


def without_drawn_by(chart: dict) -> dict:
    return {k: v for k, v in chart.items() if k != "drawn_by"}


async def replays(ctx, chart: dict, **swap) -> dict:
    """The call a chart says drew it, made again (with `swap` in its arguments)."""
    drawn = chart["drawn_by"]
    return await ask(ctx, **{**drawn["arguments"], **swap})


# ---------- what it takes ----------
async def test_it_takes_a_reading_whole_days_and_a_chart_and_says_what_it_is_not_for(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    (made,) = relate.tools(ctx)
    props = made.parameters["properties"]
    assert made.name == "relate" and set(props) == {"reading", "period", "start", "end", "chart"} and "required" not in made.parameters
    assert "Whole days only, up to yesterday" in props["period"]["description"] and "the last 90 days" in props["period"]["description"]
    assert all(name in props["reading"]["description"] for name in ("pressure.relative", "outdoor.humidity", "wind.wind_speed", "air.pm2_5", "air.pm10"))
    assert len(made.description) < 1500 and "\"X against rain\" is this tool" in made.description
    assert all(f"({tool})" in made.description for tool in ("scan", "summary", "days")) and made.description.count("{reading:") >= 3
    assert made.annotations == {"readOnlyHint": True, "openWorldHint": True}   # the air sensor's missing days are fetched
    (station_only,) = relate.tools(Context(eco=eco, tz=TZ))
    assert station_only.annotations["openWorldHint"] is False
    await eco.close()


# ---------- the findings ----------
async def test_with_nothing_named_it_is_pressure_against_rain_over_the_last_90_days_as_weather_link_finds(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx)
    assert list(out)[:9] == ["period", "as_of", "fresh", "reading", "label", "unit", "verdict", "how_to_read", "findings"]
    assert (out["reading"], out["label"], out["unit"]) == ("pressure.relative", "Pressure", "hPa")
    assert out["period"]["asked"] == "last 90 days" and out["period"]["cut"].endswith("today isn't over")
    first, last = days_of(out)
    assert last == ctx.now().date() - timedelta(days=1) and (last - first).days == 89
    before, _, _, _ = link(eco.storage.read_now, eco.mac, TZ, {"start_date": str(first), "end_date": str(last)}, ctx.now())
    assert {k: v for k, v in out.items() if k in before and k != "period"} == \
        {k: v for k, v in before.items() if k not in ("period", "driver")}
    assert "charts" not in out and "chart_note" not in out and "driver" not in out   # a chart only when asked
    await eco.close()


async def test_the_words_people_use_name_the_reading(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    for words, name in (("humidity", "outdoor.humidity"), ("wind", "wind.wind_speed"), ("barometer", "pressure.relative"),
                        ("outdoor humidity", "outdoor.humidity"), ("PM2.5", "air.pm2_5"), ("pm10", "air.pm10")):
        out = await ask(ctx, reading=words, period="last 30 days")
        assert out["reading"] == name and out["verdict"] and "note_others" not in out, words
    out = await ask(ctx, reading="pm2.5")
    assert out["period"]["asked"] == "last 60 days" and out["label"] == "PM2.5"     # the air's own default
    out = await ask(ctx, reading="air", period="last 30 days")                        # the air sensor: its first that rain relates to
    assert out["reading"] == "air.pm2_5" and "air.pm10" in out["note_others"]
    out = await ask(ctx, reading="pressure and humidity", period="last 30 days")
    assert out["reading"] == "pressure.relative" and "outdoor.humidity" in out["note_others"] and "call again" in out["note_others"]
    await eco.close()


async def test_air_against_rain_is_air_links_findings(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, reading="air.pm10", period="last 3 weeks")
    first, last = days_of(out)
    before = json.loads(await ctx.composer.air_link({"metric": "pm10", "start_date": str(first), "end_date": str(last)}, Turn()))
    assert {k: v for k, v in out.items() if k in before and k != "period"} == \
        {k: v for k, v in before.items() if k not in ("period", "metric", "chart")}
    assert out["how_to_read"].startswith("Open with `verdict`") and (out["reading"], out["unit"]) == ("air.pm10", "µg/m³")
    await eco.close()


async def test_a_request_that_may_not_fetch_reads_the_air_from_the_file(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    fetched = await ask(ctx, reading="pm2.5", chart=True)
    assert ctx.air.fetched == 1
    held = await ask(ctx, {"fetch": False}, reading="pm2.5", chart=True)
    assert ctx.air.fetched == 1 and held["notes"] == {"missing": "20 older day(s) aren't in the bot's cache yet and were left out"}
    assert {k: v for k, v in held.items() if k != "notes"} == fetched   # (the 40 days it holds: those the stand-in has)
    await eco.close()


# ---------- refusals that teach ----------
async def test_what_it_cant_relate_is_refused_with_what_it_can(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, reading="temperature")
    assert "can't be set against rain" in out["error"] and out["readings"] == ["pressure.relative", "outdoor.humidity", "wind.wind_speed",
                                                                                "air.pm2_5", "air.pm10"]
    out = await ask(ctx, reading="rain")
    assert "rainfall.rain" in out["error"] and "use days" in out["error"]
    out = await ask(ctx, reading="soil moisture")
    assert "isn't a reading" in out["error"] and "pressure.relative" in out["names"]
    assert "needs both the weather station and the air sensor" in (await ask(Context(eco=eco, tz=TZ), reading="air.pm2_5"))["error"]
    await eco.close()


async def test_a_period_that_isnt_whole_days_points_to_the_tools_that_read_one(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    for period in ("today", "last 5 hours", "this morning"):
        out = await ask(ctx, period=period)
        assert "use summary or readings" in out["error"], period
    assert "has a time" in (await ask(ctx, start="2026-09-01 10:00"))["error"]
    assert "use forecast" in (await ask(ctx, period="next week"))["error"]
    out = await ask(ctx, period="whenever")
    assert "isn't a period" in out["error"] and "on record, all time, ever (from the first reading held)" in out["forms"]
    await eco.close()


async def test_on_record_starts_where_both_the_reading_and_the_rain_are_held(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, period="on record")
    first, last = days_of(out)
    assert out["verdict"] and (last - first).days > 500 and out["period"]["text"].startswith("on record: ")
    await eco.close()


# ---------- the chart ----------
async def test_a_chart_when_asked_carries_the_call_that_draws_it_again(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, reading="pressure", chart=True)
    (chart,) = out["charts"]
    assert out["chart_note"] == CHART_NOTE and chart["title"] == "Pressure, Rain" and chart["source"] == "Ecowitt weather station"
    assert errors(without_drawn_by(chart)) == []
    assert chart["drawn_by"] == {"tool": "relate", "arguments": {"reading": "pressure.relative", "period": "last 90 days", "chart": True},
                                 "periods": [{"period": "1m", "label": "1M"}, {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"}]}
    assert await replays(ctx, chart) == out                                                # the same call again: the same answer
    for each in chart["drawn_by"]["periods"]:
        again = await replays(ctx, chart, period=each["period"])
        assert "error" not in again and again["period"]["asked"] == each["period"], each
        (redrawn,) = again["charts"]
        assert redrawn["title"] == chart["title"] and redrawn["period"] == [str(d) for d in days_of(again)]
        assert redrawn["drawn_by"] == {**chart["drawn_by"], "arguments": {**chart["drawn_by"]["arguments"], "period": each["period"]}}
    await eco.close()


async def test_a_chart_of_dates_is_drawn_again_from_those_dates(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    yesterday = ctx.now().date() - timedelta(days=1)
    first, last = yesterday - timedelta(days=40), yesterday - timedelta(days=10)
    out = await ask(ctx, reading="humidity", start=str(first), end=str(last), chart=True)
    (chart,) = out["charts"]
    assert chart["drawn_by"]["arguments"] == {"reading": "outdoor.humidity", "period": f"{first} to {last}", "chart": True}
    again = await replays(ctx, chart)
    assert again["charts"] == out["charts"] and again["findings"] == out["findings"] and again["period"]["text"] == out["period"]["text"]
    await eco.close()


async def test_the_air_against_rain_is_drawn_with_its_zones_and_drawn_again_over_each_period(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, reading="pm2.5", chart=True)
    (chart,) = out["charts"]
    assert chart["title"] == "PM2.5, Rain" and chart["source"] == "Ecowitt weather station and AirGradient sensor"
    assert chart["panels"][0]["zones"] == [9.0, 55.4] and errors(without_drawn_by(chart)) == []
    assert chart["drawn_by"]["arguments"] == {"reading": "air.pm2_5", "period": "last 60 days", "chart": True}
    assert await replays(ctx, chart) == out
    for each in chart["drawn_by"]["periods"]:
        again = await replays(ctx, chart, period=each["period"])
        assert "error" not in again and [c["title"] for c in again["charts"]] == ["PM2.5, Rain"], each
    await eco.close()


async def test_a_chart_is_sent_in_the_points_the_request_asks_for_and_only_when_the_arguments_ask(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    small = await ask(ctx, {"chart_points": 20}, reading="humidity", chart=True)
    lines = [s for p in small["charts"][0]["panels"] for s in p["series"] if s["kind"] in ("line", "band")]
    assert lines and all(len(s["points"]) <= 20 for s in lines)
    (made,) = relate.tools(ctx)
    asked = json.loads(await Tools([made.tool()]).call("relate", "{}", Turn(text="plot pressure against rain", chart_asked=True)))
    assert "charts" not in asked                                     # the words of the question never reach it: only `chart` draws
    await eco.close()
