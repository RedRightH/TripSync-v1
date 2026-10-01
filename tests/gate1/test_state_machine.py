import pytest
from hypothesis import given, strategies as st

from tripsync.core.state import (
    TRANSITIONS,
    TRIGGERS,
    IllegalTransition,
    State,
    allowed_triggers,
    apply,
)


def test_prd_table_transitions():
    expected = [
        (State.DRAFT, "trigger_accepted", State.ELICITING),
        (State.ELICITING, "preferences_received", State.COMMITTING),
        (State.COMMITTING, "quorum_cleared", State.FILTERING),
        (State.COMMITTING, "deadline_below_quorum", State.REFUNDING),
        (State.FILTERING, "feasible_found", State.LOCKED),
        (State.FILTERING, "no_feasible", State.ESCALATED),
        (State.LOCKED, "objection_window_closed", State.BOOKING),
        (State.LOCKED, "valid_objection", State.FILTERING),
        (State.BOOKING, "vendor_confirmed", State.CONFIRMED),
        (State.CONFIRMED, "vendor_cancelled", State.REBOOKING),
        (State.REBOOKING, "rebooked", State.CONFIRMED),
        (State.REFUNDING, "refunds_complete", State.CLOSED),
    ]
    for s, t, n in expected:
        assert apply(s, t) == n


def test_illegal_transitions_raise():
    with pytest.raises(IllegalTransition):
        apply(State.DRAFT, "objection_window_closed")
    with pytest.raises(IllegalTransition):
        apply(State.COMMITTING, "vendor_confirmed")
    with pytest.raises(IllegalTransition):
        apply(State.CLOSED, "trigger_accepted")
    with pytest.raises(IllegalTransition):
        apply(State.LOCKED, "not_a_trigger")


def test_cannot_book_without_passing_through_locked():
    sources = [s for (s, t), n in TRANSITIONS.items() if n == State.BOOKING]
    assert sources == [State.LOCKED]


def test_cannot_cancel_after_booking_starts():
    for s in (State.BOOKING, State.CONFIRMED, State.REBOOKING, State.REFUNDING, State.CLOSED):
        assert "host_cancelled" not in allowed_triggers(s)


def test_host_cancel_before_booking_always_refunds_unless_nothing_collected():
    assert apply(State.DRAFT, "host_cancelled") == State.CLOSED
    for s in (State.ELICITING, State.COMMITTING, State.FILTERING, State.LOCKED, State.ESCALATED):
        assert apply(s, "host_cancelled") == State.REFUNDING


def test_closed_is_absorbing():
    assert allowed_triggers(State.CLOSED) == []


def test_every_state_reachable_and_none_is_a_dead_end():
    reach = {State.DRAFT}
    frontier = [State.DRAFT]
    while frontier:
        s = frontier.pop()
        for t in allowed_triggers(s):
            n = apply(s, t)
            if n not in reach:
                reach.add(n)
                frontier.append(n)
    assert reach == set(State)
    # CLOSED is reachable from every state
    for start in State:
        seen, stack = {start}, [start]
        while stack:
            s = stack.pop()
            for t in allowed_triggers(s):
                n = apply(s, t)
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        assert State.CLOSED in seen, f"{start} cannot reach CLOSED"


@given(st.lists(st.sampled_from(sorted(TRIGGERS)), max_size=40))
def test_random_trigger_sequences_never_break_invariants(seq):
    state = State.DRAFT
    for trig in seq:
        try:
            new = apply(state, trig)
        except IllegalTransition:
            continue  # rejected, state unchanged
        if new == State.BOOKING:
            assert state == State.LOCKED
        if new == State.CONFIRMED:
            assert state in (State.BOOKING, State.REBOOKING)
        if new == State.CLOSED:
            assert state in (State.DRAFT, State.REFUNDING)
        state = new
        assert isinstance(state, State)
    if state == State.CLOSED:
        assert allowed_triggers(state) == []
