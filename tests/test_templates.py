from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent, templates
from lib.bot import Bot
from tests.fakes import TZ

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=TZ)


def test_a_button_label_is_its_sentence_without_the_emoji_and_typed_text_is_left_alone():
    assert templates.sentence("\U0001f4cb Report") == "Report"
    assert templates.sentence("🌧️ Rain chart 7d") == "Rain chart 7d"
    assert templates.sentence("report") is None and templates.sentence("Report") is None
    assert all(templates.sentence(label) for label in templates.LABELS - {templates.ALERTS})


def test_every_button_asks_something_the_bot_understands():
    def read(label):
        return intent.read(templates.sentence(label), NOW, True, True)
    assert read("\U0001f4cb Report").report
    assert read("\U0001f32c️ Air quality now").fast[0] == "air_quality"
    assert read("\U0001f3ed AQ all week").fast[1]["chart"] is True
    assert read("\U0001f4c8 Temperature chart 7d").fast[1]["chart"] is True
    assert read("\U0001f327️ Rain chart 7d").chart_asked and read("\U0001f327️ Rain chart 7d").chart_field == "daily"
    assert read("\U0001f4a7 Humidity chart 7d").chart_field == "humidity"
    assert len(read("\U0001f324️ Weather all week").chart_fields) > 5
    assert read("\U0001f321️ Weather now").needs_data


def test_the_keyboard_is_persistent_and_has_every_button():
    kb = templates.keyboard()
    assert kb.is_persistent and kb.resize_keyboard
    assert {b.text for row in kb.keyboard for b in row} == templates.LABELS


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


async def test_tapping_a_button_asks_its_sentence_and_the_alerts_button_runs_the_alerts_command():
    bot = Recorder()
    for text in ("\U0001f4cb Report", "weather now", "\U0001f514 Alerts"):
        await bot.on_message(message(text)[0], NS(bot=NS(username="b", id=99)))
    assert bot.asked == ["Report", "weather now"] and bot.alerts == 1


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
