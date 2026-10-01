"""Simulated Delhivery Maps rail: a fixed symmetric travel-time table (minutes).

Phase 4.2 replaces this with real Delhivery Maps distance/ETA data. Whether that data is
self-serve is an open Phase 0 question; if not, a different routing source plugs in here.
"""
from __future__ import annotations

from tripsync.ports import UnknownPlace

_TIMES = {
    frozenset({"HW", "RSK"}): 45,  # Haridwar station <-> Rishikesh stay
    frozenset({"HW", "HW_BUS"}): 25,  # Haridwar station <-> Haridwar bus stand
    frozenset({"RSK", "RSK_BUS"}): 15,
    frozenset({"DEL", "HW"}): 270,
    frozenset({"HW_BUS", "RSK"}): 60,
}
_PLACES = {p for pair in _TIMES for p in pair}


class SimulatedRouting:
    is_simulated = True

    def travel_minutes(self, origin: str, destination: str) -> int:
        for p in (origin, destination):
            if p not in _PLACES:
                raise UnknownPlace(p)
        if origin == destination:
            return 0
        try:
            return _TIMES[frozenset({origin, destination})]
        except KeyError:
            raise UnknownPlace(f"no route {origin}->{destination}") from None
