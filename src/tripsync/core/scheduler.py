"""Scheduler: durable timers fired from a real clock (FakeClock in tests).

Timers live in SQLite so a restart does not lose a deadline. A timer fires exactly once.
The scheduler never decides anything: it hands due timers to a handler, nothing more.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from tripsync.clock import Clock


@dataclass(frozen=True)
class Timer:
    id: int
    trip_id: str
    kind: str
    due_at: datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  due_at TEXT NOT NULL,
  fired INTEGER NOT NULL DEFAULT 0,
  cancelled INTEGER NOT NULL DEFAULT 0,
  UNIQUE(trip_id, kind)
);
"""


class Scheduler:
    def __init__(self, clock: Clock, path: str = ":memory:") -> None:
        self._clock = clock
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    def schedule(self, trip_id: str, kind: str, due_at: datetime) -> Timer:
        """Schedule a timer. One timer per (trip, kind); re-scheduling an unfired one moves it."""
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        due_at = due_at.astimezone(timezone.utc)  # stored in UTC so string comparison is safe
        row = self._db.execute(
            "SELECT id, fired FROM timers WHERE trip_id=? AND kind=?", (trip_id, kind)
        ).fetchone()
        if row and row[1]:
            raise ValueError(f"timer {kind} for {trip_id} already fired")
        if row:
            self._db.execute(
                "UPDATE timers SET due_at=?, cancelled=0 WHERE id=?", (due_at.isoformat(), row[0])
            )
            tid = row[0]
        else:
            tid = self._db.execute(
                "INSERT INTO timers(trip_id, kind, due_at) VALUES (?,?,?)", (trip_id, kind, due_at.isoformat())
            ).lastrowid
        self._db.commit()
        return Timer(tid, trip_id, kind, due_at)

    def cancel(self, trip_id: str, kind: str) -> bool:
        cur = self._db.execute(
            "UPDATE timers SET cancelled=1 WHERE trip_id=? AND kind=? AND fired=0 AND cancelled=0", (trip_id, kind)
        )
        self._db.commit()
        return cur.rowcount > 0

    def pending(self, trip_id: str | None = None) -> list[Timer]:
        q = "SELECT id, trip_id, kind, due_at FROM timers WHERE fired=0 AND cancelled=0"
        args: tuple = ()
        if trip_id:
            q += " AND trip_id=?"
            args = (trip_id,)
        return [self._timer(r) for r in self._db.execute(q + " ORDER BY due_at, id", args).fetchall()]

    def tick(self, handler: Callable[[Timer], None]) -> list[Timer]:
        """Fire every timer that is due, oldest first. Each fires once, even if the handler raises."""
        now = self._clock.now().astimezone(timezone.utc).isoformat()
        due = self._db.execute(
            "SELECT id, trip_id, kind, due_at FROM timers WHERE fired=0 AND cancelled=0 AND due_at<=? ORDER BY due_at, id",
            (now,),
        ).fetchall()
        fired: list[Timer] = []
        for r in due:
            t = self._timer(r)
            self._db.execute("UPDATE timers SET fired=1 WHERE id=?", (t.id,))
            self._db.commit()
            fired.append(t)
            handler(t)
        return fired

    @staticmethod
    def _timer(r: tuple) -> Timer:
        return Timer(r[0], r[1], r[2], datetime.fromisoformat(r[3]))
