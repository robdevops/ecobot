import json
from datetime import datetime, timedelta

from lib.charts import render
from lib.tools import Turn
from lib.compose import MAX_PANELS, SERIES, Composer, rating_shares
from lib.analysis.pairs import analyse_air
from tests.fakes import TZ, archived_station


class StubAir:
    """The part of AirGradient the composer uses: readings for a period."""
    tz = TZ

    def __init__(self, days=40):
        now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
        self.readings = []
        for i in range(days * 288, 0, -1):                      # 5-minute readings; each day is good, poor or very poor
            when = now - timedelta(minutes=5 * i)
            self.readings.append({"ts": int(when.timestamp()), "pm2_5": [5.0, 30.0, 80.0][when.day % 3], "pm10": 10.0})

    async def rows(self, t0, t1):
        lo, hi = t0.replace(tzinfo=TZ).timestamp(), t1.replace(tzinfo=TZ).timestamp()
        return [r for r in self.readings if lo <= r["ts"] <= hi], 0, []


async def composer(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    return Composer(eco, StubAir()), eco


async def plot(comp, **args):
    turn = Turn()
    out = json.loads(await comp.plot_chart(args, turn))
    return out, turn.charts


async def test_the_composer_offers_the_chart_tool_and_the_two_air_tools(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    assert [t.name for t in comp.tools] == ["plot_chart", "air_link", "air_scan"]
    schema = comp.tools[0].parameters["properties"]["panels"]
    assert schema["maxItems"] == MAX_PANELS and schema["items"]["properties"]["series"]["enum"] == SERIES
    await eco.close()


async def test_a_reading_with_its_zones_over_rain_is_one_stacked_chart(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out, specs = await plot(comp, panels=[{"series": "pm2_5"}, {"series": "rain"}])
    spec = specs[0]
    assert len(specs) == 1 and spec["kind"] == "stack" and spec["title"] == "PM2.5 and Rain" and "chart" in out
    line, rain = spec["panels"]
    assert line["zones"] == [9.0, 55.4] and len(line["series"][0]["x"]) > 20
    assert rain["bars"]["width"] == 6 * 3600 and all(v >= 0 for v in rain["bars"]["y"])      # thirty days: a bar per 6 hours
    assert out["panels"][0]["series"] == "pm2_5" and "total_mm" in out["panels"][1]
    assert render(spec, TZ)[:4] == b"\x89PNG"
    await eco.close()


async def test_the_traffic_light_rating_is_the_share_of_each_days_readings(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out, specs = await plot(comp, panels=[{"series": "pm2_5", "style": "rating"}, {"series": "rain", "style": "bars"}])
    shares = specs[0]["panels"][0]["shares"]
    assert all(abs(g + p + v - 100) < 0.2 for g, p, v in zip(shares["good"], shares["poor"], shares["very poor"]))
    assert max(shares["good"]) == 100 and max(shares["poor"]) == 100 and max(shares["very poor"]) == 100     # whole days of each
    assert out["panels"][0]["style"] == "rating" and set(out["panels"][0]["share_of_time_percent"]) == {"good", "poor", "very poor"}
    assert render(specs[0], TZ)[:4] == b"\x89PNG"
    await eco.close()


def test_the_rating_limits_are_inclusive_and_bars_start_at_local_midnight():
    day = datetime(2026, 9, 1).date()
    base = int(datetime(2026, 9, 1, tzinfo=TZ).timestamp())
    values = {base + i * 1800: v for i, v in enumerate([9.0, 9.1, 55.4, 55.5])}
    shares = rating_shares(values, (9.0, 55.4), TZ, day, day)            # one day: hourly bars, two readings each
    assert shares["width"] == 3600 and shares["x"][0] == base
    assert (shares["good"][0], shares["poor"][0], shares["very poor"][0]) == (50.0, 50.0, 0.0)
    assert (shares["good"][1], shares["poor"][1], shares["very poor"][1]) == (0.0, 50.0, 50.0)


async def test_bad_requests_come_back_as_errors_the_model_can_explain(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    for panels in ([], [{"series": "wind"}] * (MAX_PANELS + 1), [{"series": "nonsense"}], [{"series": "temperature", "style": "rating"}],
                   [{"series": "pm2_5", "style": "bars"}], [{"series": "rain", "style": "line"}]):
        out, specs = await plot(comp, panels=panels)
        assert "error" in out and specs == [], panels
    out, _ = await plot(comp, panels=[{"series": "pm2_5"}], start_date="2026-13-45")
    assert "bad date" in out["error"]
    out, _ = await plot(comp, panels=[{"series": "pm2_5"}], start_date="2026-09-20", end_date="2026-09-10")
    assert "before" in out["error"]
    await eco.close()


async def test_the_last_day_is_yesterday_at_most(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    first, last = comp.period({"end_date": str(datetime.now(TZ).date() + timedelta(days=5))})
    assert last == datetime.now(TZ).date() - timedelta(days=1) and (last - first).days == 29
    await eco.close()


def synthetic(days, air_after_rain, events_every=3):
    """30-minute slots for `days` days: a 3-hour rain spell every few days; the air is `air_after_rain(wet_or_after)`."""
    base = int(datetime(2026, 5, 1, tzinfo=TZ).timestamp())
    rain, air = {}, {}
    for i in range(days * 48):
        t = base + i * 1800
        day, slot = divmod(i, 48)
        rain[t] = 0.5 if day % events_every == 1 and 20 <= slot < 26 else 0.0
        washed = day % events_every == 1 and 20 <= slot < 38                      # during the rain and the 6 hours after
        air[t] = air_after_rain(washed)
    return air, rain


def test_air_that_clears_after_every_rain_is_a_yes_and_air_that_ignores_rain_is_a_no():
    limits = (9.0, 55.4)
    air, rain = synthetic(60, lambda washed: 10.0 if washed else 30.0)
    out = analyse_air(air, rain, limits, TZ, "PM2.5")
    assert out["verdict"].startswith("Yes - rain clears") and out["events"]["cleaner_after"] == out["events"]["count"] >= 15
    assert "PM2.5" in out["findings"][0] and "poor" in out["findings"][0]
    assert out["rank_correlation_daily_rain_vs_air"] is not None and out["days_compared"] == 60
    air, rain = synthetic(60, lambda washed: 20.0)
    assert analyse_air(air, rain, limits, TZ, "PM2.5")["verdict"].startswith("No")
    air, rain = synthetic(60, lambda washed: 40.0 if washed else 20.0)                # the air is worse around rain
    assert analyse_air(air, rain, limits, TZ, "PM2.5")["verdict"].startswith("Yes, the other way")


def test_too_few_rain_events_or_no_overlap_says_so():
    air, rain = synthetic(7, lambda washed: 10.0 if washed else 30.0)              # two events in a week
    assert analyse_air(air, rain, (9.0, 55.4), TZ, "PM2.5")["verdict"].startswith("Not enough")
    assert analyse_air({}, rain, (9.0, 55.4), TZ, "PM2.5") == {}


async def test_air_link_reports_a_verdict_and_can_chart_the_reading_over_rain(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    turn = Turn()
    out = json.loads(await comp.air_link({"chart": True}, turn))
    specs = turn.charts
    assert out["verdict"] and out["findings"] and "how_to_read" in out and out["metric"].startswith("PM2.5")
    assert len(specs) == 1 and [p["label"] for p in specs[0]["panels"]] == ["PM2.5", "Rain"] and specs[0]["panels"][0]["zones"] == [9.0, 55.4]
    assert "error" in json.loads(await comp.air_link({"metric": "co2"}))
    await eco.close()
