from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent, templates
from lib.bot import Bot
from tests.fakes import TZ

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=TZ)


def test_a_button_label_is_its_sentence_without_the_emoji_and_typed_text_is_left_alone():
    assert templates.sentence("\U0001f4cb Report") == "Report"
    assert templates.sentence("\U0001f4c8 Temperature chart 90d") == "Temperature chart 90d"
    assert templates.sentence(templates.CAPABILITIES) == "What can you do?"
    assert templates.sentence("report") is None and templates.sentence("Report") is None
    assert len(templates.LABELS) == 9


def test_every_button_asks_something_the_bot_understands():
    def read(label):
        return intent.read(templates.sentence(label), NOW, True, True)
    assert read("\U0001f4cb Report").report
    for days in (7, 30):
        name, args, _ = read(f"\U0001f3ed Air quality {days}d").fast
        assert name == "air_quality" and args["chart"] is True and set(args["metrics"]) == {"pm1", "pm2_5", "pm10"}
    for days in (7, 30, 90):
        name, args, _ = read(f"\U0001f4c8 Temperature chart {days}d").fast
        assert name == "weather_history" and args["chart"] is True
    assert read("\U0001f327️ Rain chart 7d").rain_caption and read("\U0001f327️ Rain chart 7d").chart_field == "daily"
    assert read("\U0001f4a7 Humidity chart 7d").chart_asked and read("\U0001f4a7 Humidity chart 7d").chart_field == "humidity"
    assert not read("\U0001f4a7 Humidity chart 7d").rain_caption
    assert read(templates.CAPABILITIES).about_the_bot


def test_the_keyboard_is_persistent_and_has_every_button():
    kb = templates.keyboard()
    assert kb.is_persistent and kb.resize_keyboard
    assert {b.text for row in kb.keyboard for b in row} == templates.LABELS
    assert [len(row) for row in kb.keyboard] == [3, 3, 3]


class Recorder(Bot):
    def __init__(self):
        super().__init__(NS(tz=TZ), None, [], None)
        self.asked, self.alerts = [], 0

    async def respond(self, update, context, text):
        self.asked.append(text)

    async def on_alerts(self, update, context):
        self.alerts += 1


def message(text, chat_type="private"):
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    msg = NS(text=text, chat=NS(type=chat_type, id=1, title=None), chat_id=1, reply_text=reply_text,
             reply_to_message=None, message_thread_id=None, is_topic_message=False)
    return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(id=7, full_name="Rob", username="rob")), sent


async def test_tapping_a_button_asks_its_sentence_and_capabilities_and_alerts_prints_both():
    bot = Recorder()
    for text in ("\U0001f4cb Report", "weather now", templates.CAPABILITIES):
        await bot.on_message(message(text)[0], NS(bot=NS(username="b", id=99)))
    assert bot.asked == ["Report", "weather now"] and bot.alerts == 1       # capabilities are not sent to the model


async def test_start_carries_the_keyboard_in_private_chats_only_and_keyboard_off_removes_it():
    bot = Recorder()
    update, sent = message("/start")
    await bot.on_start(update, NS(args=[]))
    assert sent[0][1]["reply_markup"].is_persistent
    update, sent = message("/start", chat_type="supergroup")
    await bot.on_start(update, NS(args=[]))
    assert sent[0][1]["reply_markup"] is None
    update, sent = message("/keyboard off")
    await bot.on_keyboard(update, NS(args=["off"]))
    assert type(sent[0][1]["reply_markup"]).__name__ == "ReplyKeyboardRemove"
    update, sent = message("/keyboard")
    await bot.on_keyboard(update, NS(args=[]))
    assert sent[0][1]["reply_markup"].is_persistent
    update, sent = message("/keyboard", chat_type="group")
    await bot.on_keyboard(update, NS(args=[]))
    assert "reply_markup" not in sent[0][1]


async def test_the_alerts_status_lists_one_bullet_per_alert():
    from lib.bot import alerts_text
    text = alerts_text(True)
    bullets = [line for line in text.splitlines() if line.startswith("\u2022 ")]
    assert len(bullets) == 6 and text.startswith("Weather alerts are on here:") and text.endswith("to turn them off.")
    assert len(alerts_text(False, pollen=True).splitlines()) == 9 and "turn them on" in alerts_text(False)
    assert len(alerts_text(True, forecast=True).splitlines()) == 9 and "A forecast I sent changes" in alerts_text(True, forecast=True)
    assert "Pollen or thunderstorm asthma risk High or Extreme" in alerts_text(True, pollen=True)



def test_report_is_on_the_right_hand_side_of_the_first_row():
    assert [label for label, _ in templates.ROWS[0]][-1].endswith("Report")


class Replies:
    """A message that records what it was answered with."""

    def __init__(self, chat_type="private"):
        self.sent, self.chat_id = [], 1
        self.chat = NS(type=chat_type)

    async def reply_text(self, body, **kw):
        self.sent.append(("text", kw))

    async def reply_photo(self, photo, **kw):
        self.sent.append(("photo", kw))

    async def reply_media_group(self, media, **kw):
        self.sent.append(("group", kw))


