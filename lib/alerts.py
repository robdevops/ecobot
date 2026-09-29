"""Weather alerts, sent to every chat the bot knows about.

  - Rain: "stopped" after 30 dry minutes, with how much fell; any rain after that is a
    new "started". One rule both ways, so the alerts never contradict each other.
  - Rain likely soon: pressure falling over 3 hours plus very humid air (and optionally a
    small temperature/dew point gap and rising gusts). At most once every 6 hours.
  - Temperatures crossing: when outdoor becomes warmer than indoor (or cooler) after the
    other way round held for 2+ days. A 0.3 degree margin stops sensor noise flip-flopping.

  - Air quality (AirGradient, checked every 30 minutes): "wear a mask outside" when PM2.5
    or PM10 reaches US AQI 151+ (unhealthy) for two checks in a row; "back to safe" when
    both return to AQI 100 or below for two checks in a row. Every episode gets the pair.

Weather checks run after each keep-warm refresh, from the same 5-minute readings (no extra Ecowitt
requests). Chats and monitor state are kept in a small JSON file, so restarts neither
forget chats nor repeat alerts.
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone, tzinfo

from telegram import MessageEntity
from telegram.error import BadRequest, Forbidden, TelegramError

log = logging.getLogger(__name__)

RAIN_STOP_DRY_SECONDS = 30 * 60
PREDICT_MIN_SCORE = 4
# Air pressure has a twice-daily "tide" (highs ~10am/10pm, lows ~4am/4pm local solar time).
# Around 37 degrees south its swing is ~0.7 hPa either side; it's subtracted before judging a fall.
TIDE_AMPLITUDE_HPA = 0.7
NIGHT_HOURS = (20, 8)   # 8pm-8am: cooling alone brings the air close to its dew point
PREDICT_EVERY_SECONDS = 6 * 3600
OPT_OUT = "/alerts off to mute"   # shown small (italic) under each alert
CROSS_MIN_SECONDS = 2 * 86400
CROSS_MARGIN = 0.3


class BotState:
    """Known chats (with alerts on/off) and monitor state, saved to a JSON file."""

    def __init__(self, path: str):
        self.path = path
        data: dict = {}
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Couldn't read %s - starting with no known chats", path)
        self.chats: dict[int, dict] = {int(k): v for k, v in data.get("chats", {}).items()}
        self.monitor: dict = data.get("monitor", {})
        log.debug("Alerts: %d known chat(s) in %s", len(self.chats), path)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"chats": {str(k): v for k, v in self.chats.items()}, "monitor": self.monitor}, f, indent=1)
        os.replace(tmp, self.path)

    def add_chat(self, chat_id: int, title: str):
        entry = self.chats.get(chat_id)
        if entry is None:
            self.chats[chat_id] = {"title": title, "alerts": True}
            log.info("Alerts: now sending to %s (id %s)", title, chat_id)
            self.save()
        elif entry.get("title") != title:
            entry["title"] = title
            self.save()

    def remove_chat(self, chat_id: int, reason: str):
        if self.chats.pop(chat_id, None) is not None:
            log.info("Alerts: no longer sending to chat %s (%s)", chat_id, reason)
            self.save()

    def set_alerts(self, chat_id: int, title: str, on: bool):
        self.chats.setdefault(chat_id, {"title": title})["alerts"] = on
        self.save()

    def alert_chats(self) -> list[int]:
        return [c for c, v in self.chats.items() if v.get("alerts", True)]


def _utf16_len(text: str) -> int:
    """Telegram measures entity positions in UTF-16 code units (emoji count as 2)."""
    return len(text.encode("utf-16-le")) // 2


def with_footer(text: str, link: tuple[str, str] | None = None, extra: str | None = None) -> tuple[str, list]:
    """Add a small italic footer line directly under the text: the link's label (clickable),
    then any extra text, joined by " \u00b7 ". Plain text stays plain (no HTML to escape);
    only the footer is formatted. link = (label, url)."""
    text = text.rstrip()
    parts = ([link[0]] if link else []) + ([extra] if extra else [])
    if not parts:
        return text, []
    start = _utf16_len(text) + 1
    footer = " \u00b7 ".join(parts)
    entities = [MessageEntity(MessageEntity.ITALIC, start, _utf16_len(footer))]
    if link:
        entities.append(MessageEntity(MessageEntity.TEXT_LINK, start, _utf16_len(link[0]), url=link[1]))
    return f"{text}\n{footer}", entities


async def send_to_all(bot, state: BotState, text: str, link: tuple[str, str] | None = None):
    """Send an alert to every chat with alerts on; forget chats the bot can no longer post to.
    link = (label, url) adds a clickable label to the italic footer (e.g. the live chart)."""
    text, entities = with_footer(text, link, OPT_OUT)
    sent = 0
    for chat_id in state.alert_chats():
        try:
            await bot.send_message(chat_id, text, entities=entities, disable_web_page_preview=True)
            sent += 1
        except Forbidden as e:  # kicked from the group, or blocked in a private chat
            state.remove_chat(chat_id, f"can't post: {e}")
        except BadRequest as e:
            if "not found" in str(e).lower():
                state.remove_chat(chat_id, f"chat gone: {e}")
            else:
                log.warning("Alert to %s failed: %s", chat_id, e)
        except TelegramError as e:
            log.warning("Alert to %s failed: %s", chat_id, e)
    log.info("Alert sent to %d chat(s): %s", sent, text.replace("\n", " "))


def _rows(data: dict) -> list[tuple[int, dict]]:
    """Ecowitt 'data' -> [(ts, {"group.field": value})] sorted by time."""
    rows: dict[int, dict] = {}
    for grp, fields in data.items():
        if not isinstance(fields, dict):
            continue
        for field, obj in fields.items():
            if isinstance(obj, dict) and isinstance(obj.get("list"), dict):
                for ts, v in obj["list"].items():
                    try:
                        rows.setdefault(int(ts), {})[f"{grp}.{field}"] = float(v)
                    except (TypeError, ValueError):
                        pass
    return sorted(rows.items())


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"


class Monitor:
    def __init__(self, make_fetcher, tz: tzinfo, mac: str, state: BotState, notify,
                 groups: str = "outdoor,indoor,rainfall,pressure,wind", rain_group: str = "rainfall",
                 longitude: float = 145.0):
        self.make_fetcher, self.tz, self.mac, self.state, self.notify = make_fetcher, tz, mac, state, notify
        self.groups, self.rain, self.longitude = groups, rain_group, longitude

    def _tide(self, ts: int) -> float:
        """Expected pressure offset from the atmospheric tide at this moment (hPa)."""
        utc = datetime.fromtimestamp(ts, timezone.utc)
        solar_hour = (utc.hour + utc.minute / 60 + self.longitude / 15) % 24
        return TIDE_AMPLITUDE_HPA * math.cos(2 * math.pi * (solar_hour - 10) / 12)

    def _now(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None, microsecond=0)

    async def _recent(self, hours: float) -> list[tuple[int, dict]]:
        now = self._now()
        data = await self.make_fetcher(self.mac, self.groups).get("5min", now - timedelta(hours=hours), now)
        return _rows(data)

    async def init(self):
        """Work out the current state from recent data without alerting (first run only)."""
        m = self.state.monitor
        if "rain" not in m:
            rows = await self._recent(3)
            wet = self._wet_flags(rows)
            raining = any(w for _, w, _ in wet[-2:])
            m["rain"] = {"raining": raining, "since": next((ts for ts, w, _ in wet if w), None) if raining else None}
            log.debug("Alerts: rain monitor starting (%s)", "raining now" if raining else "dry")
        if "cross" not in m:
            now = self._now()
            data = await self.make_fetcher(self.mac, "outdoor,indoor").get(
                "30min", now - timedelta(days=6, hours=23), now)
            side, since = None, None
            for ts, r in _rows(data):
                s = self._side(r)
                if s and s != side:
                    side, since = s, ts
            if side:
                m["cross"] = {"side": side, "since": since}
                log.debug("Alerts: outdoor has been %s than indoor since %s", side,
                         datetime.fromtimestamp(since, timezone.utc).astimezone(self.tz).strftime("%a %d %b %H:%M"))
        self.state.save()

    def _wet_flags(self, rows) -> list[tuple[int, bool, float]]:
        """[(ts, rained in this reading, rain rate)]: rate above zero, or the daily total rising."""
        out, prev_daily = [], None
        for ts, r in rows:
            rate = r.get(f"{self.rain}.rain_rate", 0.0)
            daily = r.get(f"{self.rain}.daily")
            rising = daily is not None and prev_daily is not None and daily > prev_daily  # ignores the midnight reset
            if daily is not None:
                prev_daily = daily
            out.append((ts, rate > 0 or rising, rate))
        return out

    def _side(self, r: dict) -> str | None:
        o, i = r.get("outdoor.temperature"), r.get("indoor.temperature")
        if o is None or i is None:
            return None
        return "warmer" if o - i > CROSS_MARGIN else "cooler" if o - i < -CROSS_MARGIN else None

    async def check(self):
        """Look at the latest readings and send any alerts. Called after each refresh."""
        rows = await self._recent(3)
        if not rows:
            return
        await self._check_rain(rows)
        await self._check_rain_likely(rows)
        await self._check_cross(rows)
        self.state.save()

    async def _check_rain(self, rows):
        m = self.state.monitor.setdefault("rain", {"raining": False, "since": None})
        wet = self._wet_flags(rows)
        latest_ts = wet[-1][0]
        wet_times = [ts for ts, w, _ in wet if w]
        last_wet = wet_times[-1] if wet_times else None
        if not m["raining"] and any(w for _, w, _ in wet[-2:]):
            rate = max(r for _, w, r in wet[-2:] if w)
            m.update(raining=True, since=next(ts for ts, w, _ in wet[-2:] if w))
            await self.notify("\U0001f327\ufe0f It's started raining" + (f" ({rate:g} mm/h)." if rate > 0 else "."))
        elif m["raining"] and (last_wet is None or latest_ts - last_wet >= RAIN_STOP_DRY_SECONDS):
            if last_wet is None:  # nothing in the last 3 hours (e.g. the bot was down): close it quietly
                log.info("Alerts: rain ended while not watching; no alert")
            else:
                since = m.get("since") or wet_times[0]
                fell = self._amount(rows, since, last_wet)
                amount = f"{fell:.1f} mm fell" if fell else "Only a trace fell"
                took = _duration(last_wet + 300 - since)
                await self.notify(f"\U0001f324\ufe0f The rain has stopped. {amount} over {took}.")
            m.update(raining=False, since=None)

    def _amount(self, rows, since: int, last_wet: int) -> float | None:
        """Rain that fell between since and last_wet: the rise in the daily total (allowing for
        its midnight reset). Ecowitt's 'event' counter isn't used, as it spans several showers."""
        total, prev = 0.0, None
        for ts, r in rows:
            daily = r.get(f"{self.rain}.daily")
            if daily is None or ts > last_wet:
                continue
            if ts >= since and prev is not None:
                total += daily - prev if daily >= prev else daily  # a drop means the midnight reset
            prev = daily
        return round(total, 1) if prev is not None else None

    async def _check_rain_likely(self, rows):
        """Rain likely soon: falling pressure plus arriving moisture, scored. Heuristic, clearly
        labelled as such. Not while raining or within an hour of rain; at most every 6 hours.

        Two normal night-time effects are ignored, as they aren't signs of rain: the pressure
        tide's overnight dip (subtracted from the fall), and the air cooling to near its dew
        point (at night only a *rising* dew point counts, i.e. moister air moving in)."""
        m = self.state.monitor.setdefault("predict", {"last": 0})
        rain = self.state.monitor.get("rain", {})
        latest_ts, latest = rows[-1]
        if rain.get("raining") or any(w for ts, w, _ in self._wet_flags(rows) if latest_ts - ts < 3600):
            return
        if latest_ts - m.get("last", 0) < PREDICT_EVERY_SECONDS:
            return
        get = lambda r, k: r.get(k)
        old_ts, old = next(((ts, r) for ts, r in rows if latest_ts - ts <= 3 * 3600
                            and get(r, "pressure.relative") is not None), (None, None))
        hour_ago = next((r for ts, r in rows if latest_ts - ts <= 3600), None)
        two_hours_ago = next((r for ts, r in rows if latest_ts - ts <= 2 * 3600), None)
        p_now, p_old = get(latest, "pressure.relative"), get(old or {}, "pressure.relative")
        if p_now is None or p_old is None:
            return
        raw_drop = p_old - p_now
        drop = raw_drop + (self._tide(latest_ts) - self._tide(old_ts))  # remove the tide's expected change
        if drop < 1.0:  # a real (beyond-tide) pressure fall is required
            return
        score, reasons = 0, [f"pressure down {raw_drop:.1f} hPa in 3 hours"]
        score += 3 if drop >= 3 else 2 if drop >= 2 else 1
        hour = datetime.fromtimestamp(latest_ts, timezone.utc).astimezone(self.tz).hour
        night = hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]
        t, dp = get(latest, "outdoor.temperature"), get(latest, "outdoor.dew_point")
        if night:
            dp_old = get(two_hours_ago or {}, "outdoor.dew_point")
            if dp is not None and dp_old is not None and dp - dp_old >= 1:
                score += 2 if dp - dp_old >= 2 else 1
                reasons.append(f"dew point up {dp - dp_old:.1f}\u00b0C in 2 hours")
        else:
            hum = get(latest, "outdoor.humidity")
            if hum is not None:
                rising = hour_ago is not None and get(hour_ago, "outdoor.humidity") is not None \
                    and hum - get(hour_ago, "outdoor.humidity") >= 5
                if hum >= 90 or rising:
                    score += (2 if hum >= 95 else 1 if hum >= 90 else 0) + (1 if rising else 0)
                    reasons.append(f"humidity {hum:.0f}%" + (" and rising" if rising else ""))
            if t is not None and dp is not None and t - dp <= 2:
                score += 2 if t - dp <= 1 else 1
                reasons.append(f"only {t - dp:.1f}\u00b0C above the dew point")
        gusts = [(ts, get(r, "wind.wind_gust")) for ts, r in rows if get(r, "wind.wind_gust") is not None]
        recent = [g for ts, g in gusts if latest_ts - ts <= 1800]
        earlier = [g for ts, g in gusts if latest_ts - ts > 1800]
        if recent and earlier and max(recent) - sum(earlier) / len(earlier) >= 10:
            score += 1
            reasons.append(f"gusts picking up ({max(recent):.0f} km/h)")
        log.info("Rain-likely check: pressure fall %.1f hPa (%.1f beyond the tide), %s, score %d",
                 raw_drop, drop, "night" if night else "day", score)
        if score >= PREDICT_MIN_SCORE:
            m["last"] = latest_ts
            await self.notify("\U0001f326\ufe0f Rain looks likely soon: " + ", ".join(reasons[:3]) +
                              ". (An estimate from the station's readings, not an official forecast.)")

    async def _check_cross(self, rows):
        ts, r = rows[-1]
        side = self._side(r)
        c = self.state.monitor.get("cross")
        if side is None:
            return
        if c is None:
            self.state.monitor["cross"] = {"side": side, "since": ts}
            return
        if side != c["side"]:
            held = ts - c["since"]
            if held >= CROSS_MIN_SECONDS:
                days = int(held // 86400)
                o, i = r["outdoor.temperature"], r["indoor.temperature"]
                await self.notify(f"\U0001f321\ufe0f It's now {side} outside ({o:.1f}\u00b0C) than inside "
                                  f"({i:.1f}\u00b0C), for the first time in {days} days.")
            c.update(side=side, since=ts)


# Air quality: particle levels only (masks filter particles, not gases like CO2/VOC/NOx)
MASK_PM25, MASK_PM10 = 55.5, 255.0     # US AQI 151+ ("unhealthy"): masks advised for everyone outdoors
SAFE_PM25, SAFE_PM10 = 35.4, 154.0     # US AQI 100 or better: acceptable for everyone
AIR_CONFIRM_CHECKS = 2                 # consecutive 30-minute checks, so a passing puff of smoke doesn't count
AIR_STALE_SECONDS = 2 * 3600           # ignore readings older than this (sensor offline)


class AirMonitor:
    """Reads the AirGradient sensor every 30 minutes and sends mask / back-to-safe alerts."""

    def __init__(self, air, tz: tzinfo, state: BotState, notify, link: tuple[str, str] | None = None):
        self.air, self.tz, self.state, self.notify, self.link = air, tz, state, notify, link

    async def check(self, reading: dict | None = None, now: datetime | None = None):
        reading = reading if reading is not None else await self.air.current()
        now = now or datetime.now(self.tz)
        m = self.state.monitor.setdefault("air", {"unsafe": False, "above": 0, "below": 0})
        when = reading.get("_time_utc")
        if when and (now - when).total_seconds() > AIR_STALE_SECONDS:
            log.info("Air quality: latest reading is stale (%s); skipping", when)
            return
        pm25 = (reading.get("pm2_5") or {}).get("value")
        pm10 = (reading.get("pm10") or {}).get("value")
        if pm25 is None and pm10 is None:
            return
        bad = (pm25 is not None and pm25 >= MASK_PM25) or (pm10 is not None and pm10 >= MASK_PM10)
        good = (pm25 is None or pm25 <= SAFE_PM25) and (pm10 is None or pm10 <= SAFE_PM10)
        m["above"] = m["above"] + 1 if bad else 0
        m["below"] = m["below"] + 1 if good else 0
        levels = self._levels(reading)
        if not m["unsafe"] and m["above"] >= AIR_CONFIRM_CHECKS:
            m["unsafe"] = True
            await self.notify("\U0001f637 Unhealthy air outside \u2014 wear a P2/N95 mask today. " + levels, link=self.link)
        elif m["unsafe"] and m["below"] >= AIR_CONFIRM_CHECKS:
            m["unsafe"] = False
            await self.notify("\u2705 Outdoor air is safe again. " + levels, link=self.link)
        self.state.save()

    @staticmethod
    def _levels(reading: dict) -> str:
        """Short summary: PM2.5 (with AQI), plus PM10 only when it's elevated."""
        parts = []
        pm25, pm10 = reading.get("pm2_5") or {}, reading.get("pm10") or {}
        if pm25.get("value") is not None:
            parts.append(f"PM2.5 {pm25['value']:.0f} \u00b5g/m\u00b3" + (f" (AQI {pm25['aqi_us']})" if "aqi_us" in pm25 else ""))
        if pm10.get("value") is not None and pm10["value"] > SAFE_PM10:
            parts.append(f"PM10 {pm10['value']:.0f} \u00b5g/m\u00b3")
        return (", ".join(parts) + ".") if parts else ""
