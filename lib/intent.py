"""How much the model should think about a typed question, decided from its words (docs/ecobot-mcp.md 4): only predictions, analysis
and "describe it" questions get reasoning, and a question that asks the bot to think gets more. Nothing else is decided in code: every
typed question goes to the model (step 6c), and the words go on as they are.
"""

import re

I = re.IGNORECASE

# ---------- reasoning ----------
EFFORT_DEFAULT, EFFORT_DESCRIBE, EFFORT_THINK = "none", "low", "medium"
EFFORT_STEPS = ("none", "low", "medium", "high")


def lower_effort(effort: str) -> str | None:
    """One reasoning step down (medium > low > none), for asking again after a timeout; None when there is no lower step."""
    i = EFFORT_STEPS.index(effort) if effort in EFFORT_STEPS else 0
    return EFFORT_STEPS[i - 1] if i else None


# Describing a day or playing along with a hypothetical benefits from a little thinking
DESCRIBE = re.compile(
    r"\b(what\b.*\blike|describe\w*|imagine\w*|picture|pretend|feel like|felt like|"
    r"if (we|i|you|someone|anyone) (were|was|had|lived))\b", I)

# Looking ahead, not weather words alone ("how much rain fell" is a lookup)
FORECAST = re.compile(
    r"\b(will it|will there|is it going to|going to (rain|be|get|stay)|gonna|forecast\w*|umbrella|"
    r"tonight|tomorrow|later|soon|coming|next (few )?(hour|hours)|this (afternoon|evening)|"
    r"should i|chance of|likely|expect\w*|predict\w*|outlook)\b", I)

# Combining readings across days or conditions ("the hottest day that also rained", "how many days ...")
ANALYSIS = re.compile(
    r"\b(also|both|same day|at the same time|coincid\w*|combination|how many days|how often|"
    r"(days?|times?|when) (when|that|where|with|it)|(that|where|when) (it )?(also|and)|"
    r"(hottest|coldest|wettest|driest|windiest|warmest|coolest)\b.*\b(that|where|when|but|and)\b)", I)

# Is one reading tied to another ("is there a correlation between pressure and rain"): the answer is a judgement
LINK = re.compile(
    r"\b(correlat\w*|(link|links|linked|connection|connected|relation|relationship|relationships|relate|related|relates)\b"
    r".*\b(between|to|with)\b|(between|with)\b.*\b(link|connection|relation\w*)|cross[- ]?referenc\w*|cross[- ]?check\w*|"
    r"(does|do|did|is|are)\b.*\b(affect\w*|influence\w*|predict\w*|cause\w*)\b.*\b(rain|pressure|humidity|wind|temperature)\w*)\b", I)

# Asking the model to think: "think about it", "try to work out why", "reason it through", "predict", "estimate", "grind", "whirl"
THINK = re.compile(r"\b(think\w*|try|trying|reason\w*|predict\w*|estimat\w*|grind\w*|whirl\w*)\b", I)

EFFORT_RULES = ((EFFORT_THINK, (LINK, THINK)), (EFFORT_DESCRIBE, (FORECAST, ANALYSIS, DESCRIBE)))


def reasoning_effort(text: str) -> str:
    """Thinking is for judgement calls only: how one reading relates to another (medium), and looking ahead,
    analysis across days or readings or describing a day (low). Lookups get none, unless the person asks the bot to think,
    try, reason, predict, estimate, grind or whirl (medium)."""
    return next((effort for effort, patterns in EFFORT_RULES if any(p.search(text) for p in patterns)), EFFORT_DEFAULT)


APOSTROPHES = str.maketrans("’‘", "''")   # a phone's typographic apostrophes ("what’s"), read as the plain one every pattern uses


def effort(text: str) -> str:
    """reasoning_effort of a typed question, its typographic apostrophes read as plain ones."""
    return reasoning_effort(text.translate(APOSTROPHES))


__all__ = ["EFFORT_DEFAULT", "effort", "lower_effort", "reasoning_effort"]
