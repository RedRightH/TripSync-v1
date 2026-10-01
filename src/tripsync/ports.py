"""Ports: the only interfaces the core is allowed to use to reach a partner.

Each partner (Pine Labs, Gnani, Ixigo, Delhivery) is reached through exactly one
port. A simulator and a real adapter implement the same Protocol and must pass the
same contract tests (tests/gate0/contracts.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable


# ---------- Payments (Pine Labs rail) ----------

class PaymentsError(Exception):
    pass


class MandateNotRefundable(PaymentsError):
    pass


class InsufficientAuthorization(PaymentsError):
    pass


@dataclass(frozen=True)
class Mandate:
    ref: str
    trip_id: str
    member_id: str
    status: str  # pending | active | failed | settled | revoked
    deposit_paise: int
    cap_paise: int


@dataclass(frozen=True)
class Refund:
    ref: str
    mandate_ref: str
    amount_paise: int
    idempotency_key: str


@dataclass(frozen=True)
class Settlement:
    ref: str
    trip_id: str
    vendor: str
    amount_paise: int
    idempotency_key: str


@runtime_checkable
class PaymentsPort(Protocol):
    is_simulated: bool

    def create_mandate(self, trip_id: str, member_id: str, deposit_paise: int, cap_paise: int) -> Mandate: ...
    def get_mandate(self, ref: str) -> Mandate: ...
    def refund(self, mandate_ref: str, idempotency_key: str) -> Refund: ...
    def settle(self, trip_id: str, vendor: str, amount_paise: int, idempotency_key: str) -> Settlement: ...


# ---------- Voice (Gnani rail) ----------

class UnknownAudio(Exception):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float  # 0..1
    language: str  # e.g. "hi", "en", "hi-en"


@runtime_checkable
class VoicePort(Protocol):
    is_simulated: bool

    def transcribe(self, audio_ref: str) -> Transcript: ...


# ---------- Inventory (Ixigo rail) ----------

@dataclass(frozen=True)
class Offer:
    id: str
    kind: str  # train | bus | flight | hotel
    origin: str
    destination: str
    depart: datetime
    arrive: datetime
    price_paise: int
    confirmation_probability: float  # 0..1


@runtime_checkable
class InventoryPort(Protocol):
    is_simulated: bool

    def search(self, origin: str, destination: str, on: date) -> list[Offer]: ...


# ---------- Routing (Delhivery Maps rail) ----------

class UnknownPlace(Exception):
    pass


@runtime_checkable
class RoutingPort(Protocol):
    is_simulated: bool

    def travel_minutes(self, origin: str, destination: str) -> int: ...


# ---------- Registry used by the Gate 0 exit check ----------

@dataclass
class PortSpec:
    name: str
    protocol: type
    simulators: list[type] = field(default_factory=list)


def port_registry() -> list[PortSpec]:
    """Every port with every implementation that must pass its contract."""
    from tripsync.sims.inventory import SimulatedInventory
    from tripsync.sims.payments import SimulatedPayments
    from tripsync.sims.routing import SimulatedRouting
    from tripsync.sims.voice import SimulatedVoice

    return [
        PortSpec("payments", PaymentsPort, [SimulatedPayments]),
        PortSpec("voice", VoicePort, [SimulatedVoice]),
        PortSpec("inventory", InventoryPort, [SimulatedInventory]),
        PortSpec("routing", RoutingPort, [SimulatedRouting]),
    ]
