from datetime import datetime, timedelta, timezone

import pytest

from tripsync.clock import FakeClock
from tripsync.core.scheduler import Scheduler

IST = timezone(timedelta(hours=5, minutes=30))


def make():
    c = FakeClock()
    return c, Scheduler(c)


def test_nothing_fires_before_due_and_fires_once_after():
    c, s = make()
    s.schedule("t1", "decision_deadline", c.now() + timedelta(hours=2))
    seen = []
    assert s.tick(seen.append) == []
    c.advance(timedelta(hours=1, minutes=59))
    assert s.tick(seen.append) == []
    c.advance(timedelta(minutes=1))
    assert [t.kind for t in s.tick(seen.append)] == ["decision_deadline"]
    assert s.tick(seen.append) == []
    assert len(seen) == 1


def test_fires_in_due_order():
    c, s = make()
    s.schedule("t1", "b", c.now() + timedelta(minutes=20))
    s.schedule("t2", "a", c.now() + timedelta(minutes=10))
    c.advance(timedelta(hours=1))
    assert [t.kind for t in s.tick(lambda t: None)] == ["a", "b"]


def test_cancelled_timer_never_fires():
    c, s = make()
    s.schedule("t1", "objection_window", c.now() + timedelta(minutes=10))
    assert s.cancel("t1", "objection_window") is True
    c.advance(timedelta(hours=1))
    assert s.tick(lambda t: None) == []
    assert s.cancel("t1", "objection_window") is False


def test_reschedule_moves_an_unfired_timer_but_not_a_fired_one():
    c, s = make()
    s.schedule("t1", "k", c.now() + timedelta(minutes=10))
    s.schedule("t1", "k", c.now() + timedelta(minutes=30))
    c.advance(timedelta(minutes=15))
    assert s.tick(lambda t: None) == []
    c.advance(timedelta(minutes=20))
    assert len(s.tick(lambda t: None)) == 1
    with pytest.raises(ValueError):
        s.schedule("t1", "k", c.now() + timedelta(minutes=5))


def test_a_timer_fires_once_even_if_the_handler_raises():
    c, s = make()
    s.schedule("t1", "k", c.now())

    def boom(t):
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        s.tick(boom)
    assert s.tick(lambda t: None) == []  # not re-fired: handlers must be idempotent, not retried blindly


def test_timezone_offsets_compare_correctly():
    c, s = make()
    due_ist = c.now().astimezone(IST) + timedelta(minutes=30)  # 30 minutes from now, written in IST
    s.schedule("t1", "k", due_ist)
    c.advance(timedelta(minutes=29))
    assert s.tick(lambda t: None) == []
    c.advance(timedelta(minutes=2))
    assert len(s.tick(lambda t: None)) == 1


def test_rejects_naive_datetimes():
    _, s = make()
    with pytest.raises(ValueError):
        s.schedule("t1", "k", datetime(2026, 10, 3, 12, 0))


def test_pending_lists_only_live_timers_and_survives_restart(tmp_path):
    c = FakeClock()
    path = str(tmp_path / "t.db")
    s1 = Scheduler(c, path)
    s1.schedule("t1", "a", c.now() + timedelta(hours=1))
    s1.schedule("t1", "b", c.now() + timedelta(hours=2))
    s1.cancel("t1", "b")
    s2 = Scheduler(c, path)  # "process restart"
    assert [t.kind for t in s2.pending("t1")] == ["a"]
    c.advance(timedelta(hours=1))
    assert [t.kind for t in s2.tick(lambda t: None)] == ["a"]
