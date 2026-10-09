"""Who may talk to the bot when ADMIN_ONLY is on: the admins of the groups it is in, and the ids listed in ADMIN_CHAT_IDS.

A "known admin" is anyone Telegram lists as an administrator or creator of a group the bot knows (a group becomes known when the bot is
added to it or someone speaks in it) or of a group named by a negative id in ADMIN_CHAT_IDS. The lists are asked for once and kept for
ten minutes. ADMIN_CHAT_IDS is added to that, never instead of it: a positive id is a user (it bootstraps a bot that is in no group yet),
a negative id is a group, whose own admins are then looked up and whose anonymous admins are let in: a message an admin sends "as the
group" carries the group as its `sender_chat` and no real user, so it is allowed only when that group's id is listed.
"""

import logging
import time

log = logging.getLogger(__name__)

TTL_SECONDS = 600
RETRY_SECONDS = 60          # after a failed lookup, ask again after this long (not on every message)


class Admins:
    def __init__(self, state, extra_ids=(), ttl: float = TTL_SECONDS, clock=time.monotonic):
        self.state, self.extra, self.ttl, self.clock = state, frozenset(extra_ids), ttl, clock
        self._cache: dict[int, tuple[float, frozenset[int]]] = {}   # group -> (when fetched, its admins)
        self._failing: set[int] = set()

    def groups(self) -> list[int]:
        """The groups whose admins count: the ones the bot knows, and the ones ADMIN_CHAT_IDS names."""
        known = {c for c in (self.state.chats if self.state else {}) if c < 0}
        return sorted(known | {i for i in self.extra if i < 0})

    async def _admins_of(self, bot, chat_id: int) -> frozenset[int]:
        cached = self._cache.get(chat_id)
        if cached and self.clock() - cached[0] < self.ttl:
            return cached[1]
        try:
            members = await bot.get_chat_administrators(chat_id)
            ids = frozenset(m.user.id for m in members if not getattr(m.user, "is_bot", False))
        except Exception as e:   # the bot was removed, Telegram is down: keep what was known, try again soon, say it once
            if chat_id not in self._failing:
                self._failing.add(chat_id)
                log.warning("Couldn't list the admins of %s (%s: %s)", chat_id, type(e).__name__, e)
            ids = cached[1] if cached else frozenset()
            self._cache[chat_id] = (self.clock() - self.ttl + RETRY_SECONDS, ids)
            return ids
        self._failing.discard(chat_id)
        self._cache[chat_id] = (self.clock(), ids)
        return ids

    async def allowed(self, bot, sender_ids) -> bool:
        """Is one of these ids (the sender's user id and, for a message sent as a chat, that chat's id) allowed?"""
        ids = {i for i in sender_ids if isinstance(i, int)}
        if ids & self.extra:
            return True
        for chat_id in self.groups():
            if ids & await self._admins_of(bot, chat_id):
                return True
        return False
