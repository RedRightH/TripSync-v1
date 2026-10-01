from datetime import date

from tests.gate0.contracts import inventory_contract
from tripsync.sims.inventory import SimulatedInventory


def test_inventory_contract():
    inventory_contract(SimulatedInventory, "DEL", "HW", date(2026, 10, 9))


def test_low_confirmation_option_exists_to_exercise_the_risk_discount():
    offers = SimulatedInventory().search("DEL", "HW", date(2026, 10, 9))
    assert min(o.confirmation_probability for o in offers) < 0.5
    assert max(o.confirmation_probability for o in offers) > 0.9
