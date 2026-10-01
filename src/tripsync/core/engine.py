"""Trip engine: wires the blocks together. Contains no decision logic of its own.

State is never stored: it is always rebuilt by replaying the trip's events through the
state machine, so the live state and the audit trail cannot disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from tripsync.clock import Clock
from tripsync.core.events import Event, EventLog
from tripsync.core.optimizer import Bundle, Member, Scores, optimize
from tripsync.core.policy import (
    BookingOutcome,
    Limits,
    PolicyLayer,
    ReleaseInputs,
    quorum_needed,
)
from tripsync.core.scheduler import Scheduler, Timer
from tripsync.core.state import State, apply
from tripsync.ports import Mandate, PaymentsPort, RoutingPort

# event kind -> state-machine trigger (events not listed here only update data)
EVENT_TO_TRIGGER = {
    "trigger_accepted": "trigger_accepted",
    "preferences_received": "preferences_received",
    "quorum_cleared": "quorum_cleared",
    "deadline_below_quorum": "deadline_below_quorum",
    "bundle_locked": "feasible_found",
    "no_feasible": "no_feasible",
    "valid_objection": "valid_objection",
    "objection_window_closed": "objection_window_closed",
    "release_denied": "release_denied",
    "vendor_declined": "vendor_declined",
    "vendor_confirmed": "vendor_confirmed",
    "vendor_cancelled": "vendor_cancelled",
    "rebooked": "rebooked",
    "no_rebooking_option": "no_rebooking_option",
    "escalation_abandoned": "escalation_abandoned",
    "refunds_complete": "refunds_complete",
    "host_cancelled": "host_cancelled",
}


class WrongState(Exception):
    pass


def limits_to_dict(l: Limits) -> dict[str, Any]:
    return {
        "budget_cap_paise": l.budget_cap_paise,
        "window_start": l.window_start.isoformat(),
        "window_end": l.window_end.isoformat(),
        "quorum_share": l.quorum_share,
        "quorum_min_members": l.quorum_min_members,
        "decision_deadline": l.decision_deadline.isoformat(),
        "objection_window_s": l.objection_window.total_seconds(),
        "buffer_margin_minutes": l.buffer_margin_minutes,
        "deposit_paise": l.deposit_paise,
        "cap_per_member_paise": l.cap_per_member_paise,
    }


def limits_from_dict(d: dict[str, Any]) -> Limits:
    return Limits(
        budget_cap_paise=d["budget_cap_paise"],
        window_start=date.fromisoformat(d["window_start"]),
        window_end=date.fromisoformat(d["window_end"]),
        quorum_share=d["quorum_share"],
        quorum_min_members=d["quorum_min_members"],
        decision_deadline=datetime.fromisoformat(d["decision_deadline"]),
        objection_window=timedelta(seconds=d["objection_window_s"]),
        buffer_margin_minutes=d["buffer_margin_minutes"],
        deposit_paise=d["deposit_paise"],
        cap_per_member_paise=d["cap_per_member_paise"],
    )


@dataclass
class TripView:
    trip_id: str
    state: State = State.DRAFT
    limits: Limits | None = None
    host: str = ""
    ranked_members: list[str] = field(default_factory=list)
    mandates: dict[str, str] = field(default_factory=dict)  # member_id -> mandate ref
    mandate_status: dict[str, str] = field(default_factory=dict)
    limits_locked: bool = False
    bundle: dict[str, Any] | None = None
    locks: int = 0
    vendor_ref: str | None = None

    @property
    def cleared(self) -> list[str]:
        return [m for m, s in self.mandate_status.items() if s == "active"]


def replay(trip_id: str, events: list[Event]) -> TripView:
    v = TripView(trip_id)
    for e in events:
        trig = EVENT_TO_TRIGGER.get(e.kind)
        if trig:
            v.state = apply(v.state, trig)  # raises IllegalTransition if the history is corrupt
        p = e.payload
        if e.kind == "trip_created":
            v.limits, v.host, v.ranked_members = limits_from_dict(p["limits"]), p["host"], list(p["ranked_members"])
        elif e.kind == "mandate_created":
            v.mandates[p["member_id"]] = p["ref"]
            v.mandate_status[p["member_id"]] = "pending"
        elif e.kind == "mandate_status":
            v.mandate_status[p["member_id"]] = p["status"]
        elif e.kind == "limits_locked":
            v.limits_locked = True
        elif e.kind == "bundle_locked":
            v.bundle, v.locks = p, v.locks + 1
        elif e.kind == "vendor_confirmed":
            v.vendor_ref = p["vendor_ref"]
    return v


class TripEngine:
    def __init__(
        self,
        *,
        clock: Clock,
        log: EventLog,
        scheduler: Scheduler,
        payments: PaymentsPort,
        routing: RoutingPort,
        policy: PolicyLayer,
        vendor: Callable[[dict[str, Any]], str],
        min_trust: float = 0.5,
    ) -> None:
        self._clock, self._log, self._sched = clock, log, scheduler
        self._payments, self._routing, self._policy = payments, routing, policy
        self._vendor, self._min_trust = vendor, min_trust

    # ----- writing: every state-changing event is validated BEFORE it is written -----
    def _emit(
        self,
        trip_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        rule: str | None = None,
        idem_key: str | None = None,
    ) -> Event:
        trigger = EVENT_TO_TRIGGER.get(kind)
        if trigger:
            apply(self.view(trip_id).state, trigger)  # raises IllegalTransition; nothing is written
        return self._log.append(trip_id, kind, payload, rule=rule, idem_key=idem_key)

    # ----- reading -----
    def view(self, trip_id: str) -> TripView:
        return replay(trip_id, self._log.for_trip(trip_id))

    def _require(self, trip_id: str, *states: State) -> TripView:
        v = self.view(trip_id)
        if v.state not in states:
            raise WrongState(f"{trip_id} is {v.state.value}; expected one of {[s.value for s in states]}")
        return v

    # ----- lifecycle -----
    def create_trip(self, trip_id: str, host: str, ranked_members: list[str], limits: Limits) -> TripView:
        if self._log.for_trip(trip_id):
            raise WrongState(f"trip {trip_id} already exists")
        self._emit(
            trip_id, "trip_created", {"host": host, "ranked_members": ranked_members, "limits": limits_to_dict(limits)}
        )
        return self.view(trip_id)

    def accept_trigger(self, trip_id: str) -> None:
        self._require(trip_id, State.DRAFT)
        self._emit(trip_id, "trigger_accepted")

    def start_commitment(self, trip_id: str) -> None:
        v = self._require(trip_id, State.ELICITING)
        self._emit(trip_id, "preferences_received", {"members": len(v.ranked_members)})
        for member in v.ranked_members:
            m = self._payments.create_mandate(trip_id, member, v.limits.deposit_paise, v.limits.cap_per_member_paise)
            self._emit(trip_id, "mandate_created", {"member_id": member, "ref": m.ref})
        self._sched.schedule(trip_id, "decision_deadline", v.limits.decision_deadline)

    def on_mandate_event(self, mandate: Mandate) -> None:
        """Called when the payments rail reports a mandate status change (the 'webhook')."""
        trip_id = mandate.trip_id
        self._emit(
            trip_id, "mandate_status", {"member_id": mandate.member_id, "ref": mandate.ref, "status": mandate.status},
            idem_key=f"mandate_status:{mandate.ref}:{mandate.status}",
        )
        v = self.view(trip_id)
        if mandate.status == "active" and not v.limits_locked:
            self._emit(trip_id, "limits_locked")
            v = self.view(trip_id)
        if v.state == State.COMMITTING and len(v.cleared) >= quorum_needed(len(v.ranked_members), v.limits):
            self._emit(trip_id, "quorum_cleared", {"cleared": len(v.cleared)})

    def run_optimization(self, trip_id: str, bundles: list[Bundle], members: list[Member], scores: Scores) -> None:
        v = self._require(trip_id, State.FILTERING)
        committed = [m for m in members if m.id in set(v.cleared)]
        res = optimize(
            bundles,
            committed,
            scores,
            group_budget_cap_paise=v.limits.budget_cap_paise,
            routing=self._routing,
            buffer_margin_minutes=v.limits.buffer_margin_minutes,
            min_trust=self._min_trust,
        )
        if res.chosen is None:
            reasons: dict[str, int] = {}
            for r in res.rejections:
                reasons[r.reason] = reasons.get(r.reason, 0) + 1
            self._emit(trip_id, "no_feasible", {"rejections": reasons}, rule="R6")
            return
        n = v.locks + 1
        ends = self._clock.now() + v.limits.objection_window
        self._emit(
            trip_id,
            "bundle_locked",
            {
                "id": res.chosen.id,
                "total_cost_paise": res.chosen.total_cost_paise,
                "trust": res.chosen.trust,
                "filters_passed": True,
                "lock": n,
                "objection_ends": ends.isoformat(),
                "worst_off_score": res.ranking[0].vector[0],
            },
            rule="R5",
        )
        self._sched.schedule(trip_id, f"objection_window:{n}", ends)

    def on_objection(self, trip_id: str, valid: bool) -> None:
        v = self._require(trip_id, State.LOCKED)
        if valid:
            self._sched.cancel(trip_id, f"objection_window:{v.locks}")
            self._emit(trip_id, "valid_objection")
        else:
            self._emit(trip_id, "objection_dismissed")

    def host_cancel(self, trip_id: str) -> None:
        self._emit(trip_id, "host_cancelled")
        for t in self._sched.pending(trip_id):
            self._sched.cancel(trip_id, t.kind)
        if self.view(trip_id).state == State.REFUNDING:
            self._refund(trip_id, "host_cancelled")

    def abandon_escalation(self, trip_id: str) -> None:
        self._require(trip_id, State.ESCALATED)
        self._emit(trip_id, "escalation_abandoned")
        self._refund(trip_id, "escalation_abandoned")

    # ----- timers -----
    def tick(self) -> list[Timer]:
        return self._sched.tick(self.on_timer)

    def on_timer(self, timer: Timer) -> None:
        v = self.view(timer.trip_id)
        if timer.kind == "decision_deadline":
            if v.state == State.COMMITTING:
                self._emit(timer.trip_id, "deadline_below_quorum", {"cleared": len(v.cleared)}, rule="R7")
                self._refund(timer.trip_id, "quorum_missed")
        elif timer.kind.startswith("objection_window:"):
            if v.state == State.LOCKED and timer.kind == f"objection_window:{v.locks}":
                if self._clock.now() < datetime.fromisoformat(v.bundle["objection_ends"]):
                    return  # early or spurious timer: the window is still open, do nothing
                self._attempt_release(v)

    # ----- money paths (all go through the policy layer) -----
    def _attempt_release(self, v: TripView) -> BookingOutcome:
        bundle = v.bundle
        mandates = [(m, ref) for m, ref in v.mandates.items()]
        inputs = ReleaseInputs(
            ranked_members=len(v.ranked_members),
            cleared_deposits=len(v.cleared),  # recomputed by the policy layer; not trusted
            chosen_cost_paise=bundle["total_cost_paise"],
            hard_filters_passed=bool(bundle["filters_passed"]),
            trust_passed=bundle["trust"] >= self._min_trust,
            objection_window_closed=self._clock.now() >= datetime.fromisoformat(bundle["objection_ends"]),
            open_valid_objection=False,  # a valid objection moves the trip out of LOCKED
            authorized_paise=0,  # recomputed by the policy layer; not trusted
        )
        out = self._policy.authorize_and_book(
            v.trip_id,
            v.limits,
            inputs,
            mandates=mandates,
            vendor_name=f"vendor:{bundle['id']}",
            amount_paise=bundle["total_cost_paise"],
            book_with_vendor=lambda: self._vendor(bundle),
            on_authorized=lambda: self._emit(v.trip_id, "objection_window_closed"),
        )
        if out.status == "booked":
            self._emit(v.trip_id, "vendor_confirmed", {"vendor_ref": out.vendor_ref}, rule="R1")
        return out

    def _refund(self, trip_id: str, reason: str) -> None:
        v = self.view(trip_id)
        self._policy.refund_all(trip_id, list(v.mandates.items()), reason)
        self._emit(trip_id, "refunds_complete", {"reason": reason}, idem_key=f"refunds_complete:{trip_id}")
