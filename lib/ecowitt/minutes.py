"""The minute readings from Home Assistant, kept so a short chart is drawn without asking it.

A chart of up to MINUTE_HOURS is drawn a reading a minute (Ecowitt's cloud has no such cycle). Rather than ask Home Assistant for them at every
chart, the bot takes them as they come (at start for the last KEEP_HOURS, then at each new live reading: source.pull_minutes) into the
storage's 1min rows, where they are kept ten days (storage.write.MINUTES_KEPT).
"""

MINUTE_HOURS = 8                     # a chart of up to this long is drawn from minutes
KEEP_HOURS = MINUTE_HOURS + 1        # ... and may end an hour ago: what is older is no use
SLACK_SECONDS = 300                  # the first minute held may be this much after a chart's start (the chart's first point is then a little late)
