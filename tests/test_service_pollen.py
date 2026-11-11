"""The `pollen` tool (lib/service/pollen.py): the pollen source's lines, each level with its emoji and its day, its text, what is held
when the request says not to fetch, and the off-season answer. (Until step 6c also held to pollen_asthma's answers and the bot's old
words, tests/old_tools.py and tests/report_words.py, deleted with them.)"""

import json
from datetime import date, datetime, time as dtime, timedelta

from lib.pollen.source import OFF_SEASON
from lib.service.common import FRESH
from lib.service.context import Context
from lib.service.pollen import DESCRIPTION, PARAMETERS, tools
from tests.fakes import TZ
from tests.test_pollen import SEASON_DAY, make, page


def got(text: str) -> dict:
    return json.loads(text)


def context(pollen, day: date = SEASON_DAY, hour: int = 12) -> Context:
    """The context the tool is built over, its clock at `hour` on `day` (the pollen source's own clock says the same, make())."""
    return Context(pollen=pollen, tz=TZ, clock=lambda: datetime.combine(day, dtime(hour), tzinfo=TZ).timestamp())


def tool(ctx):
    (it,) = tools(ctx)
    return it


async def test_the_lines_are_pollen_asthmas_and_each_level_has_its_emoji_and_day(tmp_path):
    pollen, calls = make(tmp_path, lambda: page(grass="High", asthma="Moderate"))
    pollen_tool = tool(context(pollen))
    assert pollen_tool.name == "pollen" and pollen_tool.annotations == {"readOnlyHint": True, "openWorldHint": True}
    out = got(await pollen_tool.call({}))
    assert len(calls) == 1   # nothing held: fetched, as pollen_asthma does
    assert out["lines"] == pollen.lines() == ["Grass pollen forecast: 🔴 High", "Thunderstorm asthma forecast: 🟠 Moderate"]
    assert out["grass"] == {"level": "High", "emoji": "🔴", "day": "2026-11-10", "day_text": "Tue 10 Nov 2026"}
    assert out["asthma"] == {"level": "Moderate", "emoji": "🟠", "updated": "1 Oct 2026 4:00pm"}
    assert out["in_season"] is True and out["season"] == "October to December" and out["scale"] == ["Low", "Moderate", "High", "Extreme"]
    assert out["place"] == pollen.place and out["source"].startswith("melbournepollen.com.au") and out["fresh"] == FRESH and "period" not in out
    fetched = datetime.fromtimestamp(int(pollen.fetched_at), TZ)
    assert out["fetched_at"] == out["as_of"]["time"] == fetched.isoformat()
    assert "Central" not in json.dumps(out)   # (the district is never named, as in pollen_asthma's)
    await pollen.close()


async def test_its_text_is_its_heading_and_a_bullet_per_line(tmp_path):
    pollen, _ = make(tmp_path, lambda: page(grass="Low", asthma="Low"))
    out = got(await tool(context(pollen)).call({}))
    assert out["text"].startswith("Pollen & asthma (") and out["text"].endswith("\n• Grass pollen forecast: 🟢 Low\n• Thunderstorm asthma forecast: 🟢 Low")
    later = SEASON_DAY + timedelta(days=1)
    assert got(await tool(context(pollen, later)).call({}))["text"].endswith(out["text"].split("\n", 1)[1])
    await pollen.close()


async def test_a_forecast_for_another_day_says_its_day_and_a_missing_asthma_level_is_said_so(tmp_path):
    pollen, _ = make(tmp_path, lambda: page(grass="Moderate", day=SEASON_DAY + timedelta(days=1), season=False))
    out = got(await tool(context(pollen)).call({}))
    assert out["lines"] == pollen.lines() == ["Grass pollen forecast: 🟠 Moderate (forecast for Wed 11 Nov)"]
    assert out["grass"]["day"] == "2026-11-11" and out["asthma"] is None
    assert out["asthma_note"] == "The page had no thunderstorm asthma level for this district when it was last read."
    await pollen.close()


async def test_with_fetch_false_it_answers_from_what_is_held_and_fetches_nothing(tmp_path):
    pollen, calls = make(tmp_path)
    out = got(await tool(context(pollen)).call({}, {"fetch": False}))
    assert out == {"error": "No pollen levels are available right now."} and calls == []
    await pollen.warm(True)
    calls.clear()
    held = got(await tool(context(pollen)).call({}, {"fetch": False}))
    assert held["lines"] == pollen.lines() and calls == []
    await pollen.close()


async def test_out_of_season_it_gives_pollen_asthmas_answer_and_fetches_nothing(tmp_path):
    pollen, calls = make(tmp_path)
    pollen.now = lambda: datetime(2026, 1, 15, 12, 0)
    out = await tool(context(pollen, date(2026, 1, 15))).call({})
    assert got(out) == {"error": OFF_SEASON} and calls == []
    await pollen.close()


def test_it_is_offered_only_with_the_pollen_source_and_its_description_says_what_it_is_not_for():
    assert tools(Context(tz=TZ)) == []
    assert len(DESCRIPTION) < 1500 and "forecast" in DESCRIPTION and "October to December" in DESCRIPTION and "{}" in DESCRIPTION
    assert PARAMETERS == {"type": "object", "properties": {}}
