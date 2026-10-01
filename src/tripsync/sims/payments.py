"""Simulated Pine Labs rail.

IMPORTANT: this models the *behaviour we need* (mandate lifecycle, idempotent refund,
bounded settlement). It is NOT a copy of Pine Labs' real API. Phase 0.1 replaces the
assumptions here with the real endpoints and response shapes from Pine Labs' docs, and
the real adapter must pass the same contract tests.
"""
from __future__ import annotations

from typing import Callable

from tripsync.ports import (
    InsufficientAuthorization,
    Mandate,
    MandateNotRefundable,
    PaymentsError,
    Refund,
    Settlement,
)


class SimulatedPayments:
    is_simulated = True

    def __init__(self) -> None:
        self._mandates: dict[str, Mandate] = {}
        self._refunds_by_key: dict[str, Refund] = {}
        self._settlements_by_key: dict[str, Settlement] = {}
        self._seq = 0
        self.listeners: list[Callable[[Mandate], None]] = []  # simulated webhooks

    # ----- port methods -----
    def create_mandate(self, trip_id: str, member_id: str, deposit_paise: int, cap_paise: int) -> Mandate:
        if deposit_paise <= 0 or cap_paise < deposit_paise:
            raise ValueError("need 0 < deposit <= cap")
        self._seq += 1
        m = Mandate(f"mnd_{self._seq:04d}", trip_id, member_id, "pending", deposit_paise, cap_paise)
        self._mandates[m.ref] = m
        return m

    def get_mandate(self, ref: str) -> Mandate:
        try:
            return self._mandates[ref]
        except KeyError:
            raise PaymentsError(f"unknown mandate {ref}") from None

    def refund(self, mandate_ref: str, idempotency_key: str) -> Refund:
        if idempotency_key in self._refunds_by_key:
            return self._refunds_by_key[idempotency_key]
        m = self.get_mandate(mandate_ref)
        if m.status != "active":
            raise MandateNotRefundable(f"mandate {m.ref} is {m.status}, only active deposits can be refunded")
        self._set(m, "revoked", notify=False)
        r = Refund(f"rfd_{len(self._refunds_by_key) + 1:04d}", m.ref, m.deposit_paise, idempotency_key)
        self._refunds_by_key[idempotency_key] = r
        return r

    def settle(self, trip_id: str, vendor: str, amount_paise: int, idempotency_key: str) -> Settlement:
        if idempotency_key in self._settlements_by_key:
            return self._settlements_by_key[idempotency_key]
        if amount_paise <= 0:
            raise ValueError("settlement amount must be positive")
        active = [m for m in self._mandates.values() if m.trip_id == trip_id and m.status == "active"]
        available = sum(m.cap_paise for m in active)
        if amount_paise > available:
            raise InsufficientAuthorization(f"need {amount_paise}, authorized {available}")
        for m in active:
            self._set(m, "settled", notify=False)
        s = Settlement(f"stl_{len(self._settlements_by_key) + 1:04d}", trip_id, vendor, amount_paise, idempotency_key)
        self._settlements_by_key[idempotency_key] = s
        return s

    # ----- curtain controls: the outside world acting on the simulator -----
    def member_approves(self, ref: str) -> None:
        self._transition(ref, "pending", "active")

    def member_bounces(self, ref: str) -> None:
        self._transition(ref, "pending", "failed")

    # ----- inspection helpers for tests -----
    @property
    def refunds(self) -> list[Refund]:
        return list(self._refunds_by_key.values())

    @property
    def settlements(self) -> list[Settlement]:
        return list(self._settlements_by_key.values())

    # ----- internals -----
    def _transition(self, ref: str, expected: str, new: str) -> None:
        m = self.get_mandate(ref)
        if m.status != expected:
            raise PaymentsError(f"mandate {ref} is {m.status}, expected {expected}")
        self._set(m, new, notify=True)

    def _set(self, m: Mandate, status: str, notify: bool) -> None:
        updated = Mandate(m.ref, m.trip_id, m.member_id, status, m.deposit_paise, m.cap_paise)
        self._mandates[m.ref] = updated
        if notify:
            for cb in list(self.listeners):
                cb(updated)
