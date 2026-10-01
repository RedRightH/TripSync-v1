"""Append-only event log (SQLite). Every decision and state change is an event.

Append-only is enforced by the database itself (triggers reject UPDATE and DELETE), not
just by convention, so a bug elsewhere cannot rewrite history.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tripsync.clock import Clock


@dataclass(frozen=True)
class Event:
    seq: int
    trip_id: str
    ts: datetime
    kind: str
    payload: dict[str, Any]
    rule: str | None
    idem_key: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  rule TEXT,
  idem_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS events_idem ON events(trip_id, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_trip ON events(trip_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class EventLog:
    def __init__(self, clock: Clock, path: str = ":memory:") -> None:
        self._clock = clock
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    @property
    def db(self) -> sqlite3.Connection:
        return self._db

    def append(
        self,
        trip_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        rule: str | None = None,
        idem_key: str | None = None,
    ) -> Event:
        """Append an event. With an idem_key, a repeat returns the original event."""
        if idem_key is not None:
            row = self._db.execute(
                "SELECT seq FROM events WHERE trip_id=? AND idem_key=?", (trip_id, idem_key)
            ).fetchone()
            if row:
                return self._get(row[0])
        cur = self._db.execute(
            "INSERT INTO events(trip_id, ts, kind, payload, rule, idem_key) VALUES (?,?,?,?,?,?)",
            (trip_id, self._clock.now().isoformat(), kind, json.dumps(payload or {}, sort_keys=True), rule, idem_key),
        )
        self._db.commit()
        return self._get(cur.lastrowid)

    def for_trip(self, trip_id: str) -> list[Event]:
        rows = self._db.execute("SELECT * FROM events WHERE trip_id=? ORDER BY seq", (trip_id,)).fetchall()
        return [self._row(r) for r in rows]

    def all(self) -> list[Event]:
        return [self._row(r) for r in self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()]

    def _get(self, seq: int) -> Event:
        return self._row(self._db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone())

    @staticmethod
    def _row(r: tuple) -> Event:
        return Event(r[0], r[1], datetime.fromisoformat(r[2]), r[3], json.loads(r[4]), r[5], r[6])
