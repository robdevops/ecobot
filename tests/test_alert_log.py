"""The alert log (lib/alert_log.py): docs/ecobot-mcp.md 3.6."""

import asyncio

from lib.alert_log import KEEP_SECONDS, AlertLog
from tests.fakes import TZ

T0 = 1_790_000_000.0   # 2026-09-21, a time the tests' clock starts at


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def new_log(tmp_path, clock=None):
    return AlertLog(tmp_path / "alerts.sqlite", TZ, now=clock or Clock())


# ---------- the log ----------
def test_events_are_kept_in_order_with_rising_ids_and_their_parts(tmp_path):
    log = new_log(tmp_path)
    chart = {"title": "Wind", "drawn_by": {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True}}}
    first = log.append("rain", "🌧️ It's started raining.", "Rain started", {"rate_mm_h": 1.2}, expires=T0 + 3600)
    second = log.append("gusts", "💨 Strong gusts", "Strong gusts", expires=T0 + 3600, link=("live chart", "https://x"), chart=chart)
    assert (first["id"], second["id"]) == (1, 2) and log.last_id() == 2
    assert first == {"id": 1, "kind": "rain", "time": "2026-09-22T00:13:20+10:00", "expires": "2026-09-22T01:13:20+10:00",
                     "title": "Rain started", "text": "🌧️ It's started raining.", "data": {"rate_mm_h": 1.2}}
    assert second["link"] == {"label": "live chart", "url": "https://x"} and second["chart"] == chart and second["data"] == {}
    assert [e["id"] for e in log.since(0)] == [1, 2] and [e["id"] for e in log.since(1)] == [2] and log.since(2) == []
    assert log.get(2) == second and log.get(3) is None


def test_the_log_survives_a_restart_and_its_ids_never_go_back(tmp_path):
    clock = Clock()
    log = new_log(tmp_path, clock)
    for n in range(3):
        log.append("uv", f"UV {n}", "Very high UV", expires=T0 + 3600)
    log.close()
    clock.t += KEEP_SECONDS + 1                       # a month later: those are dropped when it opens again ...
    again = new_log(tmp_path, clock)
    assert again.since(0, expired=True) == [] and again.last_id() == 3
    assert again.append("uv", "UV 3", "Very high UV")["id"] == 4   # ... and the ids carry on
    third = new_log(tmp_path, clock)
    assert [e["text"] for e in third.since(0)] == ["UV 3"]


def test_since_leaves_out_what_has_expired_unless_asked(tmp_path):
    clock = Clock()
    log = new_log(tmp_path, clock)
    log.append("rain", "short", "t", expires=T0 + 60)
    log.append("pollen_and_asthma", "long", "t", expires=T0 + 86400)
    log.append("rain", "never", "t")
    clock.t += 120
    assert [e["text"] for e in log.since(0)] == ["long", "never"]
    assert [e["text"] for e in log.since(0, expired=True)] == ["short", "long", "never"]


async def test_a_reader_waits_for_the_next_event(tmp_path):
    log = new_log(tmp_path)
    assert await log.wait(0, timeout=0.05) is False
    waiter = asyncio.create_task(log.wait(0))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    log.append("rain", "x", "t")
    assert await asyncio.wait_for(waiter, 1) is True
    assert await log.wait(0, timeout=0.01) is True    # one is there already: no wait
    assert await log.wait(1, timeout=0.05) is False


# ---------- the rules' store ----------
