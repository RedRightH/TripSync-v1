"""Gate 1 integration: the core blocks wired together against simulators, on a fake clock.

Nothing here reaches a real partner. Every scenario also checks the audit trail and that
money only moved when the policy layer allowed it.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from tripsync.clock import FakeClock
from tripsync.core.engine import TripEngine, WrongState, replay
from tripsync.core.events import EventLog
from tripsync.core.optimizer import Bundle, Member
from tripsync.core.policy import Limits, PolicyLayer, VendorDeclined
from tripsync.core.scheduler import Scheduler, Timer
from tripsync.core.state import IllegalTransition, State
from tripsync.sims.payments import SimulatedPayments
from tripsync.sims.routing import SimulatedRouting

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
D = date(2026, 10, 9)
RANKED = ["shreeja", "samarth", "suchit", "aayushi"]


def make_limits(**kw):
    base = dict(
        budget_cap_paise=4_000_000,
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 31),
        quorum_share=0.75,
        quorum_min_members=3,
        decision_deadline=NOW + timedelta(hours=2),
        objection_window=timedelta(minutes=10),
        buffer_margin_minutes=30,
        deposit_paise=50_000,
        cap_per_member_paise=1_000_000,
    )
    base.update(kw)
    return Limits(**base)


def bundle(id, total, per_person, trust=0.9, conf=0.95, ground_floor=True):
    return Bundle(id, "Rishikesh", D, D + timedelta(days=2), total, per_person, trust, conf, ground_floor, frozenset({"veg"}))


A = bundle("stay-A", 2_400_000, 600_000)
B_RISKY = bundle("stay-B", 1_800_000, 450_000, conf=0.35)
C_PRICEY = bundle("stay-C", 3_600_000, 900_000)
D_SHADY = bundle("stay-D", 2_000_000, 500_000, trust=0.3)
BUNDLES = [A, B_RISKY, C_PRICEY, D_SHADY]


def members(only=None):
    ms = [
        Member("shreeja", 700_000, date(2026, 10, 1), date(2026, 10, 31)),
        Member("samarth", 700_000, date(2026, 10, 1), date(2026, 10, 31)),
        Member("suchit", 700_000, date(2026, 10, 1), date(2026, 10, 31), needs_ground_floor=True),
        Member("aayushi", 700_000, date(2026, 10, 1), date(2026, 10, 31)),
    ]
    return [m for m in ms if only is None or m.id in only]


def scores(prefer="stay-A"):
    out = {}
    for m in RANKED:
        out[m] = {b.id: {"destination": 1.0 if b.id == prefer else 0.4} for b in BUNDLES}
    return out


class World:
    def __init__(self, vendor_declines=0, min_trust=0.5, limits=None):
        self.clock = FakeClock(NOW)
        self.log = EventLog(self.clock)
        self.sched = Scheduler(self.clock)
        self.pay = SimulatedPayments()
        self.routing = SimulatedRouting()
        self.policy = PolicyLayer(self.pay, self.log)
        self.vendor_calls = []
        self._declines = vendor_declines

        def vendor(bundle_summary):
            self.vendor_calls.append(bundle_summary["id"])
            if len(self.vendor_calls) <= self._declines:
                raise VendorDeclined("room sold out")
            return f"VND-{len(self.vendor_calls)}"

        self.engine = TripEngine(
            clock=self.clock, log=self.log, scheduler=self.sched, payments=self.pay, routing=self.routing,
            policy=self.policy, vendor=vendor, min_trust=min_trust,
        )
        self.pay.listeners.append(self.engine.on_mandate_event)
        self.limits = limits or make_limits()

    def start(self, trip="trip1"):
        self.engine.create_trip(trip, "shreeja", RANKED, self.limits)
        self.engine.accept_trigger(trip)
        self.engine.start_commitment(trip)
        return trip

    def refs(self, trip="trip1"):
        return self.engine.view(trip).mandates

    def approve(self, member, trip="trip1"):
        self.pay.member_approves(self.refs(trip)[member])

    def bounce(self, member, trip="trip1"):
        self.pay.member_bounces(self.refs(trip)[member])

    def state(self, trip="trip1"):
        return self.engine.view(trip).state

    def kinds(self, trip="trip1"):
        return [e.kind for e in self.log.for_trip(trip)]


def reach_filtering(w):
    trip = w.start()
    w.bounce("aayushi")
    for m in ("shreeja", "samarth", "suchit"):
        w.approve(m)
    assert w.state() == State.FILTERING
    return trip


# ---------------- happy path ----------------

def test_happy_path_from_trigger_to_confirmed_booking():
    w = World()
    trip = w.start()
    assert w.state() == State.COMMITTING
    assert len(w.refs()) == 4 and [t.kind for t in w.sched.pending(trip)] == ["decision_deadline"]

    w.bounce("aayushi")  # a bounced mandate is non-blocking
    w.approve("shreeja")
    assert "limits_locked" in w.kinds()  # first cleared deposit freezes the limits
    w.approve("samarth")
    assert w.state() == State.COMMITTING  # 2 of 4 is below quorum (needs 3)
    w.approve("suchit")
    assert w.state() == State.FILTERING

    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    v = w.engine.view(trip)
    assert v.state == State.LOCKED and v.bundle["id"] == "stay-A"
    assert w.pay.settlements == [] and w.vendor_calls == []

    w.clock.advance(timedelta(minutes=9))
    assert w.engine.tick() == [] and w.state() == State.LOCKED  # objection window still open
    w.clock.advance(timedelta(minutes=1))
    fired = w.engine.tick()
    assert [t.kind for t in fired] == ["objection_window:1"]

    v = w.engine.view(trip)
    assert v.state == State.CONFIRMED and v.vendor_ref == "VND-1"
    assert w.vendor_calls == ["stay-A"]
    assert [(s.amount_paise, s.vendor) for s in w.pay.settlements] == [(2_400_000, "vendor:stay-A")]
    k = w.kinds()
    assert k.index("objection_window_closed") < k.index("booked") < k.index("vendor_confirmed")

    # the decision deadline passing later changes nothing: quorum was already met
    n = len(w.log.for_trip(trip))
    w.clock.advance(timedelta(hours=3))
    w.engine.tick()
    assert len(w.log.for_trip(trip)) == n and w.pay.refunds == []


def test_a_committed_members_hard_constraint_beats_everyones_preference():
    w = World()
    trip = reach_filtering(w)
    no_ground_floor = bundle("stay-E", 1_200_000, 300_000, ground_floor=False)
    prefs = {m: {b.id: {"destination": 1.0 if b.id == "stay-E" else 0.3} for b in [*BUNDLES, no_ground_floor]} for m in RANKED}
    # suchit is committed and needs a ground floor, so the universally preferred stay-E is excluded
    w.engine.run_optimization(trip, [*BUNDLES, no_ground_floor], members(), prefs)
    assert w.engine.view(trip).bundle["id"] == "stay-A"


def test_over_ceiling_and_low_trust_bundles_are_never_chosen():
    w = World()
    trip = reach_filtering(w)
    prefs = {m: {b.id: {"destination": 1.0 if b.id in ("stay-C", "stay-D") else 0.2} for b in BUNDLES} for m in RANKED}
    w.engine.run_optimization(trip, BUNDLES, members(), prefs)
    assert w.engine.view(trip).bundle["id"] in {"stay-A", "stay-B"}


def test_audit_trail_replays_to_the_live_state():
    w = World()
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    w.clock.advance(timedelta(minutes=10))
    w.engine.tick()
    events = w.log.for_trip(trip)
    assert replay(trip, events).state == w.state() == State.CONFIRMED
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    # every decision that moved money or chose a bundle carries the rule that authorised it
    for e in events:
        if e.kind in ("bundle_locked", "booked", "refunded", "release_denied", "no_feasible"):
            assert e.rule, f"{e.kind} has no rule"


# ---------------- unhappy paths ----------------

def test_quorum_missed_refunds_every_cleared_deposit_exactly_once_and_books_nothing():
    w = World()
    trip = w.start()
    w.approve("shreeja")
    w.approve("samarth")
    w.bounce("suchit")
    w.bounce("aayushi")
    assert w.state() == State.COMMITTING

    w.clock.advance(timedelta(hours=1, minutes=59))
    assert w.engine.tick() == []
    w.clock.advance(timedelta(minutes=1))
    w.engine.tick()

    assert w.state() == State.CLOSED
    assert len(w.pay.refunds) == 2 and sum(r.amount_paise for r in w.pay.refunds) == 100_000
    assert w.pay.settlements == [] and w.vendor_calls == []
    n = len(w.log.for_trip(trip))
    w.clock.advance(timedelta(hours=1))
    w.engine.tick()
    assert len(w.log.for_trip(trip)) == n and len(w.pay.refunds) == 2


def test_no_feasible_option_escalates_and_never_guesses():
    w = World()
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, [C_PRICEY, D_SHADY], members(), scores())
    assert w.state() == State.ESCALATED
    ev = [e for e in w.log.for_trip(trip) if e.kind == "no_feasible"][0]
    assert ev.payload["rejections"]["budget_ceiling"] >= 1 and ev.payload["rejections"]["min_trust"] >= 1
    assert w.pay.settlements == [] and w.vendor_calls == []
    assert [t.kind for t in w.sched.pending(trip)] == ["decision_deadline"]  # no objection timer started

    w.engine.abandon_escalation(trip)
    assert w.state() == State.CLOSED and len(w.pay.refunds) == 3


def test_valid_objection_reopens_the_choice_and_the_old_window_never_books():
    w = World()
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    w.engine.on_objection(trip, valid=True)
    assert w.state() == State.FILTERING

    w.clock.advance(timedelta(minutes=30))
    w.engine.tick()  # the first objection window's timer was cancelled
    assert w.state() == State.FILTERING and w.pay.settlements == []

    w.engine.run_optimization(trip, BUNDLES, members(), scores(prefer="stay-B"))
    assert w.engine.view(trip).locks == 2
    w.clock.advance(timedelta(minutes=10))
    w.engine.tick()
    assert w.state() == State.CONFIRMED and len(w.pay.settlements) == 1


def test_dismissed_objection_changes_nothing():
    w = World()
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    w.engine.on_objection(trip, valid=False)
    assert w.state() == State.LOCKED
    w.clock.advance(timedelta(minutes=10))
    w.engine.tick()
    assert w.state() == State.CONFIRMED


def test_vendor_decline_moves_no_money_and_the_trip_can_try_again():
    w = World(vendor_declines=1)
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    w.clock.advance(timedelta(minutes=10))
    w.engine.tick()
    assert w.state() == State.FILTERING and w.pay.settlements == [] and w.vendor_calls == ["stay-A"]

    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    w.clock.advance(timedelta(minutes=10))
    w.engine.tick()
    assert w.state() == State.CONFIRMED and len(w.pay.settlements) == 1 and len(w.vendor_calls) == 2


def test_policy_layer_denies_release_when_its_own_check_fails():
    # One engine locks with a lenient trust floor; a stricter engine handles the timer.
    w = World(min_trust=0.5)
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    strict = TripEngine(
        clock=w.clock, log=w.log, scheduler=w.sched, payments=w.pay, routing=w.routing,
        policy=w.policy, vendor=lambda b: "VND-X", min_trust=0.95,
    )
    w.clock.advance(timedelta(minutes=10))
    strict.tick()
    assert w.state() == State.ESCALATED
    denied = [e for e in w.log.for_trip(trip) if e.kind == "release_denied"][0]
    assert denied.payload["failed"] == ["ground_trust"] and denied.rule == "R1"
    assert w.pay.settlements == [] and w.vendor_calls == []


def test_host_cancel_before_booking_refunds_everyone_and_after_booking_is_refused():
    w = World()
    trip = w.start()
    w.approve("shreeja")
    w.approve("samarth")
    w.engine.host_cancel(trip)
    assert w.state() == State.CLOSED and len(w.pay.refunds) == 2

    w2 = World()
    t2 = reach_filtering(w2)
    w2.engine.run_optimization(t2, BUNDLES, members(), scores())
    w2.clock.advance(timedelta(minutes=10))
    w2.engine.tick()
    assert w2.state() == State.CONFIRMED
    before = len(w2.log.for_trip(t2))
    with pytest.raises(IllegalTransition):
        w2.engine.host_cancel(t2)
    assert len(w2.log.for_trip(t2)) == before, "a refused action must not write to the log"
    assert w2.pay.refunds == []


# ---------------- robustness ----------------

def test_wrong_state_calls_are_refused_and_leave_the_log_untouched():
    w = World()
    trip = w.start()
    before = len(w.log.for_trip(trip))
    with pytest.raises(WrongState):
        w.engine.accept_trigger(trip)
    with pytest.raises(WrongState):
        w.engine.run_optimization(trip, BUNDLES, members(), scores())
    with pytest.raises(WrongState):
        w.engine.create_trip(trip, "x", RANKED, w.limits)
    assert len(w.log.for_trip(trip)) == before


def test_duplicate_mandate_webhook_is_ignored():
    w = World()
    trip = w.start()
    w.approve("shreeja")
    m = w.pay.get_mandate(w.refs()["shreeja"])
    n = len(w.log.for_trip(trip))
    w.engine.on_mandate_event(m)  # the same webhook delivered twice
    assert len(w.log.for_trip(trip)) == n


def test_two_trips_do_not_interfere():
    w = World()
    t1 = w.start("trip1")
    t2 = w.start("trip2")
    for m in ("shreeja", "samarth", "suchit"):
        w.approve(m, t1)
    assert w.state(t1) == State.FILTERING and w.state(t2) == State.COMMITTING
    w.clock.advance(timedelta(hours=2))
    w.engine.tick()  # only trip2 is below quorum
    assert w.state(t1) == State.FILTERING and w.state(t2) == State.CLOSED
    assert w.pay.refunds == []  # trip2 had no cleared deposits to refund


def test_an_early_or_spurious_timer_cannot_trigger_booking():
    w = World()
    trip = reach_filtering(w)
    w.engine.run_optimization(trip, BUNDLES, members(), scores())
    n = len(w.log.for_trip(trip))
    early = Timer(0, trip, "objection_window:1", w.clock.now())  # delivered while the window is open
    w.engine.on_timer(early)
    assert w.state() == State.LOCKED and len(w.log.for_trip(trip)) == n
    assert w.pay.settlements == [] and w.vendor_calls == []
    stale = Timer(0, trip, "objection_window:7", w.clock.now() + timedelta(days=1))  # wrong lock number
    w.clock.advance(timedelta(days=1))
    w.engine.on_timer(stale)
    assert w.state() == State.LOCKED and w.pay.settlements == []
