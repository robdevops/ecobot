# Telegram: using the bot by tapping (design)

Not built yet. This is the plan for making commands and common questions tappable, in three phases. Each phase stands on its own.

## What exists today
`lib/bot.py` registers `/start` and `/help`, `/reset` and `/alerts`, and sends every other text message to `Bot.respond`, which reads the sentence (`intent.read`), takes the fast path when the bot can fetch without the model, runs the model with the tools, and sends the answer with any charts. Replies are silent. A message in a group is answered only when the bot is mentioned or replied to. Questions in one chat are queued in order and given up on after 90 seconds.

## Built since: the thinking draft
In private chats `Bot.respond` shows a `sendMessageDraft` ("Thinking...", then the answer streaming in) instead of "typing...", then sends the real reply as before (`lib/bot.py` `Draft`, `lib/llm.py` streaming).

## Built since: the button keyboard
Private chats get a persistent reply keyboard of template questions (`lib/templates.py`); a tap arrives as ordinary text, so the answer takes the usual path. `/keyboard` and `/keyboard off` show and hide it.

## Built since: alert settings as buttons
Every alert carries Subscribe | Unsubscribe buttons that expand in place into the alert types, under headings (a button row that does nothing, as Telegram keyboards have no headings) saying whether they are on; `/alerts` shows the same menu. Callback data is `al:<action>:<kind>:<section>` (`lib/alerts/menu.py`, handled by `Bot.on_alert_button`); in groups only admins may change them.

## Principle
A command or a button never does work of its own. It is turned into the plain sentence a person would have typed and handed to `Bot.respond`. The fast path, the period hints, the model, the queue, the timeout, the charts and the alerts all behave exactly as they do for typed text, and there is one place to fix them.

## Phase 1: the command menu and tappable commands
**Menu.** At startup (`post_init`) call `bot.set_my_commands([...])` so typing `/` in any chat lists the commands with a description. It is idempotent, so it runs on every start.

| Command | Does | Sentence sent to `respond` |
|---|---|---|
| `/weather [period]` | the weather now, or a summary with a chart for a period | `weather now`, `weather 7d` |
| `/air [period]` | air quality now, or a chart for a period | `air quality now`, `ag 7d` |
| `/chart <reading> [period]` | one reading as a chart; `<reading>` is any weather or air name | `chart dew point 7d` |
| `/status`, `/report`, `/sitrep` | the full current report (synonyms, one handler registered for all three names) | `report` |
| `/alerts [on\|off]` | as today | (unchanged) |
| `/reset` | as today | (unchanged) |
| `/help` | the command list | (unchanged) |

Defaults: `/weather` and `/air` with no period are "now"; `/chart` with no reading is temperature; a chart with no period is 7 days. `/status` and `/report` use the sentence the bot already recognises (`intent.wants_report`), which fetches the weather and the air quality in one go and lists everything.

**Implementation.** A pure function `command_to_text(name, args) -> str` holds the table above. One generic `CommandHandler` for the new names calls it and then `Bot.respond(update, context, text)`. In groups, Telegram sends `/weather@ecobot`; `python-telegram-bot` strips the suffix, and a command reaches a bot in a group even with privacy mode on, so no mention is needed. Replies stay silent.

**Tappable commands in replies (free).** Telegram turns `/command` text in a message into a link. `HELP`, the `/alerts` status text and the error replies print the next useful command as text: "/alerts off", "/chart 30d", "/report". No new code beyond wording.

## Phase 2: buttons (optional)
Inline keyboards under the replies to `/start`, `/weather` and `/chart`:
- a row of periods: `24h`, `7d`, `30d`, `90d`;
- a row of readings: temperature, humidity, pressure, wind, rain, air quality.

A `CallbackQueryHandler` parses `callback_data` (kept under 64 bytes, for example `chart:dew_point:7d`), answers the query so the button stops spinning, builds the same sentence as the command would and calls `Bot.respond`. `/chart` with no arguments sends a two-step picker: reading first, then period. The callback carries everything it needs, so an old button still works. In a group, anyone may press a button, and the press counts as addressed to the bot.

## Phase 3: inline mode (only if wanted)
Typing `@ecobot` in any chat and picking a result (the current reading, a chart). It needs inline mode switched on in BotFather, a result cache and answers produced without the per-chat queue, so it is a separate piece of work.

## Testing
- `command_to_text` is a pure function: a table-driven test, including `/status` == `/report` == `/sitrep` == the report sentence and the defaults.
- Handler tests use the fake update objects already in `tests/test_bot.py`: a command produces one reply through the same path as typed text.
- The eval cases already cover the sentences the commands produce.

## Open points
- The command list and wording are a first proposal.
- Whether buttons (phase 2) are worth their keyboard clutter depends on how the menu feels in use, so build phase 1 first.
