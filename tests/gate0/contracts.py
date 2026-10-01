"""Reusable contract tests. A simulator AND its future real adapter must both pass these.

Each function takes a zero-argument factory that returns a fresh implementation.
Behaviour that only a simulator can trigger (a member approving a mandate) is passed in
as an `approve` callback so the same contract can later drive a real sandbox.
"""
from __future__ import annotations

from datetime import date

import pytest

from tripsync.ports import (
    InsufficientAuthorization,
    MandateNotRefundable,
    UnknownAudio,
    UnknownPlace,
)


# ---------------- payments ----------------

def payments_contract(factory, approve, bounce):
    p = factory()
    assert isinstance(p.is_simulated, bool)

    # mandate lifecycle
    m = p.create_mandate("trip1", "m1", 50_000, 1_000_000)
    assert m.status == "pending" and m.deposit_paise == 50_000
    approve(p, m.ref)
    assert p.get_mandate(m.ref).status == "active"

    m2 = p.create_mandate("trip1", "m2", 50_000, 1_000_000)
    bounce(p, m2.ref)
    assert p.get_mandate(m2.ref).status == "failed"

    # a failed mandate can never be refunded (nothing was collected)
    with pytest.raises(MandateNotRefundable):
        p.refund(m2.ref, "k-failed")

    # refund is idempotent and returns the deposit exactly once
    r1 = p.refund(m.ref, "refund:trip1:m1")
    r2 = p.refund(m.ref, "refund:trip1:m1")
    assert r1 == r2 and r1.amount_paise == 50_000
    assert p.get_mandate(m.ref).status == "revoked"
    # a different key cannot refund it again
    with pytest.raises(MandateNotRefundable):
        p.refund(m.ref, "another-key")

    # bad input
    with pytest.raises(ValueError):
        p.create_mandate("trip1", "m3", 0, 10)
    with pytest.raises(ValueError):
        p.create_mandate("trip1", "m3", 100, 50)


def payments_settlement_contract(factory, approve, bounce):
    p = factory()
    refs = []
    for i in range(3):
        m = p.create_mandate("trip2", f"m{i}", 50_000, 400_000)
        approve(p, m.ref)
        refs.append(m.ref)

    # cannot settle more than the authorised total
    with pytest.raises(InsufficientAuthorization):
        p.settle("trip2", "vendorX", 1_200_001, "stl:1")

    with pytest.raises(ValueError):
        p.settle("trip2", "vendorX", 0, "stl:0")

    s1 = p.settle("trip2", "vendorX", 900_000, "stl:trip2")
    s2 = p.settle("trip2", "vendorX", 900_000, "stl:trip2")  # retried webhook / timeout
    assert s1 == s2
    assert len(getattr(p, "settlements", [s1])) == 1, "idempotent settle must not double-charge"

    # settled deposits can no longer be refunded
    with pytest.raises(MandateNotRefundable):
        p.refund(refs[0], "late-refund")

    # other trips are unaffected
    assert p.get_mandate(p.create_mandate("trip3", "m", 1, 2).ref).status == "pending"


# ---------------- voice ----------------

def voice_contract(factory, known_ref, unknown_ref="does-not-exist"):
    v = factory()
    t = v.transcribe(known_ref)
    assert isinstance(t.text, str) and t.text
    assert 0.0 <= t.confidence <= 1.0
    assert t.language
    assert v.transcribe(known_ref) == t, "same audio must give the same transcript"
    with pytest.raises(UnknownAudio):
        v.transcribe(unknown_ref)


# ---------------- inventory ----------------

def inventory_contract(factory, origin, destination, on: date):
    inv = factory()
    offers = inv.search(origin, destination, on)
    assert offers, "fixture route must return offers"
    for o in offers:
        assert o.origin == origin and o.destination == destination
        assert o.price_paise > 0
        assert 0.0 <= o.confirmation_probability <= 1.0
        assert o.arrive >= o.depart and o.depart.tzinfo is not None
    assert [o.id for o in offers] == [o.id for o in inv.search(origin, destination, on)]
    assert inv.search("NOWHERE", "NOWHERE2", on) == []


# ---------------- routing ----------------

def routing_contract(factory, a, b):
    r = factory()
    assert r.travel_minutes(a, a) == 0
    assert r.travel_minutes(a, b) == r.travel_minutes(b, a) > 0
    with pytest.raises(UnknownPlace):
        r.travel_minutes(a, "ATLANTIS")
