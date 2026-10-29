"""How many SQL statements and thread hops one request makes: what the storage redesign (docs/storage-plan.md) is measured by.

A request (a question, a chart) starts a count; every statement a store's connection runs, and every hop to a thread (asyncio.to_thread),
while it lasts is added to it, in the tasks and threads it starts too (they share its context). A background task a request starts (a
refresh, an upload ahead) detaches, so its work is not the request's. The DEBUG `Chart ms` line shows the count, and so does
scripts/bench_storage.py.
"""

import asyncio
import contextvars
import sqlite3
from dataclasses import dataclass


@dataclass
class Count:
    sql: int = 0
    hops: int = 0

    def __str__(self) -> str:
        return f"{self.sql} sql, {self.hops} hops"


CURRENT: contextvars.ContextVar[Count | None] = contextvars.ContextVar("count", default=None)


def start() -> Count:
    """Count what this task, and what it starts, does from here on."""
    count = Count()
    CURRENT.set(count)
    return count


def detach():
    """This task's work (a background one, started by a request) is not counted."""
    CURRENT.set(None)


def _statement():
    if (count := CURRENT.get()) is not None:
        count.sql += 1


class Counted(sqlite3.Connection):
    """A connection that counts its statements: each execute, and each executemany or executescript once. (SQLite's trace hook would count
    each row of an executemany, but to call it Python writes out every row's statement with its values: six times the cost of the insert.)"""

    def execute(self, sql, parameters=(), /):
        _statement()
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        _statement()
        return super().executemany(sql, parameters)

    def executescript(self, script, /):
        _statement()
        return super().executescript(script)


def connect(*args, **kwargs) -> sqlite3.Connection:
    """sqlite3.connect, the connection's statements counted."""
    return sqlite3.connect(*args, factory=Counted, **kwargs)


def uncounted(db: sqlite3.Connection, sql: str):
    """A statement that is not a query (BEGIN, COMMIT), run without counting it."""
    return sqlite3.Connection.execute(db, sql)


def install():
    """Count the hops to a thread, by wrapping asyncio.to_thread (every store's reads go through it), once."""
    if getattr(asyncio.to_thread, "counted", False):
        return
    plain = asyncio.to_thread

    async def to_thread(func, /, *args, **kwargs):
        if (count := CURRENT.get()) is not None:
            count.hops += 1
        return await plain(func, *args, **kwargs)
    to_thread.counted = True
    asyncio.to_thread = to_thread
