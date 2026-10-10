"""Draw the charts the bot would draw, from your two SQLite caches, and save each chart spec (JSON) and picture (PNG).
Strictly offline: no API keys, no network (any request raises), and your caches are never touched (they are copied to a
temporary folder first). A question the cache cannot answer is reported as "not cached" and skipped.

    python scripts/dump_specs.py --data-dir . --tz Australia/Melbourne --out chart_dump

Run it before and after a change to the chart code, and compare the two folders (the JSON files hold every point drawn).
"""

import asyncio
import dataclasses
import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from _common import parser  # noqa: E402  (also puts the repo on sys.path)
from lib.config import data_dir  # noqa: E402

from lib.airgradient import AirGradient  # noqa: E402
from lib.charts import render  # noqa: E402
from lib.compose import Composer  # noqa: E402
from lib.config import Config  # noqa: E402
from lib.ecowitt import Ecowitt  # noqa: E402
from lib.tools import Turn  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


class Offline(httpx.AsyncBaseTransport):
    """Every request fails, so nothing can leave this machine; the count says how many questions wanted the network."""
    calls = 0

    async def handle_async_request(self, request):
        Offline.calls += 1
        raise httpx.ConnectError("offline: dump_specs never uses the network")


def questions(now: datetime) -> list[tuple[str, str, dict, dict]]:
    """(name, tool, arguments, what the person's words set on the Turn)."""
    # The end of the last day the caches treat as final (a day is final an hour after it ends): a still-settling day would need the network
    end = datetime.combine((now - timedelta(hours=25)).date(), datetime.min.time()) + timedelta(days=1) - timedelta(seconds=1)

    def back(days: float) -> dict:
        return {"start_date": (end - timedelta(days=days) + timedelta(seconds=1)).strftime(FMT), "end_date": end.strftime(FMT)}

    def days(n: int) -> dict:  # n whole days ending on that last final day, for the tools that take dates
        return {"start_date": (end.date() - timedelta(days=n - 1)).strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d")}

    eco = lambda name, tool, **a: (name, tool, a, {})
    return [
        eco("eco_temp_1d", "weather_history", chart=True, groups="outdoor", **back(1)),
        eco("eco_temp_2d_indoor_outdoor", "weather_history", chart=True, groups="outdoor,indoor", **back(2)),
        eco("eco_temp_7d", "weather_history", chart=True, groups="outdoor,indoor", **back(7)),
        eco("eco_temp_30d", "weather_history", chart=True, groups="outdoor,indoor", **back(30)),
        eco("eco_temp_90d", "weather_history", chart=True, groups="outdoor", **back(90)),
        eco("eco_temp_400d", "weather_history", chart=True, groups="outdoor", **back(400)),
        eco("eco_humidity_7d", "weather_history", chart=True, groups="outdoor", chart_field="humidity", **back(7)),
        eco("eco_pressure_7d", "weather_history", chart=True, groups="pressure", chart_field="relative", **back(7)),
        eco("eco_pressure_90d", "weather_history", chart=True, groups="pressure", chart_field="relative", **back(90)),
        eco("eco_wind_7d_compass", "weather_history", chart=True, groups="wind", chart_field="wind_gust", **back(7)),
        eco("eco_wind_90d_compass", "weather_history", chart=True, groups="wind", chart_field="wind_gust", **back(90)),
        eco("eco_temp_30d_average", "weather_history", chart=True, groups="outdoor", average=True, **back(30)),
        eco("eco_stack_temp_rain_14d", "weather_history", chart=True, chart_fields=["temperature", "rain"], **back(14)),
        eco("eco_stack_temp_hum_pres_14d", "weather_history", chart=True,
            chart_fields=["temperature", "humidity", "pressure"], **back(14)),
        eco("eco_stack_wind_rain_30d", "weather_history", chart=True, chart_fields=["wind", "rain"], **back(30)),
        eco("eco_link_pressure_90d", "weather_link", chart=True, driver="pressure", **days(90)),
        eco("eco_link_humidity_30d", "weather_link", chart=True, driver="humidity", **days(30)),
        eco("eco_link_wind_400d", "weather_link", chart=True, driver="wind", **days(400)),
        ("air_pm25_1d", "air_quality", {"chart": True, "metrics": ["pm2_5"], **back(1)}, {}),
        ("air_pm25_7d", "air_quality", {"chart": True, "metrics": ["pm2_5"], **back(7)}, {}),
        ("air_pm25_30d", "air_quality", {"chart": True, "metrics": ["pm2_5"], **back(30)}, {}),
        ("air_pm25_200d_mixed_resolution", "air_quality", {"chart": True, "metrics": ["pm2_5"], **back(200)}, {}),
        ("air_pm25_400d", "air_quality", {"chart": True, "metrics": ["pm2_5"], **back(360)}, {}),
        ("air_all_7d", "air_quality", {"chart": True, "metrics": ["pm2_5", "pm10", "pm1", "co2", "voc_index", "nox_index"], **back(7)}, {}),
        ("air_all_90d", "air_quality", {"chart": True, "metrics": ["pm2_5", "pm10", "pm1", "co2", "voc_index", "nox_index"], **back(90)}, {}),
        ("air_co2_voc_14d", "air_quality", {"chart": True, "metrics": ["co2", "voc_index"], **back(14)}, {}),
        ("air_pm_group_14d", "air_quality", {"chart": True, "metrics": ["pm1", "pm2_5", "pm10"], **back(14)}, {}),
        ("plot_pm25_over_rain_30d", "plot_chart", {"panels": [{"series": "pm2_5"}, {"series": "rain"}], **days(30)}, {}),
        ("plot_rating_over_rain_30d", "plot_chart",
         {"panels": [{"series": "pm2_5", "style": "rating"}, {"series": "rain"}], **days(30)}, {}),
        ("plot_temp_pm25_humidity_60d", "plot_chart",
         {"panels": [{"series": "temperature"}, {"series": "pm2_5"}, {"series": "humidity"}], **days(60)}, {}),
        ("plot_wind_pm10_90d", "plot_chart", {"panels": [{"series": "wind"}, {"series": "pm10"}], **days(90)}, {}),
        ("air_link_pm25_60d", "air_link", {"chart": True, **days(60)}, {}),
        ("air_scan_90d", "air_scan", {"chart": True, **days(90)}, {}),
    ]


def copy_db(src: Path, dest: Path):
    """A consistent copy (includes anything still in the write-ahead log), opened read-only."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    source.backup(target)
    target.close()
    source.close()


async def main():
    logging.disable(logging.WARNING)  # the fetchers log every refused request
    ap = parser(__doc__)
    ap.add_argument("--data-dir", default=str(data_dir()), help="folder (default DATA_DIR) holding ecowitt_cache.sqlite and airgradient_cache.sqlite")
    ap.add_argument("--tz", default="Australia/Melbourne")
    ap.add_argument("--out", default="chart_dump")
    args = ap.parse_args()
    data, tz, out = Path(args.data_dir), ZoneInfo(args.tz), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        eco_path, air_path = Path(tmp) / "ecowitt.sqlite", Path(tmp) / "airgradient.sqlite"
        have_eco, have_air = (data / "ecowitt_cache.sqlite").exists(), (data / "airgradient_cache.sqlite").exists()
        if have_eco:
            copy_db(data / "ecowitt_cache.sqlite", eco_path)
        if have_air:
            copy_db(data / "airgradient_cache.sqlite", air_path)
        loc = ""
        if have_air:
            db = sqlite3.connect(f"file:{air_path}?mode=ro", uri=True)
            loc = (db.execute("SELECT loc FROM days LIMIT 1").fetchone() or [""])[0]
            db.close()
        cfg = Config(telegram_token="", xai_api_key="", xai_base_url="", xai_model="", tz=tz, ecowitt_api_key="offline",
                     ecowitt_app_key="offline", airgradient_token="offline", airgradient_location=str(loc or "offline"),
                     airgradient_dashboard="", state_path=Path(tmp) / "state.json", cache_path=eco_path, air_cache_path=air_path)
        eco = air = None
        if have_eco:
            eco = Ecowitt(cfg, transport=Offline())
            db = sqlite3.connect(f"file:{eco_path}?mode=ro", uri=True)
            eco.mac = (db.execute("SELECT DISTINCT mac FROM coverage").fetchone() or [""])[0]
            db.close()
        if have_air:
            air = AirGradient(cfg, transport=Offline())
        tools = {t.name: t for s in (eco, air) if s for t in s.tools}
        if eco and air:
            tools.update({t.name: t for t in Composer(eco, air).tools})

        now = datetime.now(tz)
        print(f"as of {now:%Y-%m-%d %H:%M %Z}; caches: ecowitt={'yes' if have_eco else 'NO'} airgradient={'yes' if have_air else 'NO'}")
        for name, tool, tool_args, _ in questions(now):
            if tool not in tools:
                print(f"  skip   {name}: no {tool} tool (a cache is missing)")
                continue
            turn = Turn(chart_asked=True, chart_fields=tool_args.get("chart_fields", []),
                        chart_field=tool_args.get("chart_field"), average_asked=bool(tool_args.get("average")))
            before = Offline.calls
            try:
                result = await tools[tool].handler(tool_args, turn)
            except httpx.ConnectError:
                print(f"  cache  {name}: not fully cached (it would need the network)")
                continue
            except Exception as e:  # keep going: one broken chart must not hide the rest
                print(f"  ERROR  {name}: {type(e).__name__}: {e}")
                continue
            note = f" ({Offline.calls - before} request(s) refused: part of the range is not cached)" if Offline.calls > before else ""
            charts = turn.charts
            if not charts:
                print(f"  none   {name}: no chart{note}")
                continue
            (out / f"{name}.json").write_text(json.dumps([dataclasses.asdict(c) for c in charts], indent=1, default=str))
            for i, spec in enumerate(charts):
                (out / f"{name}{'' if i == 0 else f'_{i + 1}'}.png").write_bytes(render(spec, tz))
            try:
                (out / f"{name}.result.json").write_text(json.dumps(json.loads(result), indent=1, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
            print(f"  ok     {name}: {len(charts)} chart(s){note}")
        for source in (eco, air):
            if source:
                await source.close()
    print(f"written to {out}/")


if __name__ == "__main__":
    asyncio.run(main())
