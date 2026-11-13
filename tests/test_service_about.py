"""The `about` tool (lib/service/about.py): what the service measures and doesn't, how far back each kind of history goes, the alerts
there are with their thresholds; the lists the bot's prompt and Help had, the same."""

import json
import time
from datetime import timedelta
from types import SimpleNamespace as NS

from lib import templates
from lib.airgradient import AirGradient
from lib.alerts import forecast as forecast_alert, menu, particulates, weather
from lib.ecowitt.glance import UVI_ALERT
from lib.service import about as ab
from lib.service.about import AboutTool
from lib.service.common import request_meta
from lib.service.context import Context
from lib.service.names import AIR_DEVICE, STATION
from lib.storage.catalogue import BOT
from lib.storage.coverage import merge
from lib.timeutil import day_bounds, day_text, local_date
from tests.fakes import TZ, air_transport, archived_station, config

EVERY = ["Ecowitt", "AirGradient", "Pollen", "Forecast"]


async def call(ctx: Context, args=None) -> dict:
    return json.loads(await AboutTool(ctx).tool().call(args or {}))


async def world(tmp_path, archived_cache, *, air=True, others=True):
    """A Context over the archived station, the fake air sensor's cloud beside it in the same file, and (others) stand-ins for the
    forecast and the pollen sources (about only asks whether they are there)."""
    eco, _ = await archived_station(tmp_path, archived_cache)
    sensor = None
    if air:
        transport, _ = air_transport()
        sensor = AirGradient(config(tmp_path), transport=transport, storage=eco.storage, owned=False)
    return await Context.open(eco=eco, air=sensor, forecast=NS() if others else None, pollen=NS() if others else None)


async def close(ctx: Context):
    if ctx.air:
        await ctx.air.close()
    await ctx.eco.close()


# ---------- the tool ----------
def test_the_description_says_what_it_answers_and_that_alerts_cant_be_added_by_asking():
    d = ab.DESCRIPTION
    assert len(d) < 1500
    for said in ("what can you do", "lightning", "how far back", "what alerts are there", "{} (no arguments)",
                 "can't be added or changed by asking", "current (now)", "summary (a period)", "history", "alerts_note", "lines"):
        assert said in d, said
    (tool,) = ab.tools(Context())
    assert tool.name == "about" and tool.parameters == {"type": "object", "properties": {}}
    assert tool.annotations == {"readOnlyHint": True, "openWorldHint": False}


async def test_arguments_are_refused_with_how_to_call_it():
    got = json.loads(await AboutTool(Context()).tool().call({"topic": "alerts"}))
    assert got == {"error": "about takes no arguments, not 'topic': call it with {}.", "arguments": []}


def test_the_lists_are_the_ones_the_bots_prompt_menu_and_help_had():
    assert "lightning" in ab.NEVER_AVAILABLE and set(ab.WITHOUT) == {"Pollen", "Forecast"} and "gusts" in ab.ALERT_WORDS   # (the old prompt's lists, kept here)
    assert ab.TYPES == {kind: v for kind, v in menu.TYPES.items() if v[1] != "Irrigation"}   # (irrigation: a server of its own)
    assert ab.MEASURES == templates.MEASURES
    for sources in (EVERY, ["Ecowitt"], ["AirGradient", "Pollen"], []):
        assert "\n".join(ab.measure_lines(sources)) == templates.capabilities_text(sources)   # the Help list, word for word


# ---------- what it measures ----------
async def test_what_it_measures_and_doesnt_for_the_sources_there_are(tmp_path, archived_cache):
    ctx = await world(tmp_path, archived_cache)
    got = await call(ctx)
    assert "period" not in got and "as_of" not in got and "fresh" not in got            # no figures of the weather, nothing to call again
    m = got["measures"]
    assert list(m) == [STATION, AIR_DEVICE, "forecast", "pollen"]
    assert m[STATION]["text"].startswith("Weather station (Ecowitt): outdoor and indoor temperature")
    assert m[STATION]["readings"]["outdoor.temperature"] == "Outdoor temperature (°C)" and m[AIR_DEVICE]["readings"]["air.voc_index"] == "VOC index"
    assert set(m[STATION]["readings"]) | set(m[AIR_DEVICE]["readings"]) == set(ctx.readings().names)   # the readings the file holds
    assert got["not_available"] == ab.NEVER_AVAILABLE
    assert list(got["tools"]) == ["current", "summary", "readings", "days", "relate", "scan", "outlook", "forecast", "pollen"]
    assert [a["kind"] for a in got["alerts"]] == list(ab.TYPES)
    assert got["lines"] == templates.capabilities_text(EVERY).splitlines()
    assert json.loads(await AboutTool(ctx).tool().call({}, request_meta({"fetch": False}))) == got   # (it reads only what is held)
    await close(ctx)

    (tmp_path / "alone").mkdir()
    alone = await world(tmp_path / "alone", archived_cache, air=False, others=False)
    got = await call(alone)
    assert list(got["measures"]) == [STATION]
    assert got["not_available"] == [*ab.NEVER_AVAILABLE, *ab.WITHOUT.values(), "air quality"]
    assert [a["kind"] for a in got["alerts"]] == ["rain", "gusts", "uv", "temps"]       # the station's alerts alone
    assert "scan" not in got["tools"] and "forecast" not in got["tools"] and "outlook" in got["tools"]
    await close(alone)


