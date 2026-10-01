from datetime import datetime, timedelta, timezone

import pytest

from tripsync.core.connection import Transfer, check_transfer, unsafe_transfers
from tripsync.sims.routing import SimulatedRouting

T0 = datetime(2026, 10, 9, 11, 5, tzinfo=timezone.utc)
R = SimulatedRouting()  # HW <-> RSK is 45 minutes


def tr(gap_minutes, a="HW", b="RSK"):
    return Transfer(a, b, T0, T0 + timedelta(minutes=gap_minutes))


def test_safe_when_gap_covers_travel_plus_margin():
    c = check_transfer(tr(90), R, margin_minutes=30)
    assert c.ok and c.required_minutes == 75 and c.actual_minutes == 90


def test_exactly_the_required_buffer_is_safe():
    assert check_transfer(tr(75), R, 30).ok


def test_one_minute_short_is_unsafe_with_a_readable_reason():
    c = check_transfer(tr(74), R, 30)
    assert not c.ok and "74 min available, 75 min needed" in c.reason


def test_same_place_needs_only_the_margin():
    assert check_transfer(tr(30, "HW", "HW"), R, 30).ok
    assert not check_transfer(tr(29, "HW", "HW"), R, 30).ok


def test_arrival_after_departure_is_unsafe():
    c = check_transfer(tr(-20), R, 0)
    assert not c.ok and c.actual_minutes == -20


def test_unknown_route_fails_closed():
    c = check_transfer(tr(600, "HW", "ATLANTIS"), R, 30)
    assert not c.ok and c.required_minutes is None and "unknown route" in c.reason


def test_negative_margin_rejected():
    with pytest.raises(ValueError):
        check_transfer(tr(90), R, -1)


def test_unsafe_transfers_lists_only_the_failures():
    bad = unsafe_transfers([tr(200), tr(10), tr(500, "HW", "ATLANTIS")], R, 30)
    assert len(bad) == 2
