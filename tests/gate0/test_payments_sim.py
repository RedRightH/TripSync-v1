import pytest

from tests.gate0.contracts import payments_contract, payments_settlement_contract
from tripsync.ports import PaymentsError
from tripsync.sims.payments import SimulatedPayments


def approve(p, ref):
    p.member_approves(ref)


def bounce(p, ref):
    p.member_bounces(ref)


def test_payments_contract():
    payments_contract(SimulatedPayments, approve, bounce)


def test_payments_settlement_contract():
    payments_settlement_contract(SimulatedPayments, approve, bounce)


def test_simulator_is_flagged_simulated():
    assert SimulatedPayments().is_simulated is True


def test_webhook_listener_fires_on_member_action_only():
    p = SimulatedPayments()
    seen = []
    p.listeners.append(seen.append)
    m = p.create_mandate("t", "m1", 10, 20)
    assert seen == []
    p.member_approves(m.ref)
    assert [x.status for x in seen] == ["active"]
    p.refund(m.ref, "k")  # our own action does not echo back as a webhook
    assert len(seen) == 1


def test_member_cannot_approve_twice():
    p = SimulatedPayments()
    m = p.create_mandate("t", "m1", 10, 20)
    p.member_approves(m.ref)
    with pytest.raises(PaymentsError):
        p.member_approves(m.ref)
    with pytest.raises(PaymentsError):
        p.member_bounces(m.ref)


def test_unknown_mandate():
    with pytest.raises(PaymentsError):
        SimulatedPayments().get_mandate("nope")
