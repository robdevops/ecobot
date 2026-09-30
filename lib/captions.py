"""When a chart is drawn, and what the model is told about it: a tool that made a chart adds one of these hints to its
result, so the reply becomes a good caption for the picture (see specs.py for the chart itself, charts.py for drawing it)."""

from datetime import datetime

CHART_MIN_DAYS = 3  # a period of this many calendar days or more always gets a chart


def wants_chart(args: dict, turn, start: datetime | None = None, end: datetime | None = None) -> bool:
    """The model asked for one, the person's words did (turn.chart_asked), or the period (naive local start/end) spans 3+ days."""
    long = bool(start and end and (end.date() - start.date()).days >= CHART_MIN_DAYS - 1)
    return bool(args.get("chart")) or turn.chart_asked or long


# Added to a tool result when a chart was made, so the reply becomes a good caption
CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
              "per series with its high and low, or its average if that is what was asked (for weather, one line each for Outdoor and Indoor when both were "
              "fetched; for air quality, the peak with its ready-made rating copied exactly, emoji included: \"high_rating\"). No other lists or breakdowns; don't mention or describe the chart.")

AVERAGE_CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
                      "per series (Outdoor and Indoor when both were fetched) with its AVERAGE, copied from the series' "
                      "\"average\" field, and its low and high in brackets. Lead with the average: that is what was asked. "
                      "Don't mention or describe the chart.")
LINK_CHART_HINT = ("Your reply becomes the caption of a chart with the reading as a line and rain as bars behind it, so keep "
                   "it short: the period, then the finding in one or two lines (how much of the rain fell while the reading "
                   "was falling, and the correlation), citing the numbers. Don't mention or describe the chart.")
STACK_CHART_HINT = ("Your reply becomes the caption of a chart with these readings on one time axis, so keep it short: the "
                    "period, then one line per reading: temperature and other readings with their high and low (or their "
                    "average if that was asked), rain with its total (\"rain_total_mm\"). Don't mention or describe the chart.")
COMPOSED_CHART_HINT = ("Your reply becomes the caption of a chart of these readings on one time axis, so keep it short: the "
                       "period, then what the figures show about how they relate. Don't mention or describe the chart.")
DIRECTION_CHART_HINT = ("Your reply becomes the caption of a chart of wind: average speed with the gusts, and a compass of "
                        "where the wind came from. Keep it short: the period, the average speed and strongest gust, then the "
                        "most common direction and how steady it was. Don't mention or describe the chart.")
