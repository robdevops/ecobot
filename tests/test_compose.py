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
            self.readings.append({"ts": int(when.timestamp()), "pm2_5": [5.0, 30.0, 80.0][when.day % 3], "pm10": 10.0,
                                  "air_humidity": 50.0 + when.hour, "air_temperature": 12.0 + when.hour / 3})

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
    assert len(specs) == 1 and spec.title == "PM2.5, Rain" and "chart" in out
    (line,) = spec.panels                                                                    # the rain is behind the line
    assert line.zones == (9.0, 55.4) and len(line.lines[0].x) > 20
    assert line.bars.width == 6 * 3600 and all(v >= 0 for v in line.bars.y)                  # thirty days: a bar per 6 hours
    assert out["panels"][0]["series"] == "pm2_5" and "total_mm" in out["panels"][1]
    assert render(spec, TZ)[:4] == b"\x89PNG"
    await eco.close()


async def test_the_air_panels_share_one_load_and_a_weather_chart_asks_the_sensor_nothing(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    loads, rows = [], comp.air.rows

    async def counted(t0, t1):
        loads.append((t0, t1))
        return await rows(t0, t1)
    comp.air.rows = counted
    out, _ = await plot(comp, panels=[{"series": "pm2_5"}, {"series": "pm10", "style": "rating"}, {"series": "rain"}])
    assert "chart" in out and len(loads) == 1
    out, _ = await plot(comp, panels=[{"series": "temperature"}])
    assert "chart" in out and len(loads) == 1
    await eco.close()


async def test_the_traffic_light_rating_is_the_share_of_each_days_readings(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out, specs = await plot(comp, panels=[{"series": "pm2_5", "style": "rating"}, {"series": "rain", "style": "bars"}])
    shares = specs[0].panels[0].shares
    assert len(specs[0].panels) == 2 and specs[0].panels[1].bars                              # no line to sit behind: rain has a panel
    assert all(abs(g + p + v - 100) < 0.2 for g, p, v in zip(shares.good, shares.poor, shares.very_poor))
    assert max(shares.good) == 100 and max(shares.poor) == 100 and max(shares.very_poor) == 100     # whole days of each
    assert out["panels"][0]["style"] == "rating" and set(out["panels"][0]["share_of_time_percent"]) == {"good", "poor", "very poor"}
    assert render(specs[0], TZ)[:4] == b"\x89PNG"
    await eco.close()


def test_the_rating_limits_are_inclusive_and_bars_start_at_local_midnight():
    day = datetime(2026, 9, 1).date()
    base = int(datetime(2026, 9, 1, tzinfo=TZ).timestamp())
    values = {base + i * 1800: v for i, v in enumerate([9.0, 9.1, 55.4, 55.5])}
    shares = rating_shares(values, (9.0, 55.4), TZ, day, day, "PM2.5 rating")            # one day: hourly bars, two readings each
    assert shares.width == 3600 and shares.x[0] == base
    assert (shares.good[0], shares.poor[0], shares.very_poor[0]) == (50.0, 50.0, 0.0)
    assert (shares.good[1], shares.poor[1], shares.very_poor[1]) == (0.0, 50.0, 50.0)


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


def test_air_that_reads_zero_when_dry_and_events_that_change_nothing_are_counted_as_such():
    air, rain = synthetic(60, lambda washed: 0.0)                     # a clean sensor: zero before and after every rain
    out = analyse_air(air, rain, (9.0, 55.4), TZ, "PM2.5")
    assert out["events"]["cleaner_after"] == out["events"]["worse_after"] == 0 and out["events"]["count"] >= 15
    assert out["findings"][0].endswith("when dry (good)") and f"{out['events']['count']} about the same" in out["findings"][1]


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
    assert len(specs) == 1 and [p.label for p in specs[0].panels] == ["PM2.5"] and specs[0].panels[0].zones == (9.0, 55.4)
    assert specs[0].title == "PM2.5, Rain" and specs[0].panels[0].bars                     # the rain is behind the reading
    assert "error" in json.loads(await comp.air_link({"metric": "co2"}))
    await eco.close()


async def test_two_series_can_share_one_panel_with_a_scale_each(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out, specs = await plot(comp, panels=[{"series": "temperature"}, {"series": "pm2_5"}], two_axes=True)
    assert len(specs) == 1 and [p.label for p in specs[0].panels] == ["Temperature and PM2.5"]
    assert [s.right for s in specs[0].panels[0].lines] == [False, False, True] or [s.right for s in specs[0].panels[0].lines].count(True) >= 1
    assert render(specs[0], TZ)[:4] == b"\x89PNG" and len(out["panels"]) == 2          # both series are still described
    out, specs = await plot(comp, panels=[{"series": "temperature"}, {"series": "rain"}], two_axes=True)
    assert "two_axes needs line series" in out["error"] and specs == []
    out, specs = await plot(comp, panels=[{"series": "temperature"}], two_axes=True)
    assert "error" in out and specs == []
    await eco.close()


async def test_the_air_sensors_own_temperature_goes_over_the_stations_on_one_axis(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out, specs = await plot(comp, panels=[{"series": "air_temperature"}, {"series": "temperature"}], same_axis=True)
    assert len(specs[0].panels) == 1 and len(out["panels"]) == 2
    panel = specs[0].panels[0]
    assert [s.label for s in panel.lines] == ["AirGradient temperature", "Ecowitt temperature"] and panel.unit == "°C" and not any(s.right for s in panel.lines)
    assert [s.reading for s in panel.lines] == ["air_temperature", "temperature"] and render(specs[0], TZ)[:4] == b"\x89PNG"
    out, specs = await plot(comp, panels=[{"series": "air_humidity"}, {"series": "temperature"}], same_axis=True)
    assert "same unit" in out["error"] and specs == []                                    # % and °C: two_axes is for that
    out, specs = await plot(comp, panels=[{"series": "air_humidity"}, {"series": "temperature"}], two_axes=True)
    assert [s.right for s in specs[0].panels[0].lines] == [False, True]
    out, specs = await plot(comp, panels=[{"series": "air_temperature"}, {"series": "temperature"}, {"series": "air_humidity"}, {"series": "humidity"}],
                            two_axes=True)                                                # two pairs: a unit to each axis
    assert [(s.label, s.right) for s in specs[0].panels[0].lines] == [
        ("AirGradient temperature (°C)", False), ("Ecowitt temperature (°C)", False), ("AirGradient humidity (%)", True), ("Ecowitt humidity (%)", True)]
    assert render(specs[0], TZ)[:4] == b"\x89PNG"
    out, specs = await plot(comp, panels=[{"series": "air_temperature"}, {"series": "temperature"}, {"series": "air_humidity"}, {"series": "humidity"}],
                            two_axes=True, invert_second_axis=True)                       # the right-hand axis upside down
    assert specs[0].panels[0].label == "Temperature and Humidity"                      # not every panel's name in a row
    assert specs[0].panels[0].flip_right and render(specs[0], TZ)[:4] == b"\x89PNG"
    assert not (await plot(comp, panels=[{"series": "air_temperature"}, {"series": "humidity"}], two_axes=True))[1][0].panels[0].flip_right
    out, specs = await plot(comp, panels=[{"series": "air_temperature"}, {"series": "temperature"}], invert_second_axis=True)
    assert "needs two_axes" in out["error"] and specs == []
    out, specs = await plot(comp, panels=[{"series": "air_temperature"}, {"series": "temperature"}], two_axes=True, same_axis=True)
    assert "can't both be set" in out["error"]
    out, specs = await plot(comp, panels=[{"series": "air_humidity", "style": "rating"}])
    assert "error" in out and specs == []                                                 # no traffic light for a humidity
    await eco.close()


async def test_the_tools_offer_the_air_sensors_own_temperature_and_humidity_only_to_a_question_that_asks(tmp_path, archived_cache):
    from lib.tools import Tools
    comp, eco = await composer(tmp_path, archived_cache)
    tools = Tools(comp.tools)
    plain = tools.schemas_for(Turn())
    assert plain is tools.schemas and "air_humidity" not in json.dumps(plain) and "same_axis" not in json.dumps(plain)
    asked = {s["function"]["name"]: s["function"] for s in tools.schemas_for(Turn(air_extra=True))}
    series = asked["plot_chart"]["parameters"]["properties"]["panels"]["items"]["properties"]["series"]["enum"]
    assert series == [*SERIES, "air_temperature", "air_humidity"] and "same_axis" in asked["plot_chart"]["parameters"]["properties"]
    assert [s["function"]["name"] for s in tools.schemas_for(Turn(air_extra=True))] == [s["function"]["name"] for s in plain]   # the same tools, in order
    await eco.close()


async def test_a_composed_air_series_takes_its_range_from_the_readings_the_bot_recorded(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    air = StubAir(days=2)
    for r in air.readings[-12:]:                                      # the last hour was recorded by the bot, with a spread
        r["_range"] = {"pm2_5": (r["pm2_5"] - 2.0, r["pm2_5"] + 3.0)}
    comp.air = air
    first = datetime.now(TZ).date() - timedelta(days=2)
    series, _ = await comp.air_series(["pm2_5"], first, datetime.now(TZ).date())
    values, lows, highs = series["pm2_5"]
    spread = [t for t in values if highs[t] - lows[t] >= 5.0]
    assert spread and all(highs[t] - lows[t] >= 5.0 for t in spread[-1:])
    assert any(highs[t] == lows[t] == values[t] for t in values)       # a slot with no recorded range is a single value
    await eco.close()
