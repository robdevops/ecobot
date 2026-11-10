"""Draw the charts the bot would draw, from the bot's file (ecobot.sqlite), and save each chart (the service's JSON, as drawn) and
picture (PNG). The charts are the service's tools' (lib/service: summary, relate, scan), each asked as the model or the bot asks it.
Strictly offline: no API keys, no network (any request raises), and your file is never touched (it is copied to a temporary folder
first). A question the file cannot answer is reported as "not cached" and skipped.

    python scripts/dump_specs.py --data-dir . --tz Australia/Melbourne --out chart_dump

Run it before and after a change to the chart code, and compare the two folders (the JSON files hold every point drawn).
"""

import asyncio
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
from lib.charts import render_data  # noqa: E402
from lib.compose import Composer  # noqa: E402
from lib.config import Config  # noqa: E402
from lib.ecowitt import Ecowitt  # noqa: E402
from lib.storage import FILE, Storage  # noqa: E402
from lib.service import Service  # noqa: E402
from lib.service.context import Context  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


class Offline(httpx.AsyncBaseTransport):
    """Every request fails, so nothing can leave this machine; the count says how many questions wanted the network."""
    calls = 0

    async def handle_async_request(self, request):
        Offline.calls += 1
        raise httpx.ConnectError("offline: dump_specs never uses the network")