# ---------- the alerts ----------
def test_each_alert_has_its_rule_and_the_thresholds_the_rules_use(monkeypatch):
    rules = {a["kind"]: a for a in ab.alerts(EVERY)}
    assert rules["gusts"]["thresholds"]["gust_kmh"] == weather.GUST_ALERT_KMH and "over 40 km/h" in rules["gusts"]["rule"]
    assert rules["uv"]["thresholds"] == {"uv_index": UVI_ALERT, "readings_in_a_row": 2, "rearm_minutes": weather.UV_REARM_SECONDS // 60}
    assert rules["temps"]["thresholds"]["margin_c"] == weather.CROSS_MARGIN
    p = rules["particulates"]["thresholds"]
    assert (p["mask_pm2_5"], p["mask_pm10"], p["safe_pm2_5"], p["safe_pm10"]) == (particulates.MASK_PM25, particulates.MASK_PM10,
                                                                                 particulates.SAFE_PM25, particulates.SAFE_PM10)
    assert "55.5 µg/m³" in rules["particulates"]["rule"] and "AQI 151" in rules["particulates"]["rule"]
    assert rules["forecast"]["thresholds"]["max_change_c"] == forecast_alert.TEMP_CHANGE_C
    assert rules["pollen_and_asthma"]["thresholds"] == {"from_level": "High"} and "from 7:30am" in rules["pollen_and_asthma"]["rule"]
    assert rules["rain"]["thresholds"] == {"dry_minutes": 60, "quiet_hours": "00:00-06:00"}
    assert rules["rain"]["rule"].endswith("None from 12am to 6am: one summary of the rain in those hours after them.")
    assert all(a["what"] == ab.ALERT_WORDS[a["kind"]] and a["name"] == menu.LABELS[a["kind"]] for a in rules.values())
    monkeypatch.setattr(ab, "RAIN_QUIET_HOURS", None)            # ecobot-mcp's settings, as the config sets them
    monkeypatch.setattr(ab, "ALERT_COOLDOWN_MINUTES", 90)
    rules = {a["kind"]: a for a in ab.alerts(EVERY)}
    assert rules["rain"]["thresholds"] == {"dry_minutes": 90, "quiet_hours": None} and "None from" not in rules["rain"]["rule"]
    assert "at most one every 90 minutes" in rules["temps"]["rule"]
    assert "can't be added or changed by asking" in ab.ALERTS_NOTE


# ---------- how far back ----------
async def test_the_history_is_what_the_file_holds_at_each_resolution(tmp_path, archived_cache):
    ctx = await world(tmp_path, archived_cache)
    eco, air = ctx.eco, ctx.air
    now = time.time()
    minutes = {"outdoor": {"temperature": {"unit": "℃", "list": {str(int(now) // 60 * 60 - 60 * k): "12.3" for k in range(5)}}}}
    await eco.storage.write(lambda w: w.put_minutes(eco.mac, minutes, now))
    day = local_date(int(now), TZ) - timedelta(days=3)
    start = day_bounds(day, TZ)[0]
    await air.store.save_day(day, [{"ts": start + 300 * k, "pm2_5": 5.0} for k in range(288)])          # a day at 5 minutes
    older = day - timedelta(days=200)
    await air.store.save_day(older, [{"ts": day_bounds(older, TZ)[0] + 3600 * k, "pm2_5": 5.0} for k in range(24)])   # one of hourly averages
    await air.store.add_minutes([{"ts": int(now) // 60 * 60, "pm2_5": 4.0}], BOT, now)
    got = (await call(ctx))["history"]

    station = got[STATION]
    sensors = list(eco.storage.station(eco.mac).values())
    for res in ("5min", "30min", "4hour", "1day"):
        spans = merge([s for sensor in sensors for s in eco.storage.coverage.of(sensor.id, res)])
        assert station["held"][res] == {"from": local_date(spans[0][0], TZ).isoformat(), "to": local_date(spans[-1][1], TZ).isoformat(),
                                        "days": sum(e - s + 1 for s, e in spans) // 86400}, res
    first_5min = local_date(merge([s for sensor in sensors for s in eco.storage.coverage.of(sensor.id, "5min")])[0][0], TZ)
    minute = local_date(int(now) // 60 * 60 - 240, TZ)
    assert station["held"]["1min"]["from"] == minute.isoformat()
    assert station["text"].startswith(f"Weather station: minute readings from {day_text(minute)} (kept 10 days), 5-minute readings from "
                                      f"{day_text(first_5min)}, 30-minute readings from ")
    assert "daily records from " in station["text"] and ", each 10am to 10am" in station["text"]

    sky = got[AIR_DEVICE]
    assert sky["held"]["5min"] == {"from": day.isoformat(), "to": day.isoformat(), "days": (day_bounds(day, TZ)[1] - start) // 86400}
    assert sky["held"]["1hour"]["from"] == older.isoformat() and "1min" in sky["held"]
    today = local_date(int(now) // 60 * 60, TZ)
    assert sky["text"] == (f"Air sensor: minute readings from {day_text(today)} (kept 10 days), 5-minute readings from {day_text(day)}, "
                           f"hourly averages from {day_text(older)}. The tools use the finest held for each part of a period.")
    await close(ctx)


async def test_a_sensor_with_nothing_held_says_so(tmp_path, archived_cache):
    ctx = await world(tmp_path, archived_cache)
    assert (await call(ctx))["history"][AIR_DEVICE] == {"text": "Air sensor: nothing held yet.", "held": {}}
    await close(ctx)


def test_the_module_says_what_it_offers():
    assert all(hasattr(ab, name) for name in ab.__all__)
