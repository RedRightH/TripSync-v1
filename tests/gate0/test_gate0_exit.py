"""Gate 0 exit: every port has a simulator, and every implementation passes its contract."""
from datetime import date

from tests.gate0.contracts import (
    inventory_contract,
    payments_contract,
    payments_settlement_contract,
    routing_contract,
    voice_contract,
)
from tripsync.ports import port_registry

CONTRACTS = {
    "payments": lambda f: (
        payments_contract(f, lambda p, r: p.member_approves(r), lambda p, r: p.member_bounces(r)),
        payments_settlement_contract(f, lambda p, r: p.member_approves(r), lambda p, r: p.member_bounces(r)),
    ),
    "voice": lambda f: voice_contract(f, "vn_clear_hindi"),
    "inventory": lambda f: inventory_contract(f, "DEL", "HW", date(2026, 10, 9)),
    "routing": lambda f: routing_contract(f, "HW", "RSK"),
}


def test_every_port_has_a_contract_and_a_simulator():
    specs = port_registry()
    assert {s.name for s in specs} == set(CONTRACTS) == {"payments", "voice", "inventory", "routing"}
    for s in specs:
        assert s.simulators, f"{s.name} has no simulator"


def test_every_implementation_satisfies_its_protocol_and_contract():
    for s in port_registry():
        for impl in s.simulators:
            assert isinstance(impl(), s.protocol), f"{impl.__name__} does not satisfy {s.name} port"
            assert impl().is_simulated is True
            CONTRACTS[s.name](impl)
