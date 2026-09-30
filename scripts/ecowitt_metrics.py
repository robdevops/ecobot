"""Which metrics does the Ecowitt station report? Asks the cloud API about every data group its documentation lists,
one group at a time (one unknown group fails a whole request), and prints the fields each group answers with, their
current values and units, and whether the bot fetches the group already. Needs the API keys in the environment, as the
bot does (load your env file into the shell first; this script never reads it):

    set -a; . /path/to/envfile; set +a
    python scripts/ecowitt_metrics.py                 # every documented group (about 2-3 minutes: requests are paced)
    python scripts/ecowitt_metrics.py --quick         # the groups without channel numbers (outdoor, wind, solar ...)
    python scripts/ecowitt_metrics.py --groups outdoor,solar_and_uvi,lightning
    python scripts/ecowitt_metrics.py --history       # also check each group is kept in the 30-minute history

A group is "yes" when it answers with readings, "no data" when Ecowitt knows the name but your station has no such
sensor, and "invalid" when Ecowitt does not accept the name.
"""

import asyncio
import os
from datetime import datetime, timedelta

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from lib.config import _tz  # noqa: E402
from lib.ecowitt.api import GROUPS, EcowittAPI, EcowittError  # noqa: E402

# Data groups the API's documentation (real_time and history "call_back") lists. Channels are numbered.
SINGLE = ["outdoor", "indoor", "solar_and_uvi", "rainfall", "rainfall_piezo", "wind", "pressure", "lightning", "indoor_co2",
          "co2_aqi_combo", "pm10_aqi_combo", "t_rh_aqi_combo", "pm25_aqi_combo", "battery"]
CHANNELS = {"pm25_ch": 4, "water_leak_ch": 4, "temp_and_humidity_ch": 8, "soil_ch": 16, "temp_ch": 8, "leaf_ch": 8}


def leaves(node, path=""):
    """(name, unit, value) for every reading in a response: a dict with a "value" (real_time) or a "list" (history)."""
    if isinstance(node, dict):
        if "value" in node or "list" in node:
            yield path, node.get("unit", ""), node.get("value") if "value" in node else f"{len(node['list'])} readings"
            return
        for key, child in node.items():
            yield from leaves(child, f"{path}.{key}" if path else key)


async def probe(api: EcowittAPI, mac: str, group: str, history: bool) -> tuple[str, list[tuple[str, str, object]], list[str]]:
    """("yes" / "no data" / "invalid" / the error, current readings, the history fields)."""
    try:
        data = await api._get("real_time", mac=mac, call_back=group, temp_unitid=1, pressure_unitid=3, wind_speed_unitid=7,
                              rainfall_unitid=12)
    except EcowittError as e:
        return ("invalid" if "invalid" in str(e).lower() else f"error: {e}"), [], []
    found = list(leaves(data.get(group) if isinstance(data, dict) and group in data else data))
    if not found:
        return "no data", [], []
    fields: list[str] = []
    if history:
        now = datetime.now(_tz())
        try:
            hist = await api.history(mac, "30min", (now - timedelta(hours=6)).replace(tzinfo=None), now.replace(tzinfo=None), group)
            fields = sorted(name for name, _, _ in leaves(hist.get(group) or {}))
        except EcowittError as e:
            fields = [f"(history: {e})"]
    return "yes", found, fields


async def main():
    ap = parser(__doc__)
    ap.add_argument("--quick", action="store_true", help="only the groups without channel numbers")
    ap.add_argument("--groups", help="comma-separated groups to ask about, instead of the documented list")
    ap.add_argument("--history", action="store_true", help="also ask for the last 6 hours at 30 minutes, to list the history fields")
    args = ap.parse_args()
    key, app = os.getenv("ECOWITT_API_KEY", "").strip(), os.getenv("ECOWITT_APP_KEY", "").strip()
    if not (key and app):
        raise SystemExit("Set ECOWITT_API_KEY and ECOWITT_APP_KEY in the environment first (see the top of this file).")
    if args.groups:
        names = [g.strip() for g in args.groups.split(",") if g.strip()]
    else:
        names = SINGLE + ([] if args.quick else [f"{base}{n}" for base, count in CHANNELS.items() for n in range(1, count + 1)])
    api = EcowittAPI(key, app)
    try:
        devices = await api.devices()
        if not devices:
            raise SystemExit("No Ecowitt device on this account.")
        mac = str(devices[0]["mac"]).upper()
        print(f"station {devices[0].get('name', '')!r} {mac}; {len(names)} groups to ask about (about {2 * len(names)} s)\n")
        silent = 0
        for group in names:
            status, found, fields = await probe(api, mac, group, args.history)
            if status != "yes":
                silent += 1
                if status != "no data" and status != "invalid":
                    print(f"{group:24} {status}")
                continue
            print(f"{group:24} yes  {'(fetched by the bot)' if group in GROUPS else '(NOT fetched by the bot)'}")
            for name, unit, value in found:
                print(f"    {name:32} {str(value):>10} {unit}")
            if fields:
                print(f"    history fields: {', '.join(fields)}")
        print(f"\n{silent} group(s) had no data or were not accepted.")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
