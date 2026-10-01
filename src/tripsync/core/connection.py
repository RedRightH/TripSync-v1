"""Connection-buffer check: the hard filter for transfers that are too tight to be safe.

required buffer = travel time between the two places (RoutingPort) + a safety margin.
It fails closed: if a route is unknown, the transfer is treated as unsafe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tripsync.ports import RoutingPort, UnknownPlace


@dataclass(frozen=True)
class Transfer:
    from_place: str  # where the previous leg arrives
    to_place: str  # where the next leg departs from
    arrive: datetime
    depart: datetime


@dataclass(frozen=True)
class TransferCheck:
    ok: bool
    required_minutes: int | None
    actual_minutes: int
    reason: str


def check_transfer(transfer: Transfer, routing: RoutingPort, margin_minutes: int) -> TransferCheck:
    if margin_minutes < 0:
        raise ValueError("margin must be >= 0")
    actual = int((transfer.depart - transfer.arrive).total_seconds() // 60)
    try:
        travel = routing.travel_minutes(transfer.from_place, transfer.to_place)
    except UnknownPlace as e:
        return TransferCheck(False, None, actual, f"unknown route: {e}")
    required = travel + margin_minutes
    if actual < required:
        return TransferCheck(False, required, actual, f"{actual} min available, {required} min needed")
    return TransferCheck(True, required, actual, "ok")


def unsafe_transfers(transfers, routing: RoutingPort, margin_minutes: int) -> list[TransferCheck]:
    checks = [check_transfer(t, routing, margin_minutes) for t in transfers]
    return [c for c in checks if not c.ok]
