"""Policy layer: owns the group's limits and is the ONLY code that settles or refunds money.

The agent (a language model) proposes; this layer decides. Nothing else in the codebase may
call `payments.settle` or `payments.refund` (enforced by a source-scan test).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable

from tripsync.core.events import EventLog
from tripsync.ports import InsufficientAuthorization, PaymentsPort, Refund, Settlement

CONDITIONS = ("quorum", "budget_cap", "hard_filters", "ground_trust", "objection_window", "funds_authorized")
REFUND_REASONS = frozenset({"quorum_missed", "host_cancelled", "escalation_abandoned"})


class LimitsLocked(Exception):
    pass


class PolicyViolation(Exception):
    pass


class VendorDeclined(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    """Set by the group. The agent never edits these."""

    budget_cap_paise: int
    window_start: date
    window_end: date
    quorum_share: float
    quorum_min_members: int
    decision_deadline: datetime
    objection_window: timedelta
    buffer_margin_minutes: int
    deposit_paise: int
    cap_per_member_paise: int

    def __post_init__(self) -> None:
        if self.budget_cap_paise <= 0:
            raise ValueError("budget cap must be positive")
        if self.window_end < self.window_start:
            raise ValueError("date window ends before it starts")
        if not 0 < self.quorum_share <= 1:
            raise ValueError("quorum share must be in (0, 1]")
        if self.quorum_min_members < 1:
            raise ValueError("quorum needs at least one member")
        if self.decision_deadline.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        if self.objection_window <= timedelta(0):
            raise ValueError("objection window must be positive")
        if self.buffer_margin_minutes < 0:
            raise ValueError("buffer margin must be >= 0")
        if not 0 < self.deposit_paise <= self.cap_per_member_paise:
            raise ValueError("need 0 < deposit <= per-member cap")


class TripLimits:
    """Limits can be amended until the first deposit clears, then they are frozen."""

    def __init__(self, limits: Limits) -> None:
        self._limits = limits
        self._locked = False

    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def locked(self) -> bool:
        return self._locked

    def lock(self) -> None:
        self._locked = True

    def amend(self, new: Limits) -> None:
        if self._locked:
            raise LimitsLocked("limits are frozen once the first deposit has cleared")
        self._limits = new


def quorum_needed(ranked_members: int, limits: Limits) -> int:
    return max(math.ceil(limits.quorum_share * ranked_members), limits.quorum_min_members)


@dataclass(frozen=True)
class ReleaseInputs:
    ranked_members: int
    cleared_deposits: int
    chosen_cost_paise: int
    hard_filters_passed: bool
    trust_passed: bool
    objection_window_closed: bool
    open_valid_objection: bool
    authorized_paise: int


@dataclass(frozen=True)
class ReleaseDecision:
    allowed: bool
    failed: tuple[str, ...]


def check_release(limits: Limits, i: ReleaseInputs) -> ReleaseDecision:
    failed = []
    if i.cleared_deposits < quorum_needed(i.ranked_members, limits):
        failed.append("quorum")
    if i.chosen_cost_paise > limits.budget_cap_paise:
        failed.append("budget_cap")
    if not i.hard_filters_passed:
        failed.append("hard_filters")
    if not i.trust_passed:
        failed.append("ground_trust")
    if not i.objection_window_closed or i.open_valid_objection:
        failed.append("objection_window")
    if i.chosen_cost_paise > i.authorized_paise:
        failed.append("funds_authorized")
    return ReleaseDecision(not failed, tuple(failed))


@dataclass(frozen=True)
class BookingOutcome:
    status: str  # booked | denied | vendor_declined | already_booked
    failed: tuple[str, ...] = ()
    vendor_ref: str | None = None
    settlement: Settlement | None = None


class PolicyLayer:
    def __init__(self, payments: PaymentsPort, log: EventLog) -> None:
        self._payments = payments
        self._log = log

    def authorize_and_book(
        self,
        trip_id: str,
        limits: Limits,
        inputs: ReleaseInputs,
        *,
        mandates: list[tuple[str, str]],
        vendor_name: str,
        amount_paise: int,
        book_with_vendor: Callable[[], str],
        on_authorized: Callable[[], None] = lambda: None,
    ) -> BookingOutcome:
        """Check every release condition; only then book with the vendor and settle the money.

        The caller's claims about cleared deposits and authorized funds are NOT trusted: both
        are recomputed here from the payments port using the trip's mandates.
        """
        done = [e for e in self._log.for_trip(trip_id) if e.kind == "booked"]
        if done:
            return BookingOutcome("already_booked", vendor_ref=done[0].payload.get("vendor_ref"))

        active = [m for m in (self._payments.get_mandate(ref) for _, ref in mandates) if m.status == "active"]
        inputs = replace(
            inputs,
            cleared_deposits=len(active),
            authorized_paise=sum(m.cap_paise for m in active),
            ranked_members=len(mandates),
        )
        decision = check_release(limits, inputs)
        if not decision.allowed:
            self._log.append(trip_id, "release_denied", {"failed": list(decision.failed)}, rule="R1")
            return BookingOutcome("denied", decision.failed)

        on_authorized()  # lets the caller record "booking has started" before the vendor is contacted
        try:
            vendor_ref = book_with_vendor()
        except VendorDeclined as e:
            self._log.append(trip_id, "vendor_declined", {"reason": str(e)}, rule="R1")
            return BookingOutcome("vendor_declined")

        try:
            settlement = self._payments.settle(trip_id, vendor_name, amount_paise, f"settle:{trip_id}")
        except InsufficientAuthorization:
            self._log.append(trip_id, "release_denied", {"failed": ["funds_authorized"], "vendor_ref": vendor_ref}, rule="R1")
            return BookingOutcome("denied", ("funds_authorized",), vendor_ref=vendor_ref)

        self._log.append(
            trip_id,
            "booked",
            {"vendor_ref": vendor_ref, "settlement": settlement.ref, "amount_paise": amount_paise},
            rule="R1",
            idem_key=f"booked:{trip_id}",
        )
        return BookingOutcome("booked", vendor_ref=vendor_ref, settlement=settlement)

    def refund_all(self, trip_id: str, mandates: list[tuple[str, str]], reason: str) -> list[Refund]:
        """Refund every active deposit, exactly once. `mandates` is [(member_id, mandate_ref)]."""
        if reason not in REFUND_REASONS:
            raise PolicyViolation(f"refund reason {reason!r} is not allowed")
        refunds: list[Refund] = []
        for member_id, ref in mandates:
            key = f"refund:{trip_id}:{member_id}"
            status = self._payments.get_mandate(ref).status
            if status not in ("active", "revoked"):
                continue  # nothing was collected from this member
            r = self._payments.refund(ref, key)
            refunds.append(r)
            self._log.append(
                trip_id, "refunded", {"member_id": member_id, "refund": r.ref, "reason": reason}, rule="R7", idem_key=key
            )
        return refunds
