"""The pollen source: grass pollen and thunderstorm asthma risk from melbournepollen.com.au, kept warm and cached.

The site is a website, not an API, so it is fetched gently: only between 6 am and 6 pm (lib/warm.py SYNC_HOURS), every 30
minutes at most, with a conditional request (a 304 costs the site almost nothing); a question is answered from the last fetch.
One fetch is made at the start (or the first question) when nothing is cached, even at night."""

import json
import logging
import time
from datetime import datetime

import httpx

from .. import intent
from ..config import Config
from ..timeutil import now_local
from ..tools import Tool, Turn
from ..warm import Warmer, in_sync_hours
from .parse import LEVEL_EMOJI, parse_melbourne_pollen

log = logging.getLogger(__name__)

URL = "https://www.melbournepollen.com.au/"
HEADERS = {"User-Agent": "ecobot/1.0 (personal weather bot; a few requests a day)"}
POLLEN_REFRESH_SECONDS = 30 * 60

DESCRIPTION = ("Melbourne's grass pollen level today and the thunderstorm asthma risk (Low, Moderate, High or Extreme), from "
               "melbournepollen.com.au. Use it for pollen, hay fever and thunderstorm asthma questions. Takes no arguments. "
               "The result's \"lines\" are ready-made, emoji included: copy them as they are.")
PARAMETERS = {"type": "object", "properties": {}}


class Pollen:
    name = "Pollen"

    def __init__(self, cfg: Config, transport=None):
        self.tz, self.district = cfg.tz, cfg.pollen_district
        self.client = httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True, transport=transport)
        self.data: dict | None = None        # the parsed page
        self.fetched_at = 0.0                # when (epoch)
        self._validators: dict = {}          # ETag / Last-Modified for the conditional request
        self.requests = 0
        self.warmer = Warmer(self.warm, POLLEN_REFRESH_SECONDS)
        self.tools = [Tool("pollen_asthma", DESCRIPTION, PARAMETERS, self.handle)]

    async def start(self):
        try:
            await self.warm(True)
            log.info("Pollen: %s", "; ".join(self.lines()) or "no levels on the page")
        except Exception as e:  # the site may just be briefly unreachable
            log.warning("Pollen not readable yet: %s", e)

    def describe(self) -> str:
        return "Melbourne pollen forecast and thunderstorm asthma risk (melbournepollen.com.au) (people say \"pollen\" or \"hay fever\")"

    def wants(self, text: str) -> bool:
        return intent.mentions_pollen(text) or intent.wants_report(text)

    def poke(self):
        self.warmer.poke()

    async def close(self):
        await self.client.aclose()

    # ---------- fetching ----------
    def now(self) -> datetime:
        return now_local(self.tz)

    async def warm(self, fresh: bool = True) -> str:
        """Fetch the page when it is due: never outside the sync hours (unless nothing is cached), and not more often than
        the poll interval."""
        age = time.time() - self.fetched_at
        if self.data is not None and (not in_sync_hours(self.now()) or age < POLLEN_REFRESH_SECONDS - 5 and not fresh):
            return "Pollen cached"
        self.requests += 1
        res = await self.client.get(URL, headers=self._validators)
        if res.status_code == 304 and self.data is not None:
            self.fetched_at = time.time()
            return "Pollen 1 req (not modified)"
        res.raise_for_status()
        self._validators = {k: v for k, v in (("If-None-Match", res.headers.get("etag")),
                                              ("If-Modified-Since", res.headers.get("last-modified"))) if v}
        self.data, self.fetched_at = parse_melbourne_pollen(res.text), time.time()
        if not self.data["melbourne_grass"] and not self.data["district_grass"]:
            log.warning("Pollen: no grass pollen level found on the page (has its layout changed?)")
        return "Pollen 1 req"

    # ---------- reading ----------
    def current(self) -> dict:
        """{"grass": {level, emoji, date} | None, "asthma": {level, emoji, updated} | None, "fetched_at": epoch}"""
        d = self.data or {}
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
        if self.data is None:
            try:
                await self.warm(True)
            except Exception as e:
                log.warning("Pollen not readable: %s", e)
        lines = self.lines()
        if not lines:
            return json.dumps({"error": "No pollen levels are available right now."})
        cur = self.current()
        out = {"lines": lines, "source": "melbournepollen.com.au (grass pollen from pollen-trap data; asthma risk from the "
                                         "Victorian Department of Health and BOM, 1 Oct - 31 Dec)"}
        if cur["asthma"] and cur["asthma"]["updated"]:
            out["asthma_updated"] = cur["asthma"]["updated"]
        if not cur["asthma"]:
            out["asthma_note"] = "No thunderstorm asthma forecast now (it runs 1 Oct - 31 Dec)."
        return json.dumps(out, ensure_ascii=False)