def questions(now: datetime) -> list[tuple[str, str, dict]]:
    """(name, service tool, arguments)."""
    # The last day the caches treat as final (a day is final an hour after it ends): a still-settling day would need the network
    end = (now - timedelta(hours=25)).date()

    def back(days: int) -> dict:   # the whole days to that last final day
        return {"start": (end - timedelta(days=days - 1)).isoformat(), "end": end.isoformat()}

    chart = lambda name, *readings, **a: (name, "summary", {"readings": list(readings), "chart": True, **a})
    return [
        chart("eco_temp_1d", "outdoor.temperature", **back(1)),
        chart("eco_temp_2d_indoor_outdoor", "temperature", **back(2)),
        chart("eco_temp_7d", "temperature", **back(7)),
        chart("eco_temp_30d", "temperature", **back(30)),
        chart("eco_temp_90d", "outdoor.temperature", **back(90)),
        chart("eco_temp_400d", "outdoor.temperature", **back(400)),
        chart("eco_humidity_7d", "outdoor.humidity", **back(7)),
        chart("eco_pressure_7d", "pressure", **back(7)),
        chart("eco_pressure_90d", "pressure", **back(90)),
        chart("eco_wind_7d_compass", "wind", **back(7)),
        chart("eco_wind_90d_compass", "wind", **back(90)),
        chart("eco_temp_30d_average", "outdoor.temperature", average=True, **back(30)),
        chart("eco_stack_temp_rain_14d", "temperature", "rain", **back(14)),
        chart("eco_stack_temp_hum_pres_14d", "temperature", "humidity", "pressure", **back(14)),
        chart("eco_stack_wind_rain_30d", "wind", "rain", **back(30)),
        ("eco_link_pressure_90d", "relate", {"reading": "pressure", "chart": True, **back(90)}),
        ("eco_link_humidity_30d", "relate", {"reading": "humidity", "chart": True, **back(30)}),
        ("eco_link_wind_400d", "relate", {"reading": "wind", "chart": True, **back(400)}),
        chart("air_pm25_1d", "pm2.5", **back(1)),
        chart("air_pm25_7d", "pm2.5", **back(7)),
        chart("air_pm25_30d", "pm2.5", **back(30)),
        chart("air_pm25_200d_mixed_resolution", "pm2.5", **back(200)),
        chart("air_pm25_400d", "pm2.5", **back(360)),
        chart("air_all_7d", "air", **back(7)),
        chart("air_all_90d", "air", **back(90)),
        chart("air_co2_voc_14d", "co2", "voc", **back(14)),
        chart("air_pm_group_14d", "pm1", "pm2.5", "pm10", **back(14)),
        chart("plot_pm25_over_rain_30d", "pm2.5", "rain", **back(30)),
        chart("plot_rating_over_rain_30d", "pm2.5", "rain", chart_readings=["air.pm2_5:rating", "rain"], **back(30)),
        chart("plot_temp_pm25_humidity_60d", "outdoor.temperature", "pm2.5", "outdoor.humidity", **back(60)),
        chart("plot_wind_pm10_90d", "wind", "pm10", **back(90)),
        ("air_link_pm25_60d", "relate", {"reading": "pm2.5", "chart": True, **back(60)}),
        ("air_scan_90d", "scan", {"chart": True, **back(90)}),
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
    ap.add_argument("--data-dir", default=str(data_dir()), help="folder (default DATA_DIR) holding ecobot.sqlite")
    ap.add_argument("--tz", default="Australia/Melbourne")
    ap.add_argument("--out", default="chart_dump")
    args = ap.parse_args()
    data, tz, out = Path(args.data_dir), ZoneInfo(args.tz), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        if not (data / FILE).exists():
            raise SystemExit(f"{data} holds no {FILE}")
        copy_db(data / FILE, folder / FILE)
        storage = Storage(folder / FILE, tz)
        macs = sorted({s.device for s in storage.sensors.all() if s.kind != "air"})
        air_sensors = storage.sensors.all(kind="air")
        cfg = Config(telegram_token="", openrouter_api_key="", openrouter_base_url="", openrouter_model="", tz=tz, ecowitt_api_key="offline",
                     ecowitt_app_key="offline", airgradient_token="offline", airgradient_location=air_sensors[0].device if air_sensors else "offline",
                     airgradient_dashboard="", state_path=folder / "state.json", storage_path=folder / FILE)
        eco = air = None
        if macs:
            eco = Ecowitt(cfg, transport=Offline(), storage=storage)
            eco.mac = macs[0]
        if air_sensors:
            air = AirGradient(cfg, transport=Offline(), storage=storage, owned=eco is None)
        if not (eco or air):
            storage.close()
            raise SystemExit(f"{folder / FILE} holds no sensors")
        service = Service.of(await Context.open(eco=eco, air=air, composer=Composer(eco, air) if eco and air else None, tz=tz))

        now = datetime.now(tz)
        print(f"as of {now:%Y-%m-%d %H:%M %Z}; held: ecowitt={'yes' if eco else 'NO'} airgradient={'yes' if air else 'NO'}")
        for name, tool, tool_args in questions(now):
            if tool not in service:
                print(f"  skip   {name}: no {tool} tool (a source is missing)")
                continue
            before = Offline.calls
            try:
                result = json.loads(await service[tool].call(tool_args, {"chart_points": "drawn", "fetch": False}))
            except httpx.ConnectError:
                print(f"  cache  {name}: not fully cached (it would need the network)")
                continue
            except Exception as e:  # keep going: one broken chart must not hide the rest
                print(f"  ERROR  {name}: {type(e).__name__}: {e}")
                continue
            note = f" ({Offline.calls - before} request(s) refused: part of the range is not cached)" if Offline.calls > before else ""
            charts = result.pop("charts", None) or []
            if not charts:
                print(f"  none   {name}: no chart{note}{': ' + str(result['error']) if 'error' in result else ''}")
                continue
            (out / f"{name}.json").write_text(json.dumps(charts, indent=1, ensure_ascii=False))
            for i, chart in enumerate(charts):
                (out / f"{name}{'' if i == 0 else f'_{i + 1}'}.png").write_bytes(render_data(chart, tz))
            (out / f"{name}.result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
            print(f"  ok     {name}: {len(charts)} chart(s){note}")
        for source in (eco, air):
            if source:
                await source.close()
    print(f"written to {out}/")


if __name__ == "__main__":
    asyncio.run(main())
