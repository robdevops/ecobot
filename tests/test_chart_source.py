"""The source line under a chart: the model names it through a chart tool's `source` argument, else the tool's own is used."""

import asyncio
import json

from lib.charts import draw_texts, render
from lib.specs import CHART_SOURCE, SOURCE_CHARS, Chart, source_text
from lib.tools import Tool, Tools, Turn
from tests import chart_samples
from tests.fakes import TZ


def drawer(delay: float = 0.0):
    async def handler(args, turn):
        await asyncio.sleep(delay)
        turn.charts.append(Chart("T", "", chart_samples.temperature().panels))
        return "ok"
    return handler


def test_only_chart_tools_offer_a_source_argument():
    tools = Tools([Tool("plain", "", {"type": "object", "properties": {}}, drawer()),
                   Tool("charts", "", {"type": "object", "properties": {"chart": {"type": "boolean"}}}, drawer(), source="Station")])
    params = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in tools.schemas}
    assert "source" not in params["plain"] and set(params["charts"]) == {"chart", "source"}


async def test_the_models_source_wins_else_the_tools_own():
    tools = Tools([Tool("charts", "", {"type": "object", "properties": {}}, drawer(), source="Ecowitt weather station")])
    turn = Turn()
    await tools.call("charts", json.dumps({"source": "  ecowitt.net\n"}), turn)
    await tools.call("charts", "{}", turn)
    assert [c.source for c in turn.charts] == ["ecowitt.net", "Ecowitt weather station"]
    assert CHART_SOURCE.get() == ""   # nothing left behind for the next chart


async def test_calls_made_together_keep_their_own_sources():
    tools = Tools([Tool("slow", "", {"type": "object", "properties": {}}, drawer(0.05), source="Ecowitt weather station"),
                   Tool("fast", "", {"type": "object", "properties": {}}, drawer(0.0), source="AirGradient sensor")])
    turn = Turn()
    await tools.call_all([("slow", {}), ("fast", {})], turn)
    assert [c.source for c in turn.charts] == ["AirGradient sensor", "Ecowitt weather station"]


def test_a_long_source_is_cut_to_one_line():
    text = source_text("https://example.com/" + "x" * 100)
    assert len(text) == SOURCE_CHARS and text.endswith("…") and source_text(None) == ""


def test_the_source_is_drawn_only_when_there_is_one():
    chart = chart_samples.counts()
    assert chart.source == "" and render(chart, TZ)
    chart.source = "Ecowitt weather station"
    assert render(chart, TZ) != render(chart_samples.counts(), TZ)
    assert all("Source" not in t.text for t in draw_texts(chart, TZ))   # not one of the recorded value labels
