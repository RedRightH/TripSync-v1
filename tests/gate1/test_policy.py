import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tripsync.clock import FakeClock
from tripsync.core.events import EventLog
from tripsync.core.policy import (
    CONDITIONS,
    Limits,
    LimitsLocked,
    PolicyLayer,
    PolicyViolation,
    ReleaseInputs,
    TripLimits,
    VendorDeclined,
    check_release,
    quorum_needed,
)
from tripsync.sims.payments import SimulatedPayments

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
COST = 2_400_000  # 4 members x 1,000,000 authorised cap; 3 cleared => 3,000,000 authorised


def limits(**kw):
    base = dict(
        budget_cap_paise=4_000_000,
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 12),
        quorum_share=0.75,
        quorum_min_members=3,
        decision_deadline=NOW + timedelta(hours=2),
        objection_window=timedelta(minutes=10),
        buffer_margin_minutes=30,
        deposit_paise=50_000,
        cap_per_member_paise=1_000_000,
    )
    base.update(kw)
    return Limits(**base)


def inputs(**kw):
    base = dict(
        ranked_members=4,
        cleared_deposits=3,
        chosen_cost_paise=COST,
        hard_filters_passed=True,
        trust_passed=True,
        objection_window_closed=True,
        open_valid_objection=False,
        authorized_paise=3_000_000,
    )
    base.update(kw)
    return ReleaseInputs(**base)


# ---------------- limits ----------------

def test_limits_validate_their_own_invariants():
    for bad in (
        dict(budget_cap_paise=0),
        dict(window_end=date(2026, 10, 1)),
        dict(quorum_share=0),
        dict(quorum_share=1.2),
        dict(quorum_min_members=0),
        dict(decision_deadline=datetime(2026, 10, 2, 11, 0)),
        dict(objection_window=timedelta(0)),
        dict(buffer_margin_minutes=-1),
        dict(deposit_paise=2_000_000),
    ):
        with pytest.raises(ValueError):
            limits(**bad)


def test_limits_are_immutable():
    with pytest.raises(Exception):
        limits().budget_cap_paise = 1  # frozen dataclass


def test_limits_can_be_amended_until_locked_then_never():
    tl = TripLimits(limits())
    tl.amend(limits(budget_cap_paise=5_000_000))
    assert tl.limits.budget_cap_paise == 5_000_000
    tl.lock()
    with pytest.raises(LimitsLocked):
        tl.amend(limits(budget_cap_paise=9_000_000))
    assert tl.limits.budget_cap_paise == 5_000_000


# ---------------- quorum ----------------

@pytest.mark.parametrize(
    "ranked,share,min_members,needed",
    [(4, 0.75, 3, 3), (5, 0.75, 3, 4), (10, 0.5, 3, 5), (3, 0.5, 3, 3), (2, 0.75, 3, 3), (4, 1.0, 1, 4)],
)
def test_quorum_needed(ranked, share, min_members, needed):
    assert quorum_needed(ranked, limits(quorum_share=share, quorum_min_members=min_members)) == needed


# ---------------- the release conditions ----------------

def test_all_conditions_true_allows_release():
    d = check_release(limits(), inputs())
    assert d.allowed and d.failed == ()


@pytest.mark.parametrize(
    "change,failed",
    [
        (dict(cleared_deposits=2), "quorum"),
        (dict(chosen_cost_paise=4_000_001, authorized_paise=9_000_000), "budget_cap"),
        (dict(hard_filters_passed=False), "hard_filters"),
        (dict(trust_passed=False), "ground_trust"),
        (dict(objection_window_closed=False), "objection_window"),
        (dict(open_valid_objection=True), "objection_window"),
        (dict(authorized_paise=COST - 1), "funds_authorized"),
    ],
)
def test_each_condition_alone_blocks_release(change, failed):
    d = check_release(limits(), inputs(**change))
    assert not d.allowed and d.failed == (failed,)
    assert failed in CONDITIONS


def test_several_failures_are_all_reported():
    d = check_release(limits(), inputs(cleared_deposits=1, trust_passed=False, objection_window_closed=False))
    assert set(d.failed) == {"quorum", "ground_trust", "objection_window"}


# ---------------- the policy layer and the money ----------------

