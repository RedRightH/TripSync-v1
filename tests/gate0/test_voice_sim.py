from tests.gate0.contracts import voice_contract
from tripsync.ports import Transcript
from tripsync.sims.voice import SimulatedVoice


def test_voice_contract():
    voice_contract(SimulatedVoice, "vn_clear_hindi")


def test_low_confidence_fixture_exists_for_clarification_flow():
    t = SimulatedVoice().transcribe("vn_noisy")
    assert t.confidence < 0.6


def test_custom_fixtures():
    v = SimulatedVoice({"x": Transcript("hello", 0.8, "en")})
    assert v.transcribe("x").text == "hello"
