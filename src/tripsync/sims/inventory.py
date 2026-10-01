"""Simulated Ixigo / ConfirmTkt rail: deterministic fixtures, not live fares or odds."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tripsync.ports import Offer

IST = timezone(timedelta(hours=5, minutes=30))


def _fixture(on: date) -> list[Offer]:
    def at(h: int, m: int, plus_days: int = 0) -> datetime:
        return datetime(on.year, on.month, on.day, h, m, tzinfo=IST) + timedelta(days=plus_days)

    return [
        Offer("TRN-A", "train", "DEL", "HW", at(6, 45), at(11, 5), 55000, 0.92),
        Offer("TRN-B", "train", "DEL", "HW", at(22, 30), at(3, 40, 1), 38000, 0.35),
        Offer("BUS-A", "bus", "DEL", "RSK", at(21, 0), at(4, 30, 1), 90000, 0.99),
        Offer("HTL-A", "hotel", "RSK", "RSK", at(14, 0), at(11, 0, 2), 360000, 0.97),
        Offer("HTL-B", "hotel", "RSK", "RSK", at(14, 0), at(11, 0, 2), 240000, 0.80),
    ]


class SimulatedInventory:
    is_simulated = True

    def search(self, origin: str, destination: str, on: date) -> list[Offer]:
        return [o for o in _fixture(on) if o.origin == origin and o.destination == destination]
