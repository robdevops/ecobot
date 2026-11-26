# ecobot

Two MCP servers for your Ecowitt weather station and AirGradient air sensor, and for an irrigation controller:

- **ecobot-mcp** reads the sources, keeps their history, runs the alert rules, draws the charts and serves the lot over MCP (tools, resources, prompts) to any MCP client: Claude, LibreChat, [signalbot](#other-mcp-clients) and others. It also serves a live [web dashboard](#web-dashboard).
- **irrigation-mcp** (optional) is the Tuya irrigation controller's own MCP server: its tools, its valve watchdog and its daily rain check.

**Data sources** (each one is optional)
- AirGradient air quality, from the sensor on your network, from AirGradient's cloud, or both
- Ecowitt weather station, through Ecowitt's cloud or Home Assistant
- Open-Meteo weather forecast
- Melbourne Pollen: pollen and thunderstorm asthma forecast

**Outputs**
- Tuya irrigation controllers

## Contents

- [Features](#features)
- [Install](#install)
  - [systemd](#systemd)
  - [Docker](#docker)
    - [Docker logging](#docker-logging)
    - [Docker networking](#docker-networking)
  - [Irrigation](#irrigation)
- [Other MCP clients](#other-mcp-clients)
  - [signalbot: the alert feeds](#signalbot-the-alert-feeds)
  - [Charts in LibreChat](#charts-in-librechat-and-other-chat-apps-chartslink)
- [Web dashboard](#web-dashboard)
- [Upgrading](#upgrading)
  - [From the Telegram bot](#from-the-telegram-bot)
  - [Renamed settings](#renamed-settings)
  - [systemd](#systemd-1)
  - [Docker](#docker-1)
- [Configuration](#configuration)

## Features

- **Ten tools** a model calls to answer in plain words: the readings now (`current`), over any period at any interval (`readings`), highs and lows and charts (`summary`), days counted or ranked by any reading (`days`), whether one reading follows another (`relate`), air quality against everything (`scan`), the forecast, the outlook and pollen (`forecast`, `outlook`, `pollen`), and what the station has (`about`).
- **Charts:** any reading or several on one time axis, rain behind the lines, drawn as data a client can render, or as pictures (`?charts=png`), or as links to pictures (`?charts=link`, for chat apps that only show markdown images).
- **Resources:** the station's catalogue, the live weather and air readings, the forecast, the pollen, a Status picture of the temperature, humidity and rain gauges, and each server's health, pushed to a client that listens when they change.
- **Prompts:** `report`, `now`, `will_it_rain`, `chart`, `rainfall` and more, for a client that offers them as commands.
- **Alerts:** rain, strong gusts, high UV, unhealthy air, temperature changes, pollen and a revised forecast, written to an alert log and served as a feed (`ecobot://feed`) that signalbot posts to its chats.
- **Web dashboard:** the gauges as a live page in any browser on your network, laid out to fill a phone, a tablet or a screen.
- **Irrigation:** water or pause a Tuya controller from any MCP client, with the controller paused automatically when rain has fallen or is forecast.

## Install

Already installed? See [Upgrading](#upgrading).

Start by putting your keys in one private file:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
```

```
sudoedit /etc/ecobot.env
```

```
# Weather station (optional)
MCP_ECOWITT_API_KEY=
MCP_ECOWITT_APP_KEY=

# AirGradient sensor (optional)
MCP_AIRGRADIENT_API_TOKEN=
MCP_AIRGRADIENT_LOCATION_ID=
```

At least one of the two sources is needed. Settings that start with `MCP_` are read by ecobot-mcp, the `IRRIGATION_` ones by
irrigation-mcp ([Irrigation](#irrigation), optional); the few without any, such as `TZ`, by both.

Every request to ecobot-mcp needs a token. No setting is needed: at its first start it makes one and keeps it in the data folder
(`/var/lib/private/ecobot/auth_token`; `sudo cat` it for a client). To choose your own instead, set `AUTH_TOKENS=<token>` (one per
client, separated by commas).

### systemd

Needs Python 3.13. ecobot-mcp (`python -m ecobot_mcp`) serves MCP at `http://127.0.0.1:8765/mcp`; irrigation-mcp has a unit of its own
([Irrigation](#irrigation)). Both run from one checkout and one venv.

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
sudo python3.13 -m venv /opt/ecobot/.venv
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt -r /opt/ecobot/requirements-charts.txt
```

```
sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```
sudo systemctl enable --now ecobot-mcp.service
```

Watch the log with `journalctl -fu ecobot-mcp` (warnings and errors alone: `journalctl -p warning -u ecobot-mcp`). It runs sandboxed as
a user of its own and keeps its data in `/var/lib/private/ecobot`. The token for [other MCP clients](#other-mcp-clients):
`sudo cat /var/lib/private/ecobot/auth_token`.

### Docker

Docker runs each program from its own image built from this repository: `ecobot-mcp`, and, with an irrigation controller,
`irrigation-mcp` ([Irrigation](#irrigation)).

```
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot
```

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
cd /opt/ecobot
```

```
sudo docker compose up -d --build
```

Watch the log: `sudo docker logs -tf ecobot-mcp` shows `MCP: http://...:8765/mcp`. The token is in the data folder:
`sudo cat /var/lib/private/ecobot/auth_token`.

Docker uses the same data folder as the systemd units, so you can switch between them, one at a time: `sudo docker compose down`, then
`sudo systemctl start ecobot-mcp`. Only one ecobot-mcp runs on the data folder: a second stops at start, saying so in its log.

#### Docker logging

Logs go to Docker's own rotated log; to send them to the journal instead (kept across upgrades, read with `journalctl -f CONTAINER_NAME=ecobot-mcp`):

```
echo "COMPOSE_FILE=compose.yaml:compose.journald.yaml" | sudo tee -a /opt/ecobot/.env
```

The servers write warnings and errors to stderr and the rest to stdout, which Docker files in the journal at the priorities err and info, so `journalctl -p warning CONTAINER_NAME=ecobot-mcp` shows the warnings and errors alone.

To use both this and [Docker host network](#docker-networking), set `COMPOSE_FILE=compose.yaml:compose.host-network.yaml:compose.journald.yaml`.

#### Docker networking

`.local` names (mDNS) can't be found from Docker's private network: use IP addresses for config values, or put the containers on the host's network (then run the `docker compose` command again):

```
echo "COMPOSE_FILE=compose.yaml:compose.host-network.yaml" | sudo tee -a /opt/ecobot/.env
```

`/opt/ecobot/.env` holds one `COMPOSE_FILE=` line: to use more than one of these files, list them all in it, separated by `:`.

### Irrigation

Optional, for a Tuya smart irrigation timer. The controller has its own program, **irrigation-mcp** (`python -m irrigation_mcp`): an
MCP server on port 8766 with the tools `irrigation_status`, `water`, `stop` and `delay`, which also closes the valve after every run
(even across a restart) and checks once a day, at 18:00, whether to pause watering for rain (asking ecobot-mcp for the rain measured and
the forecast) and whether the battery is low. Its alerts (a low battery, watering paused for rain) are its feed, `irrigation://feed`
([signalbot](#signalbot-the-alert-feeds) reads it). It accepts the same tokens as ecobot-mcp.

Add the controller's Tuya cloud keys to `/etc/ecobot.env` ([docs/configuration.md](docs/configuration.md#irrigation-mcps-settings-irrigation_)):

```
IRRIGATION_TUYA_DEVICE_ID=
IRRIGATION_TUYA_CLIENT_ID=
IRRIGATION_TUYA_CLIENT_SECRET=
```

**systemd:** install and start its unit beside ecobot-mcp's:

```
sudo cp /opt/ecobot/irrigation-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```
sudo systemctl enable --now irrigation-mcp.service
```

Its log: `journalctl -fu irrigation-mcp`.

**Docker:** add the `irrigation` profile to `/opt/ecobot/.env` (`COMPOSE_PROFILES=irrigation`), then `sudo docker compose up -d --build`.
Its log: `sudo docker logs -tf irrigation-mcp`. Leave `IRRIGATION_ECOBOT_MCP_URL` out of `/etc/ecobot.env`: Docker sets it to the
ecobot-mcp container. A client on the project's network reaches it at `http://irrigation-mcp:8766/mcp`.

## Other MCP clients

ecobot-mcp is published on port 8765 of every address your machine has under Docker (under systemd it listens on 127.0.0.1 unless
`MCP_BIND=0.0.0.0:8765` is set), so other devices on your network (Claude Code on a laptop, say) can use it with a token: the one it
generated (`sudo cat /var/lib/private/ecobot/auth_token`), or, better, one each: set `AUTH_TOKENS=<one token>,<another token>` in
`/etc/ecobot.env` (made with `openssl rand -hex 16`; a token taken out is refused from the next start). Add your machine's IP address or
name, as they write it in the URL, to `MCP_ALLOWED_HOSTS`, e.g. `http://192.168.1.5:8765/mcp` needs `192.168.1.5`. For Claude Code:
`claude mcp add --transport http ecobot http://192.168.1.5:8765/mcp --header "Authorization: Bearer <token>"`. It is plain HTTP: keep it
on your own network. Docker's published ports go around ufw's rules. To keep it to this machine only, change `8765:8765` in
`compose.yaml` to `127.0.0.1:8765:8765`. A client that shows pictures can have the charts as images: add `?charts=png` to its URL.

### signalbot: the alert feeds

ecobot-mcp writes every alert to its alert log and serves the unexpired ones as `ecobot://feed`; irrigation-mcp does the same as
`irrigation://feed`. Both are in signalbot's feed format: signalbot reads each feed and posts
new alerts to the chats subscribed with `/alerts`, each with its link and, where it has one, a chart it can redraw for another period.
A client that listens (MCP's `subscriptions/listen`) is told when a feed changes; signalbot reads it every 30 seconds. On the same
host signalbot reads the token from the data folder, or is given one of `AUTH_TOKENS`.

### Charts in LibreChat (and other chat apps): `?charts=link`

Some clients (LibreChat, with an Ollama endpoint, say) don't show an image a tool sends, but do show a markdown image in the model's
reply. For those, add `?charts=link` to the MCP URL (`https://api.example.org/ecobot/mcp?charts=link`): each chart then comes as a
link to its picture, `/charts/<id>.png` on ecobot-mcp's port, and the model is asked to put it in its reply as a markdown image. Your
browser loads the picture, so the link has to be one it can reach: set `MCP_CHART_BASE_URL` to where your reverse proxy serves
`/charts`, e.g. with nginx

```
location /ecobot-charts/ { proxy_pass http://127.0.0.1:8765/charts/; }
```

and `MCP_CHART_BASE_URL=https://api.example.org/ecobot-charts`. Unset, the link is built from the address the MCP request came to
(the proxy's `X-Forwarded-Proto` and `X-Forwarded-Host` when it sends them) with `/charts`. The pictures need no token and answer any
`Host`: anyone who has a link can open it until it expires, so treat links as you would the chart itself. Each link's id is random
and unguessable; the pictures are kept in memory for `MCP_CHART_LINK_HOURS` (24 by default), at most 500 of them and 64 MB in all
(the oldest go first), and a restart of ecobot-mcp forgets them all (an old link then answers 404).

## Web dashboard

ecobot-mcp also serves the station's and the air sensor's gauges as a live web page at `/dashboard` on its port, e.g. `http://deb1:8765/dashboard`
(your machine's name or IP address). It shows every gauge with the day's, month's and year's highs and lows, drawn by the browser (plain HTML5, nothing fetched from
the internet), and fits every tile on the screen in portrait or landscape where they stay readable (on a phone it scrolls). It
updates by itself when a new reading arrives, and says "reconnecting…" while ecobot-mcp is away. This page is the dashboard.

Temperature, humidity, pressure, rain, UV index and the AQI come first; then the weather station's other gauges, then the air
sensor's (when there is one). The air sensor's bands are on US standards:

- **AQI** (the tile's title; it is the US AQI, which ecobot-mcp's tools still call it), the higher of PM2.5's and PM10's (as AirNow gives it; "(PM10)" after the category when PM10 sets it), on a 0-500 dial with the EPA's six categories and colours (Good 0-50 green, Moderate 51-100 yellow,
  Unhealthy for Sensitive Groups 101-150 orange, Unhealthy 151-200 red, Very Unhealthy 201-300 purple, Hazardous 301-500 maroon), the
  category's name under the number;
- **PM2.5** (µg/m³) on the EPA's 2024 breakpoints (9.0, 35.4, 55.4, 125.4, 225.4) and **PM10** on the EPA's (54, 154, 254, 354,
  424), in the same colours; **PM1** on PM2.5's (it has no standard of its own, and is part of PM2.5);
- **CO₂** (ppm): there is no EPA standard, so the usual US indoor guidance (from ASHRAE's): under 800 good, 800-1000 fair, 1000-1500
  poor, 1500-2000 bad, 2000 and over very bad;
- **VOC index** and **NOx index**, when the monitor reports them (Sensirion's indices, learnt from its own air: VOC 100 is its
  average, NOx 1 its baseline): VOC under 100 low, 100-150 average, then elevated, high (250) and very high (400); NOx up to 20 low,
  then elevated, high (150) and very high (300).

On the air dials every category gets an equal share of the dial (as AirNow's AQI dial draws them), so "good" and "moderate" aren't
squeezed into a sliver of a scale that runs to 500; the numbers round the dial are the breakpoints. The monitor's own temperature and
humidity repeat the station's outdoor ones, so they have no gauge. The AQI and each category's name are of the reading now, so they
always agree with the needle and with the PM2.5 and PM10 dials (the alerts and ecobot-mcp's words rate PM2.5 and PM10 on the last two
readings, so a passing spike isn't called poor). The page's data never asks the air sensor: with its reading
stale it shows the one it holds.

The Wind direction tile has a table under its compass, headed Direction: the compass point the wind came from most often today,
this month and this year (N, NNE, NE ... NNW, in the station's time zone, kept in the same grey Daily, Monthly and Yearly rows). Each stored
reading counts once in its 16th of the compass (22.5 degrees, N being 348.75 to 11.25); readings of a wind under 1 km/h are left out, as they have no
direction; a tie goes to the point nearest the latest reading's direction, so the answer does not flip back and forth; a dash means
nothing left.

A tile whose reading is missing just now (the station or the air sensor not sending it) is dimmed, its gauge at its floor (0, or the
scale's low end where 0 isn't on it: pressure 950 hPa, CO₂ 400 ppm) with its highs and lows still beside it; when the reading comes
back the tile brightens and its gauge rises in place. A reading the station or the monitor has never sent has no tile.

The tiles can be arranged to taste. Drag a tile by its gauge to move it (with a mouse, from anywhere on it but its title, its reading
and its table, which stay selectable for copying; on a touch screen, a long press picks it up, with a short buzz on Android); the
others make room. Its ×, shown on mouseover (or after a long press), hides it, as does dropping it on the "remove" zone that slides
down from the top while dragging. With a tile hidden, "reset preferences" beside the time at the bottom brings every tile back in the
usual order (and the units below back to metric). Fewer tiles grow to fill the window (a single one fills it), never smaller than the whole dashboard's. The arrangement
is kept in the browser (its local storage, per browser and device); nothing is sent to ecobot-mcp. A gauge added later takes its
usual place among yours.

Four small switches at the bottom right, left of the time (just above it on a phone), choose the units: "°C | °F" for every
temperature tile (Temperature, Dew point, Feels like, Indoor temperature), "km/h | mph" for the Wind's, "hPa | inHg" for Atmospheric
pressure and "mm | in" for Rain and Rain rate (mm/h becomes in/h), the one in use lit (buttons, so Tab and Enter or Space work too).
The tile's reading, its scale and the numbers on it (0-40 °C becomes 32-104 °F, 0-100 km/h 0-60 mph, 950-1050 hPa 28-31 inHg, the
rain's 0-100 mm 0-4 in and its rate's 0-50 mm/h 0-2 in/h), its Daily, Monthly and Yearly figures, its unit and the red hand (the
Wind's gust, the rain rate's high) all change in place, with the same decimals (two in inHg and inches); the pressure's arrow (after
its reading: the WMO 3-hour tendency, up and blue when risen, down and red when fallen, by size: a short arrow for 0.1 to 1.5 hPa, the normal one for 1.6 to 3.5, a long one for 3.6 to 6.0, a double one over 6.0, none when steady (under 0.1 hPa) or with no reading about 3 hours old) points the same way. ecobot-mcp still sends metric and the browser converts. The wind's direction stays in degrees. Each browser keeps its
choice with the arrangement, and "reset preferences" (shown while any is not metric) puts all four back to metric. The weather
behind the gauges goes by the metric readings whatever is shown (heat from 30 °C, leaves above 15 km/h, the flood from a depth of 0.05 of full in the server's leaky bucket over the past six hours' rain, the haze's opacity from the PM2.5 itself).

A click or tap on a gauge (or Enter or Space on a tile reached with Tab) zooms its title and gauge (its table hidden until you go
back) to fill the window, a single tile shown too. Zoomed, page through the tiles like a photo gallery, each still live: ← and →, Space (next) and Backspace (previous), a
swipe, a drag on the gauge, or a two-finger sideways swipe on a trackpad; it stops at the first and the last (with one tile, paging just nudges it and back). A click or tap (not on the text) or Esc goes back.

Behind the gauges, the weather is drawn as faint pixel art while the readings call for it: rain while it rains (falling straight down in no wind, and slanting with the live wind's speed, whatever the wind effect: by drop size: the three layers are size classes of the rain's median drop (0.9 R^0.21 mm for a rain rate of R mm/h, Marshall-Palmer: far 0.6, mid 1, near 1.5 times it), each leaning atan(wind speed / its terminal speed) (Gunn-Kinzer: 9.65 - 10.3 exp(-0.6 D) m/s), so at 25 km/h and a moderate 5 mm/h the small far drops lean about 66 degrees and the large near ones 48, heavy rain leans less than drizzle, at most 70 from vertical, a gust adding a little, eased over a few seconds; each layer falls at its own vertical speed, following its terminal speed (drizzle slower, a downpour faster), whatever the wind, so the steeper the wind the faster they go along their path and the longer their streaks (up to 1.6 times the calm length), the same number landing; the heavy rain's beads on the glass grow with the drops, 2.6 px in drizzle to 3 in a cloudburst), a toxic smog veil (a static baked image of streaming tendrils and banks, tinted from grey-yellow to dark maroon-brown with the band, panned sideways only while it blows: about 0.4 px/s at 3 km/h to 3 px/s at 40, perfectly still in calm air) from the US
AQI's Moderate band up, UV beams from a UV index of about 6.2 (where the violet first shows), full at 11 (twelve thick violet beams of medium strength all round the sun, all alike, each running on to the window's edge or down to the flood's surface and fading gradually with the distance (to about 5 % of its strength at two thirds of the window's height at UV 11, with a faint tail beyond, a smooth violet tint with no speckle, over a shorter distance at a lower UV index), where they overlap still violet and only at their roots next to the sun pink-white, inside a wide pink halo, the whole fan sweeping slowly and rigidly back and forth about 12 degrees either way every 80 s with each beam also swaying a couple of degrees on its own, so the spacing never opens a gap, the same at every place of the sun; the sun's place and the day's sunrise and sunset are worked out from the station's latitude and longitude, or the forecast's MCP_FORECAST_LAT and MCP_FORECAST_LON, and its time zone's clock, with no library, the sunrise-to-sunset curve alone when the station has no location; sunrise and sunset themselves are worked out from the same latitude and longitude by the sunrise equation, not taken from the forecast, and 06:00 to 18:00 applies only with no location), the heat's sun (from 30 °C: below), the glass fogging over at 90 % humidity (heavier to 100 %), beads condensing in the fog and running down it, wiping clear tracks, leaves blowing past
above 15 km/h of wind (the haze's pan, leaves, rain, flood waves, the frog and the gust's cow, barn or chicken (solid, in front of the sun's beams, never dimmed by the other effects' cap) all going the wind's way: leftward for a wind direction reading from the east side, 0 to 180 degrees, rightward from the west side, 180 to 360; due north or south or no reading keeps the way it was, leftward when the page opens; a change eases over a few seconds: the eddies in the wind keep swirling the same way throughout, and a leaf going round in one veers round in a U in front of it, up on one change and down on the next, over about nine seconds, instead of stopping and reversing), the heat: from 30 °C a small blinding white sun on a clean arc along the strip above the top row of tiles, the bottom layer of the weather (under everything else): it rises in the east, on the right of the screen (the compass dial's east), at the real sunrise, climbs as high as the day's noon elevation allows (higher in summer than in winter) and sets in the west, on the left, at the real sunset, drawn only in daylight, its glare fading out before the window's sides. The sun is a blown-out white ball with no edge, its brightness falling smoothly from a white core through pale yellow-white to a pink-magenta glare that melts into the beams, with a modest dark-orange ember in the glare and a soft ember bloom round it; at 43 °C it is larger and whiter, at 30 °C smaller and dimmer with a warmer cream centre. The glare and the ember bloom breathe, slowly and unevenly like a furnace (three slow swells of about 7, 11 and 17 seconds at once, none faster than 0.15 Hz, stopping with the weather), the hotter the stronger: at 30 °C a barely visible breath of a couple of percent, at 43 °C the glare's radius swells and shrinks by about a fifth and its brightness by about a third, and the glare itself is a quarter wider at 43 °C than at 30 °C beyond the sun's growth with the heat; the white ball keeps its size and shape, and the beams never pulse, they only sweep. The UV beams are twelve thick violet shafts round the sun, about 30 degrees apart, blunt-ended and fading smoothly to nothing, reaching at most a third of the way down the window (partly over the top row of tiles), each as wide as its length asks, so they read as distinct shafts, brightening where they overlap near the sun; the ones toward the real sun's direction are the longest and brightest, and they rise from UV index 6 (showing from about 6.2) to full at 11. Ice follows below 0 °C. "stop" at the bottom left stops and clears them for that page view only, and ends any effect a temporary test link turned on;
nothing is saved, so a reload brings them back. `MCP_DASHBOARD_ANIMATIONS` sets how much moves, for everyone: `on` (the default)
animates everything; `low` keeps the gauges sweeping and the tiles rearranging animated but draws no weather (no "stop" or test links)
and zooms at once; `off` draws no weather and animates nothing (the gauges jump to each new reading, zooming and rearranging happen at
once). A browser asking for reduced motion gets `off`'s.

On a phone or tablet the humidity's beads run downhill as it tilts: the page reads the motion sensor (gravity, turned for the screen's
rotation, so the true downhill whichever way it is held), smoothed; they run off whichever edge is lowest, and the steeper the
tilt the faster (sin of the angle off level: none under 5 degrees, rising smoothly to full speed at 40, which holds through upright, then
easing off again from 140 degrees). A bead also sticks until the glass is steep enough for its size, as a real drop does (surface
tension pins it by its width, gravity pulls it by its volume): the radius from which a bead runs is 1.4 px / sqrt(sin of the angle off
level), so 1.4 on an upright device (and with no sensor), 2.0 at 30 degrees and 3.4 at 10; smaller beads stay put (and keep merging
and growing until they give way), and one nearing its size leans toward downhill (a pixel, a little longer) before it lets go. A
running bead the glass is levelled under brakes to a stop in about 0.3 s. Lying flat, face up or face down (under 5 degrees), the water pools instead: nothing runs, no trails
are laid, and beads that touch (or sit a few px apart: a slight pull draws them together) keep merging into ever larger blobs, with no
size limit. Android's Chrome and Firefox give the sensor freely and it starts by itself; an iPhone's or
iPad's Safari asks first, so a small "tilt" link appears at the bottom left (a tap asks; gone once answered). Desktop browsers have no
sensor, so nothing changes there, and with `low` or `off` (or reduced motion) the sensor is never read. The browser needs the page
served over HTTPS for the sensor (behind a TLS proxy, not plain `http://` on the LAN, except on localhost).

A strong wind pushes the beads sideways as they run: from 15 km/h (the effective speed, a gust counting a third of what it is over the
speed) they lean downwind, by `0.6 * smoothstep((speed - 15) / 30)` of the pull sideways (0.3 at 30 km/h), up to about 31 degrees off
straight down from 45 km/h; their speed, the trails, merging and running off the downwind edge are as ever, and a flat (pooled) device
stays pooled. The test link's wind blows at 11 km/h, under the onset: to see the push, open `?fx=humidity2,wind-west&windspeed=40`
(`windspeed`, km/h, sets the effective wind speed for the page while a test link is on; `wind-east` pushes the beads leftward).

It is read-only (the weather and air readings, nothing else), so it is open to anyone on your network: no token, and any name or address
in the URL (`MCP_ALLOWED_HOSTS` is only for `/mcp`, which keeps its token). ecobot-mcp has to listen beyond the machine itself:
Docker publishes port 8765 already; under systemd set `MCP_BIND=0.0.0.0:8765`. It is plain HTTP: keep it on your own network. To
turn it off, set `MCP_DASHBOARD=off`. To keep your home's own readings off it (privacy), set `MCP_DASHBOARD_INDOOR=off`: the indoor
tiles (Indoor temperature, Indoor humidity) are then neither drawn nor sent, and nothing indoor leaves ecobot-mcp through the dashboard.
The MCP tools, the Status picture (`ecobot://status`) and the alerts still have them.

To put it on a site of its own behind a reverse proxy, map the site's root to `/dashboard/` (it answers there too), e.g. with nginx
`location / { proxy_pass http://deb1:8765/dashboard/; }`. The live updates need nothing more: they tell nginx not to buffer them.

A link to the page shared in a chat app previews with its sun icon, "Weather Dashboard" and a line about it (the page's Open Graph
tags), behind a proxy too, with nothing to set: the preview's picture is the icon as this repository publishes it on GitHub
(`raw.githubusercontent.com`), since WhatsApp and Messenger want a full https address and ecobot-mcp can't know its own. Only the
chat app fetches it; your browser loads the page's icon from ecobot-mcp (`/dashboard/icon.png`). Serve the site over https
(Signal shows no preview for plain http). A chat app keeps a preview it has made: to see a new one, share the link with `?v=2` on the end.

## Upgrading

Your keys in `/etc/ecobot.env` and the data folder (`/var/lib/private/ecobot`) are kept; settings with an old name still work
([Renamed settings](#renamed-settings)). The log's first line, `Starting ecobot <commit>`, says which version is running.

### From the Telegram bot

The Telegram bot is gone: ecobot-mcp and irrigation-mcp remain, and [signalbot](#signalbot-the-alert-feeds) and other MCP clients
take its place. Stop and remove the bot, then update the rest as below:

- **Docker:** `cd /opt/ecobot && sudo git pull && sudo docker compose up -d --build --remove-orphans && sudo docker image prune -f`.
  `--remove-orphans` removes the old `ecobot` container (the `bot` profile's); `COMPOSE_PROFILES=bot` in `/opt/ecobot/.env` can lose
  `bot` (keep `irrigation` if you run it).
- **systemd:** `sudo systemctl disable --now ecobot`, then `sudo rm /etc/systemd/system/ecobot.service && sudo systemctl daemon-reload`.
  ecobot-mcp's and irrigation-mcp's units no longer start before it.
- The `BOT_*`, `TELEGRAM_*` and `OPENROUTER_*` lines in `/etc/ecobot.env` (and `ADMIN_ONLY`, `ADMIN_CHAT_IDS`, `LOG_WIDTH`,
  `WEATHER_MCP_URL`) are ignored; the units unset the secrets among them. The [settings script](#renamed-settings) lists them as
  removed, with `--write` it deletes them (keeping a backup).
- `bot_state.json` (the chats' settings) stays in the data folder, unused. Alert subscriptions are signalbot's now (`/alerts`).
- The bot's alert state and irrigation state were copied into ecobot-mcp's and irrigation-mcp's own files at their first start (a few
  weeks ago). An install that never started either since then loses that state: the alert rules start afresh and irrigation-mcp
  forgets a delay it was holding.
- `ecobot://alerts` and `irrigation://alerts` (with their `since` and `expired` queries) are gone: read `ecobot://feed` and
  `irrigation://feed`.
- The repository secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` of the GitHub workflows
  are unused now (CI failure notices no longer go to Telegram): delete them in the repository's settings.

### Renamed settings

In November 2026 every setting got the name of the program that reads it: `MCP_` for ecobot-mcp's, `IRRIGATION_` for irrigation-mcp's,
and none for the few both read. The old names still work: when a new name is unset and its old one is set, the old one is used, and the log says
once at start which to rename (never their values). Rename them in `/etc/ecobot.env` when convenient, by hand from the table, or with the script, which shows what it
would change (secrets masked) and changes the file only with `--write`, keeping a backup beside it:

```
sudo python3 /opt/ecobot/scripts/migrate_env.py /etc/ecobot.env
```

```
sudo python3 /opt/ecobot/scripts/migrate_env.py /etc/ecobot.env --write
```

Then restart ecobot-mcp (and irrigation-mcp). Beyond the renames it moves `WEATHER_MCP_TOKEN` into `AUTH_TOKENS`, changes the old
container name `ecobot-service` in a URL or allowed host to `ecobot-mcp`, and removes the Telegram bot's settings, with a note each; it
stops, changing nothing, where an old and a new name are both set to different values.

| Old name | New name |
|---|---|
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY`, `ECOWITT_SOURCE` | `MCP_ECOWITT_API_KEY`, `MCP_ECOWITT_APP_KEY`, `MCP_ECOWITT_SOURCE` |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID`, `AIRGRADIENT_DASHBOARD_URL`, `AIRGRADIENT_LOCAL_URL`, `AIRGRADIENT_SOURCE` | the same with `MCP_` in front |
| `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` | `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` |
| `PLACE`, `POLLEN`, `POLLEN_DISTRICT`, `FORECAST`, `FORECAST_LAT`, `FORECAST_LON` | the same with `MCP_` in front |
| `RAIN_QUIET_HOURS`, `ALERT_COOLDOWN_MINUTES`, `ANTISPIKE_REQUIRE_CONSECUTIVE` | the same with `MCP_` in front |
| `CHART_ALL_FEELS_LIKE`, `CHART_ALL_VPD` | `MCP_CHART_ALL_FEELS_LIKE`, `MCP_CHART_ALL_VPD` |
| `MCP_SERVICE_BIND`, `MCP_SERVICE_ALLOWED_HOSTS` | `MCP_BIND`, `MCP_ALLOWED_HOSTS` |
| `MCP_SERVICE_TOKENS` | `AUTH_TOKENS` |
| `TUYA_DEVICE_ID`, `TUYA_CLIENT_ID`, `TUYA_CLIENT_SECRET`, `TUYA_BASE_URL`, and their names with `BOT_` in front | the same with `IRRIGATION_` in front (irrigation-mcp's) |
| `BOT_IRRIGATION_CHECK_HOUR` | `IRRIGATION_CHECK_HOUR` |
| `WEATHER_MCP_TOKEN` | retired: put the token in `AUTH_TOKENS`, or delete it to use the generated one |

`TZ`, `DATA_DIR`, `LOG_LEVEL` and `DARKMODE` keep their names.

### Your data

The weather station's and the air sensor's history lives in one file, `ecobot.sqlite`. A version from before 29 October 2026 kept it in `ecowitt_cache.sqlite`, the two files beside it, `airgradient_cache.sqlite` and `conditions_cache.sqlite`; the releases of that week moved them into the one file at start, so upgrade through one of those (commit 56a998c or earlier that week) if you still have the old files, then delete them.

### systemd

```
cd /opt/ecobot && sudo git pull
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt -r /opt/ecobot/requirements-charts.txt
```

Copy the unit in again (it changes now and then), then restart ecobot-mcp:

```
sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart ecobot-mcp
```

(Before November 2026 the unit ran `python -m ecobot_service`, its old name: an old unit not yet copied in still starts, with a warning
in its log saying to copy the new one.)

With [irrigation-mcp](#irrigation) installed, copy its unit in again too and restart it:
`sudo cp /opt/ecobot/irrigation-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart irrigation-mcp`.

### Docker

```
cd /opt/ecobot && sudo git pull
```

```
sudo docker rm -f ecobot-service 2>/dev/null; sudo docker compose up -d --build --remove-orphans
```

(The first part removes the container ecobot-mcp had before November 2026, `ecobot-service`, which would keep port 8765; once it's gone
it does nothing. `--remove-orphans` removes the Telegram bot's container.)

Compose replaces a container only if its image or settings changed. To remove the old images and build cache that upgrades leave behind:

```
sudo docker image prune -f && sudo docker builder prune -f
```

Your token lines still work (`MCP_SERVICE_TOKENS` is read as `AUTH_TOKENS`, and `WEATHER_MCP_TOKEN` is ignored once that is set, with
a warning): rename `MCP_SERVICE_TOKENS` to `AUTH_TOKENS` and delete `WEATHER_MCP_TOKEN`, or delete both to use the token ecobot-mcp
generates. See [Renamed settings](#renamed-settings).

## Configuration

Settings are environment variables in `/etc/ecobot.env`. The ones you're most likely to change:

| Variable | What it does |
|---|---|
| `MCP_ECOWITT_API_KEY`, `MCP_ECOWITT_APP_KEY` | Weather station through Ecowitt's cloud. |
| `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` | Weather station through Home Assistant (instead of the cloud, or with it as a fallback; `MCP_ECOWITT_SOURCE`). |
| `MCP_AIRGRADIENT_API_TOKEN`, `MCP_AIRGRADIENT_LOCATION_ID` | Air sensor, via AirGradient's cloud. |
| `MCP_AIRGRADIENT_LOCAL_URL` | Read the AirGradient sensor directly on your network e.g. `http://airgradient_<serial>.local` (use a fixed IP address for the URL in Docker, or see [Docker networking](#docker-networking)) |
| `MCP_AIRGRADIENT_SOURCE` | Where the air sensor's readings come from: `auto` (the default) is each that is set, the sensor itself first, then Home Assistant (if it has the sensor), then the cloud, each filling only what the ones before it lack; `local`, `homeassistant` or `cloud` is that one alone. |
| `MCP_POLLEN`, `MCP_FORECAST` | `on` to add pollen and thunderstorm asthma, or the daily forecast. |
| `AUTH_TOKENS` | Optional. The tokens ecobot-mcp and irrigation-mcp accept (`token,token,...`, one per client). Unset, ecobot-mcp generates one at its first start and keeps it in the data folder (`auth_token`). |
| `IRRIGATION_TUYA_DEVICE_ID`, `IRRIGATION_TUYA_CLIENT_ID`, `IRRIGATION_TUYA_CLIENT_SECRET` | The irrigation controller (Tuya cloud), for irrigation-mcp. See [Irrigation](#irrigation). |

After editing the file, restart: `sudo systemctl restart ecobot-mcp`, or for Docker `sudo docker compose up -d --force-recreate` (`docker restart` keeps the old settings).

Every setting is in [docs/configuration.md](docs/configuration.md).

More: [what the servers provide](docs/features.md), [how it works](docs/how-it-works.md), [the MCP design](docs/ecobot-mcp.md), [developing](DEVELOPMENT.md).