class Fixture:
    def __init__(self, cleared=3):
        self.clock = FakeClock()
        self.log = EventLog(self.clock)
        self.pay = SimulatedPayments()
        self.policy = PolicyLayer(self.pay, self.log)
        self.mandates = []
        for i in range(4):
            m = self.pay.create_mandate("t1", f"m{i}", 50_000, 1_000_000)
            if i < cleared:
                self.pay.member_approves(m.ref)
            else:
                self.pay.member_bounces(m.ref)
            self.mandates.append((f"m{i}", m.ref))
        self.vendor_calls = 0

    def vendor(self, decline=False):
        def book():
            self.vendor_calls += 1
            if decline:
                raise VendorDeclined("sold out")
            return "VND-123"

        return book

    def book(self, inp=None, decline=False, amount=COST, lim=None):
        return self.policy.authorize_and_book(
            "t1", lim or limits(), inp or inputs(), mandates=self.mandates,
            vendor_name="stay-co", amount_paise=amount, book_with_vendor=self.vendor(decline),
        )


def test_denied_release_moves_no_money_and_does_not_even_call_the_vendor():
    f = Fixture(cleared=2)
    out = f.book()
    assert out.status == "denied" and out.failed == ("quorum", "funds_authorized")
    assert f.pay.settlements == [] and f.vendor_calls == 0
    ev = [e for e in f.log.for_trip("t1") if e.kind == "release_denied"]
    assert len(ev) == 1 and ev[0].payload["failed"] == ["quorum", "funds_authorized"] and ev[0].rule == "R1"


def test_allowed_release_books_once_then_settles_once():
    f = Fixture()
    out = f.book()
    assert out.status == "booked" and out.vendor_ref == "VND-123"
    assert len(f.pay.settlements) == 1 and f.pay.settlements[0].amount_paise == COST
    assert f.vendor_calls == 1
    assert [e.kind for e in f.log.for_trip("t1")] == ["booked"]


def test_repeating_the_request_does_not_double_book_or_double_charge():
    f = Fixture()
    f.book()
    again = f.book()
    assert again.status == "already_booked"
    assert f.vendor_calls == 1 and len(f.pay.settlements) == 1


def test_vendor_decline_moves_no_money():
    f = Fixture()
    out = f.book(decline=True)
    assert out.status == "vendor_declined"
    assert f.pay.settlements == []
    assert [e.kind for e in f.log.for_trip("t1")] == ["vendor_declined"]


def test_callers_claims_about_deposits_and_funds_are_not_trusted():
    # Only 3 mandates are really active (3,000,000 authorised). The caller lies on all counts.
    f = Fixture(cleared=3)
    lying = inputs(cleared_deposits=4, authorized_paise=99_000_000, chosen_cost_paise=3_500_000)
    out = f.book(lying, amount=3_500_000, lim=limits(budget_cap_paise=9_000_000))
    assert out.status == "denied" and out.failed == ("funds_authorized",)
    assert f.vendor_calls == 0 and f.pay.settlements == []

    g = Fixture(cleared=2)
    out = g.book(inputs(cleared_deposits=4, authorized_paise=99_000_000))
    assert out.status == "denied" and "quorum" in out.failed
    assert g.pay.settlements == []


# ---------------- refunds ----------------

def test_refund_all_returns_every_cleared_deposit_exactly_once():
    f = Fixture(cleared=2)
    r1 = f.policy.refund_all("t1", f.mandates, "quorum_missed")
    r2 = f.policy.refund_all("t1", f.mandates, "quorum_missed")  # retried
    assert len(r1) == 2 and r1 == r2
    assert len(f.pay.refunds) == 2
    assert sum(r.amount_paise for r in f.pay.refunds) == 100_000
    assert len([e for e in f.log.for_trip("t1") if e.kind == "refunded"]) == 2


def test_failed_mandates_are_skipped_not_refunded():
    f = Fixture(cleared=1)
    assert len(f.policy.refund_all("t1", f.mandates, "host_cancelled")) == 1


def test_refund_requires_an_allowed_reason():
    f = Fixture()
    with pytest.raises(PolicyViolation):
        f.policy.refund_all("t1", f.mandates, "because I said so")
    assert f.pay.refunds == []


def test_nothing_is_refunded_after_money_has_been_settled():
    f = Fixture()
    assert f.book().status == "booked"
    assert f.policy.refund_all("t1", f.mandates, "host_cancelled") == []
    assert f.pay.refunds == []


# ---------------- architecture: nothing else may touch the money ----------------

SRC = Path(__file__).resolve().parents[2] / "src" / "tripsync"
ALLOWED = {SRC / "core" / "policy.py", SRC / "sims" / "payments.py", SRC / "ports.py"}


def test_only_the_policy_layer_calls_settle_or_refund():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in ALLOWED:
            continue
        if re.search(r"\.(settle|refund)\(", path.read_text()):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"money-moving calls outside the policy layer: {offenders}"
