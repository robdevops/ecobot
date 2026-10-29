"""The chart that goes with an alert: the same picture a typed request for it would give ("Wind chart 1d" is what the Wind button sends), so what an
alert shows is defined once, by the button sentences and lib/intent."""

import asyncio
import logging

from .. import intent
from ..charts import render
from ..timeutil import now_local
from ..tools import Turn

log = logging.getLogger(__name__)

# The alert types that carry a chart, and the request it answers: what the alert is sent with, and what its Refresh draws again
CHARTS = {"gusts": "Wind chart 1d", "uv": "UV chart 1d", "temps": "Temperature chart 1d", "particulates": "Particulates chart 1d"}


async def chart(source, text: str, tz) -> bytes | None:
    """The PNG for the request `text`, made from `source`'s tool the way the bot would answer it; None when it cannot be made (the alert then
    goes out as text). `source` is the station or the air sensor: the tool is found in its `tools` by the name the request resolves to."""
    try:
        read = intent.read(text, now_local(tz))
        tool, args = read.fast[0], read.fast[1]
        turn = Turn.of(read, text)
        handler = next(t for t in source.tools if t.name == tool).handler
        await handler(args, turn)
        return await asyncio.to_thread(render, turn.charts[0], tz)
    except Exception as e:
        log.warning("The chart for the alert (%s) couldn't be made: %s: %s", text, type(e).__name__, e)
        return None
