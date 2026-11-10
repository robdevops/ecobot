"""Tools.call: which call drew each chart of a question (Turn.chart_calls), when the model's calls in one step run together."""

import asyncio
import json

from lib.tools import Tool, Tools, Turn

OBJECT = {"type": "object", "properties": {}}


async def test_each_chart_is_put_down_to_the_call_that_added_it_when_calls_run_together():
    """The model's calls in one step are made together (llm gathers them): a call that draws nothing but finishes after another drew
    its chart doesn't claim that chart."""
    drawn = asyncio.Event()

    async def draws(args, turn):
        turn.charts.append("chart")
        drawn.set()
        return "drew"

    async def waits(args, turn):
        await drawn.wait()   # (started first, finishes last)
        await asyncio.sleep(0)
        return "nothing drawn"
    tools = Tools([Tool("waits", "", OBJECT, waits), Tool("draws", "", OBJECT, draws)])
    turn = Turn()
    await asyncio.gather(tools.call("waits", json.dumps({"a": 1}), turn), tools.call("draws", json.dumps({"b": 2}), turn))
    assert turn.charts == ["chart"]
    assert turn.chart_calls == [("draws", {"b": 2})]


async def test_charts_added_outside_any_call_have_no_call():
    async def draws(args, turn):
        turn.charts.append("theirs")
        return "drew"
    tools = Tools([Tool("draws", "", OBJECT, draws)])
    turn = Turn()
    turn.charts.append("ours")
    await tools.call("draws", "{}", turn)
    turn.charts.append("ours too")
    assert turn.chart_calls == [(None, {}), ("draws", {}), (None, {})]
