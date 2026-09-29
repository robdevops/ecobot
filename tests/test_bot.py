"""Bot.respond: what the person is sent when things go wrong."""

from types import SimpleNamespace as NS

from lib import intent
from lib.bot import Bot
from tests.fakes import TZ


class RaisingAgent:
    async def run(self, *a, **k):
        raise RuntimeError("401 Bearer xai-SECRET-1234 quota exceeded for org acme")


class AnsweringAgent:
    async def run(self, messages, system, effort, first_call=None, require_tool=True):
        self.first_call = first_call
        return "It is 12 degrees."


async def ask(agent, text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    await Bot(NS(tz=TZ), agent, [], None).respond(update, context, text)
    return replies


async def test_error_details_stay_in_the_log_not_the_chat():
    replies = await ask(RaisingAgent())
    assert len(replies) == 1 and "went wrong" in replies[0] and "try again" in replies[0].lower()
    assert "SECRET" not in replies[0] and "Bearer" not in replies[0] and "acme" not in replies[0]
    assert "RuntimeError" in replies[0]      # just the kind of problem


async def test_a_failing_shortcut_falls_back_to_the_model(monkeypatch):
    def boom(*a, **k):
        raise OverflowError("date value out of range")
    monkeypatch.setattr(intent, "fast_call", boom)
    agent = AnsweringAgent()
    assert await ask(agent, "weather last 99999 years") == ["It is 12 degrees."]
    assert agent.first_call is None
