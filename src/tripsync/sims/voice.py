"""Simulated Gnani rail. Fixture-driven; does not claim to be Gnani's real output.

Phase 2.3 replaces this with the real Gnani call: raw audio goes in, and the exact
response (transcript + confidence) is what the core consumes.
"""
from __future__ import annotations

from tripsync.ports import Transcript, UnknownAudio


DEFAULT_FIXTURES: dict[str, Transcript] = {
    "vn_clear_hindi": Transcript("mujhe Rishikesh chalega, budget teen hazaar tak", 0.93, "hi-en"),
    "vn_clear_english": Transcript("I prefer rafting and a ground floor room", 0.96, "en"),
    "vn_noisy": Transcript("... pahad ya ... beach ... pata nahi", 0.41, "hi-en"),
}


class SimulatedVoice:
    is_simulated = True

    def __init__(self, fixtures: dict[str, Transcript] | None = None) -> None:
        self._fixtures = dict(DEFAULT_FIXTURES if fixtures is None else fixtures)

    def transcribe(self, audio_ref: str) -> Transcript:
        try:
            return self._fixtures[audio_ref]
        except KeyError:
            raise UnknownAudio(audio_ref) from None
