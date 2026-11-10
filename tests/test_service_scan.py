"""The service's `scan` tool (lib/service/scan.py): each air reading against every weather reading over whole days, with the findings of
the bot's air_scan, at most 400 days, and a chart of the strongest pair that says how to draw it again."""

import json
import time
from datetime import date, datetime, timedelta

from lib.compose import Composer
from lib.service import scan
from lib.service.common import CHART_NOTE, read_period
from lib.service.context import Context
from lib.tools import Turn
from tests.fakes import TZ, archived_station, pressure
from tests.test_chart_json import errors
from tests.test_service_relate import HeldStubAir, days_of


class PressureAir(HeldStubAir):
    """An air sensor whose PM2.5 follows the station's pressure (and whose PM10 changes by the day): a relationship that stands out,
    so the scan has a strongest pair to draw."""

    def __init__(self, days=60):
        super().__init__(days=0)
        now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
        for i in range(days * 288, 0, -1):
            when = now - timedelta(minutes=5 * i)
            ts = int(when.timestamp())
            self.readings.append({"ts": ts, "pm2_5": 2 * (pressure(ts) - 1000), "pm10": 10.0 + when.day % 3})


async def world(tmp_path, archived_cache, air=None):
    eco, _ = await archived_station(tmp_path, archived_cache)
    sensor, now = air or PressureAir(), int(time.time())
    return Context(eco=eco, air=sensor, composer=Composer(eco, sensor), tz=TZ, clock=lambda: now), eco


async def ask(ctx, meta: dict | None = None, **args) -> dict:
    (made,) = scan.tools(ctx)
    return json.loads(await made.call(args, meta))


async def replays(ctx, chart: dict, **swap) -> dict:
    return await ask(ctx, **{**chart["drawn_by"]["arguments"], **swap})