async def test_deliver_puts_the_keyboard_on_the_last_text_or_the_single_photo(tmp_path):
    from lib.bot import deliver
    markup = templates.keyboard()
    msg = Replies()
    assert await deliver(msg, "hello", [], markup=markup) is True and msg.sent[-1][1]["reply_markup"] is markup
    msg = Replies()
    assert await deliver(msg, "caption", [b"p"], markup=markup) is True and msg.sent == [("photo", msg.sent[0][1])]
    assert msg.sent[0][1]["reply_markup"] is markup
    msg = Replies()
    assert await deliver(msg, "caption", [b"p", b"q"], markup=markup) is False       # a group of photos can't carry it
    msg = Replies()
    assert await deliver(msg, "hello", []) is False and "reply_markup" not in msg.sent[0][1]


async def test_a_chat_with_an_old_or_no_keyboard_gets_the_current_one_with_its_next_reply_once(tmp_path):
    from lib.alerts import AlertState
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    bot = Bot(NS(tz=TZ), None, [], state)
    msg = Replies()
    assert bot._keyboard_stale(msg)                                    # never had it
    state.set_keyboard(1, "old1234")
    assert bot._keyboard_stale(msg)                                    # the buttons changed since
    state.set_keyboard(1, templates.VERSION)
    assert not bot._keyboard_stale(msg)                                # up to date: nothing is added
    state.set_keyboard(1, templates.HIDDEN)
    assert not bot._keyboard_stale(msg)                                # they hid it: leave it hidden
    assert not bot._keyboard_stale(Replies("supergroup"))
    assert not Bot(NS(tz=TZ), None, [], None)._keyboard_stale(msg)


async def test_start_and_keyboard_commands_record_which_keyboard_the_chat_has(tmp_path):
    from lib.alerts import AlertState
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    bot = Bot(NS(tz=TZ), None, [], state)
    update, _ = message("/keyboard off")
    await bot.on_keyboard(update, NS(args=["off"]))
    assert state.keyboard(1) == templates.HIDDEN
    update, _ = message("/keyboard")
    await bot.on_keyboard(update, NS(args=[]))
    assert state.keyboard(1) == templates.VERSION
    state.set_keyboard(1, "old1234")
    update, _ = message("/start")
    await bot.on_start(update, NS(args=[]))
    assert state.keyboard(1) == templates.VERSION


async def test_a_reply_in_a_private_chat_carries_the_new_keyboard_once(tmp_path):
    from lib.alerts import AlertState

    class Agent:
        async def run(self, *a, **k):
            return "It is 12 degrees."
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    state.set_keyboard(1, "old1234")
    bot = Bot(NS(tz=TZ), Agent(), [], state)

    async def ask():
        update, sent = message("how hot is it")
        update.effective_message.chat = NS(type="private", id=1, title=None)
        await bot.respond(update, NS(bot=NS(id=99)), "how hot is it")
        return sent
    first, second = await ask(), await ask()
    assert first[0][1].get("reply_markup") is not None and second[0][1].get("reply_markup") is None
    assert state.keyboard(1) == templates.VERSION


async def test_at_startup_private_chats_with_old_buttons_are_told_once_and_others_left_alone(tmp_path):
    from telegram.error import Forbidden
    from lib.alerts import AlertState
    state = AlertState(tmp_path / "s.json")
    for chat_id in (10, 11, 12, 13, -20):                     # -20 is a group
        state.add_chat(chat_id, str(chat_id))
    state.set_keyboard(10, "old1234")
    state.set_keyboard(11, templates.VERSION)
    state.set_keyboard(12, templates.HIDDEN)                  # 13 and the group have no record
    sent = []

    class TG:
        async def send_message(self, chat_id, text, **kw):
            if chat_id == 13:
                raise Forbidden("bot was blocked by the user")
            sent.append((chat_id, text, kw["reply_markup"].is_persistent))
    bot = Bot(NS(tz=TZ), None, [], state)
    assert await bot.refresh_keyboards(TG()) == 1
    assert sent == [(10, "Buttons updated.", True)]
    assert state.keyboard(10) == templates.VERSION and 13 not in state.chats and state.keyboard(-20) is None
    assert await bot.refresh_keyboards(TG()) == 0             # nothing more on the next start
    assert await Bot(NS(tz=TZ), None, [], None).refresh_keyboards(TG()) == 0


def test_the_capabilities_list_is_the_users_bullets():
    assert templates.capabilities_text() == "\n".join([
        "\u2022 Temperature, humidity (indoor, outdoor)", "\u2022 Dew point, vapour pressure deficit, pressure, wind speed",
        "\u2022 Rain, solar radiation, UV index", "\u2022 PM1, PM2.5, PM10, CO₂, VOC, NOx", "\u2022 History charts"])
    assert len(templates.capabilities_text(air=False).splitlines()) == 4 and len(templates.capabilities_text(weather=False).splitlines()) == 2
    full = templates.capabilities_text(pollen=True, forecast=True).splitlines()
    assert len(full) == 7 and full[4:] == ["\u2022 Pollen and thunderstorm asthma risk", "\u2022 Forecast", "\u2022 History charts"]





def test_the_capabilities_list_adds_pollen_and_forecast_only_when_they_are_on():
    plain = templates.capabilities_text()
    assert "Pollen" not in plain and "Forecast" not in plain
    full = templates.capabilities_text(pollen=True, forecast=True).splitlines()
    assert full[-3:] == ["• Pollen and thunderstorm asthma risk", "• Forecast", "• History charts"]
