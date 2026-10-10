"""The pollen source: grass pollen and thunderstorm asthma risk from melbournepollen.com.au, kept warm and cached.

The site is a website, not an API, so it is fetched gently: only between 6 am and 6 pm (lib/warm.py SYNC_HOURS), every 30
minutes at most, with a conditional request (a 304 costs the site almost nothing); a question is answered from the last fetch.
One fetch is made at the start (or the first question) when nothing is cached, even at night."""

import json
import logging
import time
from datetime import date, datetime

import httpx

from .. import intent
from ..config import Config
from ..snapshots import Snapshots
from ..timeutil import now_local
from ..tools import Tool, Turn
from ..warm import Warmer, in_sync_hours
from .parse import LEVEL_EMOJI, parse_melbourne_pollen

log = logging.getLogger(__name__)

URL = "https://www.melbournepollen.com.au/"
HEADERS = {"User-Agent": "ecobot/1.0 (personal weather bot; a few requests a day)"}
POLLEN_REFRESH_SECONDS = 30 * 60
SEASON_MONTHS = (10, 11, 12)   # grass pollen and the thunderstorm asthma forecast run October to December: nothing else is fetched, shown or alerted
OFF_SEASON = "Pollen and thunderstorm asthma forecasts only run from October to December."

DESCRIPTION = ("Melbourne's grass pollen level today and the thunderstorm asthma risk (Low, Moderate or High), from "
               "melbournepollen.com.au. Use it for pollen, hay fever and thunderstorm asthma questions. Takes no arguments. "
               "The result's \"lines\" are ready-made, emoji included: copy them as they are.")
PARAMETERS = {"type": "object", "properties": {}}


class Pollen:
    name = "Pollen"

    def __init__(self, cfg: Config, transport=None):
        self.tz, self.district, self.place = cfg.tz, cfg.pollen_district, cfg.place
        self.client = httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True, transport=transport)
        self.data: dict | None = None        # the parsed page
        self.fetched_at = 0.0                # when (epoch)
        self._validators: dict = {}          # ETag / Last-Modified for the conditional request
        self.requests = 0
        self.store = Snapshots.open(cfg.conditions_cache_path)
        self._load()
        self.warmer = Warmer(self.warm, POLLEN_REFRESH_SECONDS)
        self.tools = [Tool("pollen_asthma", DESCRIPTION, PARAMETERS, self.handle)]

    async def start(self):
        if not self.in_season():
            log.info("Pollen: out of season (October to December)")
            return
        try:
            await self.warm(True)
            log.info("Pollen: %s", "; ".join(self.lines()) or "no levels on the page")
        except Exception as e:  # the site may just be briefly unreachable
            log.warning("Pollen not readable yet: %s", e)

    def describe(self) -> str:
        return "Melbourne pollen forecast and thunderstorm asthma risk (melbournepollen.com.au) (people say \"pollen\" or \"hay fever\")"

    def wants(self, text: str) -> bool:
        """Only pollen questions start a refresh; the report always uses what is cached."""
        return intent.mentions_pollen(text)

    def poke(self):
        self.warmer.poke()

    async def close(self):
        await self.client.aclose()
        self.store.close()

    # ---------- kept on disk ----------
    def _load(self):
        """Start from the last page seen (before the restart), so a night-time start needs no fetch."""
        if last := self.store.latest("pollen"):
            self.fetched_at, saved = last
            self.data = {**saved, "melbourne_date": date.fromisoformat(saved["melbourne_date"]) if saved.get("melbourne_date") else None}
            self._validators = json.loads(self.store.get("pollen_validators") or "{}")

    def _save(self):
        d = self.data
        self.store.save("pollen", {**d, "melbourne_date": d["melbourne_date"].isoformat() if d.get("melbourne_date") else None},
                        now=self.fetched_at)
        self.store.put("pollen_validators", json.dumps(self._validators))

    # ---------- fetching ----------
    def now(self) -> datetime:
        return now_local(self.tz)

    def in_season(self) -> bool:
        return self.now().month in SEASON_MONTHS

    async def warm(self, fresh: bool = True) -> str:
        """Fetch the page when it is due: never out of season, never outside the sync hours (unless nothing is cached), and not
        more often than the poll interval."""
        if not self.in_season():
            return "Pollen out of season"
        age = time.time() - self.fetched_at
        if self.data is not None and (not in_sync_hours(self.now()) or age < POLLEN_REFRESH_SECONDS - 5 and not fresh):
            return "Pollen cached"
        self.requests += 1
        res = await self.client.get(URL, headers=self._validators)
        if res.status_code == 304 and self.data is not None:
            self.fetched_at = time.time()
            self._save()                       # the same page: only its last-seen time moves
            return "Pollen 1 req (not modified)"
        res.raise_for_status()
        self._validators = {k: v for k, v in (("If-None-Match", res.headers.get("etag")),
                                              ("If-Modified-Since", res.headers.get("last-modified"))) if v}
        self.data, self.fetched_at = parse_melbourne_pollen(res.text), time.time()
        self._save()
        if not self.data["melbourne_grass"] and not self.data["district_grass"]:
            log.warning("Pollen: no grass pollen level found on the page (has its layout changed?)")
        return "Pollen 1 req"

    # ---------- reading ----------
    def current(self) -> dict:
        """{"grass": {level, emoji, date} | None, "asthma": {level, emoji, updated} | None, "fetched_at": epoch}"""
        d = self.data if self.in_season() else {}   # last season's page is never shown
        d = d or {}
        grass = d.get("melbourne_grass") or (d.get("district_grass") or {}).get(self.district)
        asthma = (d.get("thunderstorm_asthma") or {}).get(self.district)
        return {"grass": {"level": grass, "emoji": LEVEL_EMOJI[grass], "date": d.get("melbourne_date")} if grass else None,
                "asthma": {"level": asthma, "emoji": LEVEL_EMOJI[asthma], "updated": d.get("asthma_updated")} if asthma else None,
                "fetched_at": self.fetched_at}

    def lines(self) -> list[str]:
        """The ready-made report lines; the asthma line is left out when there is no risk forecast (outside the season)."""
        cur, out = self.current(), []
        if g := cur["grass"]:
            when = f" (forecast for {g['date']:%a %d %b})" if g["date"] and g["date"] != self.now().date() else ""
            out.append(f"Grass pollen: {g['emoji']} {g['level']}{when}")
        if a := cur["asthma"]:
            out.append(f"Thunderstorm asthma risk: {a['emoji']} {a['level']}")
        return out

    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        if not self.in_season():
            return json.dumps({"error": OFF_SEASON})
        if self.data is None and not args.get("cached"):   # the report ("cached") never fetches
            try:
                await self.warm(True)
            except Exception as e:
                log.warning("Pollen not readable: %s", e)
        lines = self.lines()
        if not lines:
            return json.dumps({"error": "No pollen levels are available right now."})
        cur = self.current()
        out = {"lines": lines, "place": self.place, "source": "melbournepollen.com.au (grass pollen from pollen-trap data; asthma risk from the "
                                         "Victorian Department of Health and BOM, 1 Oct - 31 Dec)"}
        if cur["asthma"] and cur["asthma"]["updated"]:
            out["asthma_updated"] = cur["asthma"]["updated"]
        if not cur["asthma"]:
            out["asthma_note"] = "No thunderstorm asthma forecast now (it runs 1 Oct - 31 Dec)."
        return json.dumps(out, ensure_ascii=False)
