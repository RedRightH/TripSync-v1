import sqlite3
from datetime import timedelta

import pytest

from tripsync.clock import FakeClock
from tripsync.core.events import EventLog


def make():
    c = FakeClock()
    return c, EventLog(c)


def test_append_assigns_increasing_seq_and_timestamps():
    c, log = make()
    a = log.append("t1", "trip_created", {"x": 1})
    c.advance(timedelta(minutes=1))
    b = log.append("t1", "trigger_accepted")
    assert b.seq > a.seq
    assert b.ts - a.ts == timedelta(minutes=1)
    assert a.payload == {"x": 1} and b.payload == {}


def test_events_are_scoped_per_trip_and_ordered():
    _, log = make()
    log.append("t1", "a")
    log.append("t2", "b")
    log.append("t1", "c")
    assert [e.kind for e in log.for_trip("t1")] == ["a", "c"]
    assert [e.kind for e in log.for_trip("t2")] == ["b"]
    assert [e.kind for e in log.all()] == ["a", "b", "c"]


def test_log_cannot_be_updated_or_deleted_even_with_raw_sql():
    _, log = make()
    log.append("t1", "a")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log.db.execute("UPDATE events SET kind='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log.db.execute("DELETE FROM events")
    assert [e.kind for e in log.for_trip("t1")] == ["a"]


def test_idempotency_key_returns_original_event_without_duplicating():
    _, log = make()
    e1 = log.append("t1", "refunded", {"m": "m1"}, idem_key="refund:t1:m1")
    e2 = log.append("t1", "refunded", {"m": "m1"}, idem_key="refund:t1:m1")
    assert e1 == e2
    assert len(log.for_trip("t1")) == 1


def test_same_idempotency_key_on_a_different_trip_is_a_different_event():
    _, log = make()
    log.append("t1", "x", idem_key="k")
    log.append("t2", "x", idem_key="k")
    assert len(log.all()) == 2


def test_rule_is_recorded_for_the_audit_trail():
    _, log = make()
    e = log.append("t1", "release_denied", {"failed": ["quorum"]}, rule="R1")
    assert log.for_trip("t1")[0].rule == "R1" and e.rule == "R1"


def test_persists_across_connections(tmp_path):
    c = FakeClock()
    path = str(tmp_path / "ev.db")
    EventLog(c, path).append("t1", "a", {"k": "v"})
    assert EventLog(c, path).for_trip("t1")[0].payload == {"k": "v"}
