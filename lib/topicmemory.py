"""Which guidance topics a chat is still on. A follow-up ("and for temperature?") often says nothing that names its topic, so a topic
stays on for a chat while either side keeps naming it or the model keeps using its tools, until RUNS runs in a row go by with none of
that. (The last messages are looked at as well: see Bot._respond.)"""

RUNS = 2


class TopicMemory:
    def __init__(self):
        self.idle: dict[str, int] = {}   # topic -> runs since it was last named or used; a topic that has gone quiet is dropped

    def active(self) -> set[str]:
        return set(self.idle)

    def update(self, offered: set[str], mentioned: set[str]) -> None:
        """After a run. `offered`: the topics it was given guidance for; `mentioned`: those named by the person, named in the reply or
        used by a tool."""
        for topic in mentioned:
            self.idle[topic] = 0
        for topic in offered - mentioned:
            if topic in self.idle:
                self.idle[topic] += 1
        self.idle = {t: n for t, n in self.idle.items() if n < RUNS}