# ---------- what it takes ----------
async def test_it_takes_an_air_reading_or_all_whole_days_and_a_chart(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    (made,) = scan.tools(ctx)
    props = made.parameters["properties"]
    assert made.name == "scan" and set(props) == {"reading", "period", "start", "end", "chart"} and "required" not in made.parameters
    assert "Whole days only, up to yesterday" in props["period"]["description"] and "at most 400 days" in props["period"]["description"]
    assert all(name in props["reading"]["description"] for name in ("air.pm2_5", "air.co2", "air.voc_index", "all of them"))
    assert len(made.description) < 1500 and all(f"({tool})" in made.description for tool in ("relate", "summary", "current"))
    assert "{reading: \"co2\"}" in made.description and "At most 400 days" in made.description
    assert made.annotations == {"readOnlyHint": True, "openWorldHint": True}
    await eco.close()


# ---------- the findings ----------
async def test_with_nothing_named_it_scans_every_air_reading_over_the_last_90_days_as_air_scan_does(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx)
    assert list(out)[:7] == ["period", "as_of", "fresh", "reading", "verdict", "how_to_read", "findings"]
    assert out["reading"] == "all" and out["period"]["asked"] == "last 90 days" and out["period"]["cut"].endswith("today isn't over")
    assert out["verdict"] == "Yes - 1 relationship stands out" and "pressure" in out["findings"][0]
    first, last = days_of(out)
    assert last == ctx.now().date() - timedelta(days=1) and (last - first).days == 89
    before = json.loads(await ctx.composer.air_scan({"start_date": str(first), "end_date": str(last)}, Turn()))
    assert {k: v for k, v in out.items() if k in before and k != "period"} == {k: v for k, v in before.items() if k not in ("period", "chart")}
    assert "charts" not in out and "chart" not in out   # a chart only when asked
    await eco.close()


async def test_the_words_people_use_name_the_air_reading(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    for words, name in (("pm2.5", "air.pm2_5"), ("CO2", "air.co2"), ("voc", "air.voc_index"), ("air.nox_index", "air.nox_index"),
                        ("air", "all"), ("airgradient", "all"), ("everything", "all"), (["all"], "all"), ("", "all")):
        out = await ask(ctx, reading=words, period="last 2 weeks")
        assert out["reading"] == name and out["verdict"], words
    await eco.close()


async def test_a_reading_with_nothing_to_scan_says_so(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)   # (the stand-in measures no CO2)
    out = await ask(ctx, reading="co2", chart=True)
    assert out["verdict"] == "Not enough data to say" and out["findings"] and "charts" not in out
    await eco.close()


async def test_what_it_cant_scan_is_refused_with_what_it_can(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    readings = ["all", "air.pm2_5", "air.pm10", "air.co2", "air.voc_index", "air.nox_index"]
    out = await ask(ctx, reading="pm1")
    assert "isn't an air reading this tool scans" in out["error"] and out["readings"] == readings
    out = await ask(ctx, reading="temperature")
    assert "use relate" in out["error"] and out["readings"] == readings
    out = await ask(ctx, reading="pm2.5 and pm10")
    assert "names 2 air readings: send one, or all" in out["error"]
    assert "use summary or readings" in (await ask(ctx, period="today"))["error"]
    assert "needs both the weather station and the air sensor" in (await ask(Context(eco=eco, tz=TZ)))["error"]
    await eco.close()


def test_a_period_longer_than_400_days_is_cut_to_its_latest_and_says_so():
    now = datetime(2026, 11, 10, 14, 5, tzinfo=TZ)
    two = read_period({"period": "2y"}, tz=TZ, now=now, default="last 90 days", whole_days=True)
    cut, note = scan.latest_days(two, TZ)
    assert (cut.first_day, cut.last_day) == (date(2025, 10, 6), date(2026, 11, 9)) and cut.asked == "2y" and cut.cut == two.cut
    assert cut.start == datetime(2025, 10, 6, tzinfo=TZ) and cut.text == "its last 400 days: Mon 6 Oct 2025 – Mon 9 Nov 2026"
    assert note == ("Only the last 400 days can be scanned in one call: this covers Mon 6 Oct 2025 – Mon 9 Nov 2026, not all of the last "
                    "2 years: Sun 10 Nov 2024 – Mon 9 Nov 2026.")
    one = read_period({"period": "1y"}, tz=TZ, now=now, default="last 90 days", whole_days=True)
    assert scan.latest_days(one, TZ) == (one, None)


async def test_a_long_period_is_scanned_over_its_latest_400_days_and_drawn_again_over_them(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    yesterday = ctx.now().date() - timedelta(days=1)
    long = await ask(ctx, start=str(yesterday - timedelta(days=500)), chart=True)
    assert days_of(long) == (yesterday - timedelta(days=399), yesterday) and long["period"]["text"].startswith("its last 400 days: ")
    assert long["note_period"].startswith("Only the last 400 days can be scanned in one call")
    (chart,) = long["charts"]
    assert chart["drawn_by"]["arguments"]["period"] == f"{yesterday - timedelta(days=399)} to {yesterday}"   # the days it scanned
    again = await replays(ctx, chart)
    assert again["charts"] == long["charts"] and again["findings"] == long["findings"] and "note_period" not in again
    await eco.close()


async def test_a_request_that_may_not_fetch_reads_the_air_from_the_file(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    fetched = await ask(ctx, reading="pm2.5", period="last 30 days")
    held = await ask(ctx, {"fetch": False}, reading="pm2.5", period="last 30 days")
    assert ctx.air.fetched == 1 and held == fetched
    await eco.close()


# ---------- the chart ----------
async def test_a_chart_of_the_strongest_pair_carries_the_call_that_draws_it_again(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache)
    out = await ask(ctx, chart=True)
    (chart,) = out["charts"]
    assert out["chart_note"] == CHART_NOTE and chart["title"] == "PM2.5, Pressure" and errors(chart) == []
    assert chart["source"] == "Ecowitt weather station and AirGradient sensor"
    assert chart["drawn_by"] == {"tool": "scan", "arguments": {"reading": "all", "period": "last 90 days", "chart": True},
                                 "periods": [{"period": "1m", "label": "1M"}, {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"}]}
    assert await replays(ctx, chart) == out
    for each in chart["drawn_by"]["periods"]:
        again = await replays(ctx, chart, period=each["period"])
        assert "error" not in again and again["period"]["asked"] == each["period"], each
        (redrawn,) = again["charts"]
        assert redrawn["title"] == chart["title"] and redrawn["period"] == [str(d) for d in days_of(again)]
    small = await ask(ctx, {"chart_points": 30}, chart=True)
    assert all(len(s["points"]) <= 30 for p in small["charts"][0]["panels"] for s in p["series"] if s["kind"] in ("line", "band"))
    await eco.close()


async def test_no_chart_when_nothing_stands_out(tmp_path, archived_cache):
    ctx, eco = await world(tmp_path, archived_cache, air=HeldStubAir())   # PM2.5 by the day, nothing to do with the weather
    out = await ask(ctx, reading="pm2.5", chart=True)
    assert out["verdict"].startswith("Nothing stands out") and "charts" not in out and "chart_note" not in out
    await eco.close()
