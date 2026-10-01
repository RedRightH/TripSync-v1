from datetime import datetime, timedelta, timezone

import pytest

from tripsync.clock import FakeClock, SystemClock


def test_fake_clock_starts_aware_and_advances():
    c = FakeClock()
    t0 = c.now()
    assert t0.tzinfo is not None
    assert c.advance(timedelta(minutes=10)) == t0 + timedelta(minutes=10)
    assert c.now() == t0 + timedelta(minutes=10)


def test_fake_clock_does_not_tick_by_itself():
    c = FakeClock()
    assert c.now() == c.now()


def test_fake_clock_rejects_going_backwards():
    c = FakeClock()
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        c.set(c.now() - timedelta(seconds=1))


def test_fake_clock_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 10, 2))
    c = FakeClock()
    with pytest.raises(ValueError):
        c.set(datetime(2030, 1, 1))


def test_fake_clock_set_forward():
    c = FakeClock()
    target = c.now() + timedelta(days=1)
    assert c.set(target) == target


def test_system_clock_is_aware_utc_and_monotone_enough():
    s = SystemClock()
    a, b = s.now(), s.now()
    assert a.tzinfo is not None and a.utcoffset() == timedelta(0)
    assert b >= a
