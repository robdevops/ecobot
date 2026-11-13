"""intent: the reasoning effort from the words (docs/ecobot-mcp.md 4), the one thing the bot decides about a typed question in code:
every typed question goes to the model (step 6c). The keyboard's and the alerts' charts are fixed calls."""

import pytest

from lib import intent, templates
from lib.alerts import pictures


def test_only_looking_ahead_and_descriptions_reason_a_little():
    assert intent.reasoning_effort("will it rain later?") == "low"
    assert intent.reasoning_effort("what was yesterday like?") == "low"
    assert intent.reasoning_effort("what was this week's high and low") == "none"
    assert intent.reasoning_effort("how much rain fell today") == "none"


def test_asking_to_think_try_or_reason_gives_medium_reasoning_to_any_question():
    for text in ("think about the wind this week", "try to work out why it was humid", "reason it through: hottest day", "thinking harder about yesterday",
                 "how hot was it, think", "what was yesterday like? think", "Try 3m", "estimate the hottest day this week",
                 "predict the average humidity for 30d", "what's your estimate of the week's rain", "an estimation of the wind 7d",
                 "give me a prediction for the week's high", "grind on the hottest day this week", "whirl it around: 7d wind"):
        assert intent.reasoning_effort(text) == "medium", text                      # medium wins over describe's low
    for text in ("what was this week's high and low", "thanks", "a thinner chart please", "I tried that yesterday"):
        assert intent.reasoning_effort(text) == "none", text


def test_how_one_reading_relates_to_another_gets_medium_thinking():
    for text in ("Is there a correlation between pressure and rainfall", "is there a link between humidity and rain",
                 "cross-reference pressure with rain", "cross reference wind and rain", "what's the connection between wind and temperature",
                 "is there a relationship between pressure and rain", "is rain related to pressure", "what is the relation between heat and pm2.5",
                 "does pressure affect the rain", "correlate wind and rain"):
        assert intent.reasoning_effort(text) == "medium", text
    for text in ("what was the pressure last month", "how much rain fell yesterday", "hottest day", "connect to the station"):
        assert intent.reasoning_effort(text) == "none", text


def test_analysis_across_days_or_readings_gets_low_thinking():
    for text in ("what was the hottest day that where it also rained", "how many days over 30 had rain",
                 "hottest day when it also rained?", "days when it was both windy and cold",
                 "how often does it rain on hot days", "was the coldest day also the wettest"):
        assert intent.reasoning_effort(text) == "low", text
    for text in ("what was this week's high and low", "how much rain fell yesterday", "weather 1m",
                 "what's the hottest day this year", "thanks!"):
        assert intent.reasoning_effort(text) == "none", text
    assert intent.reasoning_effort("will it rain later?") == "low"
    assert intent.reasoning_effort("what was yesterday like?") == "low"


def test_one_reasoning_step_down_for_asking_again_after_a_timeout():
    assert [intent.lower_effort(e) for e in ("high", "medium", "low", "none", "odd")] == ["medium", "low", "none", None, None]


def test_every_button_and_alert_chart_is_a_fixed_call():
    """The keyboard's chart buttons and the alerts' charts are fixed calls (no words read: step 6c), the same calls they were read as
    before (Wind's is the gusts alert's)."""
    calls = {**{s: templates.call(s) for s in templates.CALLS}, **{f"alert {kind}": call for kind, call in pictures.CHARTS.items()}}
    assert calls == {
        "Rain chart 1d": ("summary", {"readings": ["rain"], "period": "1d", "chart": True}),
        "Weather chart 1d": ("summary", {"readings": ["weather"], "period": "1d", "chart": True}),
        "Temperature chart 1d": ("summary", {"readings": ["temperature"], "period": "1d", "chart": True}),
        "Humidity chart 1d": ("summary", {"readings": ["humidity"], "period": "1d", "chart": True}),
        "Air quality all metrics 1d": ("summary", {"readings": ["air"], "period": "1d", "chart": True}),
        "Particulates chart 1d": ("summary", {"readings": ["pm2.5", "pm10", "pm1"], "period": "1d", "chart": True}),
        "Wind chart 1d": ("summary", {"readings": ["wind"], "period": "1d", "chart": True}),
        "alert gusts": ("summary", {"readings": ["wind"], "period": "1d", "chart": True}),
        "alert uv": ("summary", {"readings": ["uv"], "period": "1d", "chart": True}),
        "alert temps": ("summary", {"readings": ["temperature"], "period": "1d", "chart": True}),
        "alert particulates": ("summary", {"readings": ["pm2.5", "pm10", "pm1"], "period": "1d", "chart": True})}
    first = templates.call("Rain chart 1d")
    first[1]["period"] = "7d"                                                # a copy each time: the button's own call stays
    assert templates.call("Rain chart 1d")[1]["period"] == "1d" and templates.call("Report") is None and templates.call(None) is None


@pytest.mark.parametrize("text", ["status", "humidity 7d", "how hot is it", "thanks", "will it rain later?"])
def test_the_effort_is_read_for_every_message(text):
    assert intent.effort(text) == intent.reasoning_effort(text)


def test_a_phones_typographic_apostrophe_reads_as_a_plain_one():
    assert intent.effort("what’s yesterday been like?") == intent.reasoning_effort("what's yesterday been like?") == "low"
