# What the bot does

## Ask it anything about your weather or air

Type a question in any chat the bot is in ("how hot is it?", "will it rain?", "rain by month for 2 years", "air quality 30d"). Simple lookups, reports and charts are worked out by the bot itself, instantly; only questions that need reasoning go to the AI model.

- **Charts:** any reading or several on one time axis, rain behind the lines, one colour per reading. Under a single chart are period buttons (1D, 7D, 1M, 3M, 1Y); pressing one redraws it in place. Ask for "plot uv and solar on two axes" (or "dual axis", "overlay") to put two readings on one chart, each with its own scale: the left axis for the first, the right for the second. The AirGradient sensor's own temperature and humidity can be charted too, against the weather station's: "plot airgradient humidity over ecowitt humidity" draws both on one axis. They're offered to the AI model only when you name the AirGradient with temperature or humidity. "weather all week" draws every reading, a panel each.
- **Reports:** `/report` (or the Status button, or typing "report", "status" or "weather now") lays out the weather station, the air quality and, if switched on, pollen and the forecast, with a 🔄 Refresh button. With the weather station it comes as the caption of a picture of three gauges (temperature, humidity and rain, each with its highs and lows today, this month and this year, as on `/dashboard`); Refresh redraws the picture too. A report too long for a caption goes as its own message after the picture, and its Refresh redraws only the text.
- **Dashboard:** `/dashboard` (or typing "dashboard" or "gauges") draws the weather station as one picture of gauges: temperature, humidity, pressure, dew point, feels like, wind, rain, rain rate, solar radiation and UV index (and the indoor sensor, if there is one). Each shows the reading now and its highest and lowest today, this month and this year (rain: the wettest day and the total), worked out from the history the bot has stored. No AI model is needed.
- **Days and totals:** count or rank days by any reading ("how many days was UV 9 or more", "hottest day each year"), or total rain per month or year as bars.
- **Analysis:** whether rain follows a reading, or how air quality relates to everything else, over the stored history.
- **Commands:** each chart or report button also works as a command, in private chats and groups: `/report` (or `/status`), `/dashboard`, `/rain`, `/weather`, `/temperature`, `/humidity`, `/air_quality`, `/particulates`, `/wind`.
- The commands also appear in Telegram's `/` menu beside the message box.
- **Private chats** also get a button keyboard (Help & Alerts, Status, Rain, Weather, Temperature, Humidity, Air Quality, Particulates, Wind) and show Telegram's "Thinking…" while the answer is written.

## Alerts

Alerts go to every chat the bot is in, silently unless you turn sound on:

- rain starting and stopping (with quiet hours, default midnight to 6 am, summed up when they end)
- gusts over 40 km/h, with the last day's wind chart
- UV index of 10 or more in two readings in a row, with the UV chart
- indoor and outdoor temperatures crossing by more than 1 °C, with the temperature chart
- unhealthy air (two readings in a row), and when it is safe again, with the particulates chart
- grass pollen or thunderstorm asthma risk reaching High (optional)
- a forecast day you were told about changing substantially (optional)
- irrigation battery low, and the bot pausing watering for rain (optional)

Every alert carries a 🔔 button that opens its toggles: **Alert** (on or off in this chat) and, in a private chat while that is on, **Sound**. An alert with a chart also has **🔄 Refresh**, which puts the chart with the latest readings in the same message, the alert's text as it was (anyone in the chat can press it): for six hours after an alert its chart is kept drawn ahead with the keyboard's, so the press is answered at once. Every toggle in the bot is a blue button while on and a plain one with its name struck through while off.

`/alerts` opens a menu with 🔔 Alerts and (in a private chat) 🔊 Sound. Each lists the alert types as toggles; while one is open only its own button shows (press it to close the list). `/alerts on` or `/alerts off` switches every type. A chat that blocks or removes the bot keeps its settings, which apply again when it comes back. In a group anyone can open `/alerts`, but only admins can change anything.

## Who can use it

By default the bot answers everyone who messages it. `ADMIN_ONLY=on` answers only the admins of the groups it is in, plus any ids in `ADMIN_CHAT_IDS`; everyone else is told it is for group admins. A refused sender is logged once, with their id, so the owner can add them to `ADMIN_CHAT_IDS`. See [configuration.md](configuration.md).

## Optional extras

- **Pollen and thunderstorm asthma** for Melbourne (October to December), from melbournepollen.com.au: `POLLEN=on`.
- **Daily forecast** from Open-Meteo, with the chance of at least 1 mm of rain: `FORECAST=on`.
- **Irrigation controller** (a Tuya smart timer): an Irrigation button with its status and buttons to water for 5 to 30 minutes, switch off, pause for 24 to 72 hours, and its own alert toggles. A daily check at 18:00 delays watering 24 hours when rain fell or is likely. Set the `TUYA_*` variables.
