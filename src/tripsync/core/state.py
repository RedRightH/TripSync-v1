"""Trip state machine: the only definition of which transitions are legal.

Mirrors the PRD's state table. Three transitions beyond the PRD's first draft are explicit
here and should be added to the PRD: `release_denied` (LOCKED or BOOKING -> ESCALATED, when
the policy layer refuses or fails to release money) and `vendor_declined` (BOOKING -> FILTERING).
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    DRAFT = "DRAFT"
    ELICITING = "ELICITING"
    COMMITTING = "COMMITTING"
    FILTERING = "FILTERING"
    LOCKED = "LOCKED"
    BOOKING = "BOOKING"
    CONFIRMED = "CONFIRMED"
    REBOOKING = "REBOOKING"
    ESCALATED = "ESCALATED"
    REFUNDING = "REFUNDING"
    CLOSED = "CLOSED"


class IllegalTransition(Exception):
    pass


S = State
_PRE_BOOKING = (S.ELICITING, S.COMMITTING, S.FILTERING, S.LOCKED, S.ESCALATED)

TRANSITIONS: dict[tuple[State, str], State] = {
    (S.DRAFT, "trigger_accepted"): S.ELICITING,
    (S.ELICITING, "preferences_received"): S.COMMITTING,
    (S.COMMITTING, "quorum_cleared"): S.FILTERING,
    (S.COMMITTING, "deadline_below_quorum"): S.REFUNDING,
    (S.FILTERING, "feasible_found"): S.LOCKED,
    (S.FILTERING, "no_feasible"): S.ESCALATED,
    (S.LOCKED, "objection_window_closed"): S.BOOKING,
    (S.LOCKED, "valid_objection"): S.FILTERING,
    (S.LOCKED, "release_denied"): S.ESCALATED,
    (S.BOOKING, "vendor_confirmed"): S.CONFIRMED,
    (S.BOOKING, "vendor_declined"): S.FILTERING,
    (S.BOOKING, "release_denied"): S.ESCALATED,
    (S.CONFIRMED, "vendor_cancelled"): S.REBOOKING,
    (S.REBOOKING, "rebooked"): S.CONFIRMED,
    (S.REBOOKING, "no_rebooking_option"): S.ESCALATED,
    (S.ESCALATED, "escalation_abandoned"): S.REFUNDING,
    (S.REFUNDING, "refunds_complete"): S.CLOSED,
    (S.DRAFT, "host_cancelled"): S.CLOSED,
    **{(s, "host_cancelled"): S.REFUNDING for s in _PRE_BOOKING},
}

TRIGGERS: frozenset[str] = frozenset(t for _, t in TRANSITIONS)


def apply(state: State, trigger: str) -> State:
    try:
        return TRANSITIONS[(state, trigger)]
    except KeyError:
        raise IllegalTransition(f"{trigger!r} is not allowed from {state.value}") from None


def allowed_triggers(state: State) -> list[str]:
    return sorted(t for (s, t) in TRANSITIONS if s == state)
