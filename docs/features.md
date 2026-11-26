# What the servers provide

ecobot-mcp and irrigation-mcp are MCP servers: a client (Claude, LibreChat, signalbot, a script) connects, lists what they offer and uses it. Nothing here is a chat bot; the person asks the client, and the client's model calls the tools.

## Tools a model calls

ecobot-mcp's tools answer in words and figures, with charts where a chart helps (the tool descriptions, which the client's model reads, are the exact reference; [ecobot-mcp.md](ecobot-mcp.md) 3.3 has the design):

- **Now:** `current` gives the live readings (the weather station's outdoor and indoor sensors, the air sensor's particulates, CO₂ and indices), each with its unit and a rating. `about` says what the station has and what each reading is called.
- **Over a period:** `summary` gives a reading's lowest, highest and average over a period ("rain since 6am", "the temperature this week"), and a chart of it; several readings are a panel each, or share a chart with a scale each. `readings` gives any reading at an interval (every minute to every day): "humidity each hour for the last 5 hours", "PM2.5 every 30 minutes this morning". A gap, such as a sensor that was off, is said, never filled in. Rain is the millimetres that fell in each interval.
- **Days and totals:** `days` counts or ranks days by any reading ("how many days was UV 9 or more", "hottest day each year"), or totals rain per month or year as bars.
- **Analysis:** `relate` says whether rain follows a reading; `scan` relates air quality to everything else, over the stored history.
- **Ahead:** `forecast` (the days, with the day's rain in mm, its likely range and how sure it is, from Open-Meteo), `outlook` (will it rain in the next few hours, by the station's own pressure, humidity and wind trends) and `pollen` (grass pollen and thunderstorm asthma for Melbourne, October to December).

Periods are given in words ("past week", "since 6am"): the tools read them in the station's time zone.

**Charts:** any reading or several on one time axis, rain behind the lines, one colour per reading. `DARKMODE=on` draws every picture on a dark background, or only between set times, e.g. `DARKMODE=8pm-8am` ([configuration.md](configuration.md)). A chart comes as data the client can draw, as a PNG (`?charts=png`) or as a link to a PNG (`?charts=link`, for chat apps that only show markdown images); [ecobot-mcp.md](ecobot-mcp.md) 3.7.

## Resources and prompts

- **Resources** a client reads, and listens to for changes: `ecobot://station` (what the station has), `ecobot://now/weather` and `ecobot://now/air` (the live readings), `ecobot://forecast`, `ecobot://pollen`, `ecobot://status` (a picture of the temperature, humidity and rain gauges, with the report under it, written in code), `ecobot://health` and `ecobot://feed` (the alerts, below). irrigation-mcp has `irrigation://feed`.
- **Prompts** a client offers as commands or buttons: `report`, `now`, `will_it_rain`, `weather_chart`, `chart`, `rainfall` and `air`. The words after the command fill the prompt in, in order, the last taking the rest, and the client's model answers it with the tools.

## Web dashboard

Every gauge, with its highest and lowest today, this month and this year, is ecobot-mcp's live web page (`/dashboard` beside `/mcp`, [README](../README.md#web-dashboard)): the wind's, rain rate's, sun's and UV's dials with a thin red hand at today's highest (the wind's: its gust).

## Alerts

ecobot-mcp's alert rules write each alert to its alert log, and `ecobot://feed` serves the unexpired ones in signalbot's feed format, so signalbot posts them to the chats that asked (`/alerts`). A client that listens is told when the feed changes.

- rain starting and stopping (with quiet hours, default midnight to 6 am, summed up when they end)
- gusts over 40 km/h, with the last day's wind chart
- UV index of 10 or more in two readings in a row, with the UV chart
- indoor and outdoor temperatures crossing by more than 1 °C, with the temperature chart
- unhealthy air (two readings in a row), and when it is safe again, with the particulates chart
- grass pollen or thunderstorm asthma risk reaching High (optional)
- a forecast day changing substantially (optional)
- irrigation battery low, and irrigation-mcp pausing watering for rain (optional; `irrigation://feed`)

Every alert carries its text, a link where one belongs (the AirGradient dashboard, the pollen site), and its chart's call, so a client can redraw the chart with the latest readings or for another period. An alert goes stale (rain and gusts after about an hour, a forecast the day it is about): a client back after a gap is sent what is still true, never the morning's rain as news.

## Optional extras

- **Pollen and thunderstorm asthma** for Melbourne (October to December), from melbournepollen.com.au: `MCP_POLLEN=on`.
- **Daily forecast** from Open-Meteo, with the day's rain in mm and how sure it is: `• Friday: 🌦️ Showers. 19–23°C, heavy: ~40 mm (15–60 likely; ≥15 at 90%)`. The figure is Open-Meteo's own day total (to 0.5 mm under 5 mm, 1 mm under 20, 5 mm from 20; "under 1 mm" for a drizzle, nothing on a dry day; "heavy" from 30 mm). The range is the 10th to 90th percentile of the 51 ensemble runs' day totals, shown when wider than the rounding. The last item reads: on a wet day the 10th percentile rounded down to a step (2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100 mm), so `≥15 at 90%` means 90% of the runs give at least 15 mm; on a showery day it is the chance of at least 1 mm (`~3 mm (1–3.5 likely; ≥1 at 80%)`): `MCP_FORECAST=on`.
- **Irrigation controller** (a Tuya smart timer): its own MCP server, irrigation-mcp (README: Irrigation), with tools to read its status, water for 5 to 30 minutes, switch off and pause for 24 to 72 hours. It closes the valve after every run, and a daily check at 18:00 delays watering 24 hours when rain fell or is likely, and raises a battery alert. Set the `IRRIGATION_TUYA_*` variables and run it; ecobot-mcp does not need it.
