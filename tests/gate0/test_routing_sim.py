from tests.gate0.contracts import routing_contract
from tripsync.sims.routing import SimulatedRouting


def test_routing_contract():
    routing_contract(SimulatedRouting, "HW", "RSK")


def test_known_times():
    r = SimulatedRouting()
    assert r.travel_minutes("HW", "RSK") == 45
    assert r.travel_minutes("RSK", "HW") == 45
