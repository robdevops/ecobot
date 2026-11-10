"""The service's tools (docs/mcp-service.md 3.2 and 3.3): the ten that will replace the bot's twelve, each in a module of its own, on
what every one of them shares:

  - common.py: how a tool is made, what it is sent (its arguments and the request's metadata, nothing else), and the shape of what it
    answers: the period it read, the time of its figures, its charts, and its refusals;
  - names.py: the readings the service has, one name each (`outdoor.temperature`, `air.pm2_5`), and the words people use for them;
  - daylook.py: a day's look ("wet", 🌧️) from its figures.

Nothing here is offered to a model yet, and nothing here knows of Telegram, of the bot's buttons or of who draws a chart.
"""
