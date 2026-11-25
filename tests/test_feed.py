"""lib/feed.py: an alert log as signalbot's feed (docs/ecobot-mcp.md 3.6): its identity, the kinds offered with labels and words, the
alerts not yet expired as items, each chart as the call that draws it, with its periods as the values a button may choose."""

from datetime import datetime

from lib import feed
from lib.alert_log import AlertLog
from lib.alerts.kinds import available_kinds
from tests.fakes import TZ

T0 = 1_790_000_000.0
DRAWN_BY = {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True},
            "periods": [{"period": "1d", "label": "1D"}, {"period": "7d", "label": "7D"}, {"period": "1m", "label": "1M"}]}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def test_the_alerts_not_yet_expired_are_items_with_their_link_and_their_chart_s_call(tmp_path):
    clock = Clock()
    log = AlertLog(tmp_path / "alerts.sqlite", TZ, now=clock)
    log.append("rain", "It rained", "Rain", expires=T0 - 1)                                   # 1: expired
    log.append("gusts", "💨 Gusts to 60 km/h", "Gusts", expires=T0 + 3600, chart={"title": "Wind", "drawn_by": DRAWN_BY})
    log.append("particulates", "😷 PM2.5 is high", "Particulates", expires=T0 + 600, link=("Dashboard", "https://example.org/d"))
    log.append("forecast", "", "Forecast changed", expires=None)                              # no text: the title stands in
    got = feed.feed_read(log, available_kinds(["Ecowitt", "AirGradient"]))
    assert got["identity"] == log.identity
    assert [(k["kind"], k["label"]) for k in got["kinds"]] == [("rain", "Rain"), ("gusts", "Gusts"), ("uv", "UV"),
                                                               ("temps", "Temperature crossing"), ("particulates", "Particulates")]
    assert next(k for k in got["kinds"] if k["kind"] == "gusts")["words"] == ["wind"]
    assert [i["id"] for i in got["items"]] == ["2", "3", "4"]
    gusts, air, forecast = got["items"]
    assert gusts["text"] == "💨 Gusts to 60 km/h" and datetime.fromisoformat(gusts["expires"]) > datetime.fromisoformat(gusts["time"])
    assert gusts["chart"] == {"tool": "summary", "arguments": DRAWN_BY["arguments"], "arg": "period", "values": ["1d", "7d", "1m"]}
    assert air["link"] == "https://example.org/d" and "chart" not in air
    assert forecast["text"] == "Forecast changed" and "expires" not in forecast


def test_a_chart_whose_call_names_no_period_of_its_own_list_has_no_values(tmp_path):
    log = AlertLog(tmp_path / "alerts.sqlite", TZ, now=Clock())
    start = {**DRAWN_BY, "arguments": {"readings": ["wind"], "start": "2026-09-20"}}
    log.append("gusts", "Gusty", "Gusts", expires=T0 + 60, chart={"drawn_by": start})
    log.append("gusts", "Gusty again", "Gusts", expires=T0 + 60, chart={"title": "no drawn_by"})
    first, second = feed.feed_read(log, ["gusts"])["items"]
    assert first["chart"] == {"tool": "summary", "arguments": start["arguments"]} and "chart" not in second


def test_only_the_newest_items_are_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "MAX_ITEMS", 2)
    log = AlertLog(tmp_path / "alerts.sqlite", TZ, now=Clock())
    for n in range(3):
        log.append("rain", f"Rain {n}", "Rain", expires=T0 + 60)
    assert [i["text"] for i in feed.feed_read(log, ["rain"])["items"]] == ["Rain 1", "Rain 2"]
