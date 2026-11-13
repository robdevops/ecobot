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


def test_a_long_source_is_cut_to_one_line():
    text = source_text("https://example.com/" + "x" * 100)
    assert len(text) == SOURCE_CHARS and text.endswith("…") and source_text(None) == ""


def test_the_source_is_drawn_only_when_there_is_one():
    chart = chart_samples.counts()
    assert chart.source == "" and render(chart, TZ)
    chart.source = "Ecowitt weather station"
    assert render(chart, TZ) != render(chart_samples.counts(), TZ)
    assert all("Source" not in t.text for t in draw_texts(chart, TZ))   # not one of the recorded value labels
