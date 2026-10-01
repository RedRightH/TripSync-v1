"""Optimizer: hard filters, per-member utility, min-max normalization, maximin + leximin.

A pure function of its inputs: no clock, no I/O except the RoutingPort used by the
connection-buffer filter. That is why it can be tested without any partner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from tripsync.core.connection import Transfer, check_transfer
from tripsync.ports import RoutingPort

ROUND = 9


@dataclass(frozen=True)
class Bundle:
    id: str
    destination: str
    start: date
    end: date
    total_cost_paise: int
    per_person_cost_paise: int
    trust: float  # ground-trust score 0..1
    confirm_prob: float  # confirmation probability 0..1
    ground_floor: bool = False
    diet_options: frozenset[str] = frozenset()
    transfers: tuple[Transfer, ...] = ()


@dataclass(frozen=True)
class Member:
    id: str
    budget_ceiling_paise: int
    available_from: date
    available_to: date
    diet: frozenset[str] = frozenset()
    needs_ground_floor: bool = False


@dataclass(frozen=True)
class Rejection:
    bundle_id: str
    member_id: str | None
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class Ranked:
    bundle_id: str
    normalized: dict[str, float]
    vector: tuple[float, ...]  # member scores sorted ascending: vector[0] is the worst-off member


@dataclass(frozen=True)
class OptimizationResult:
    chosen: Bundle | None
    ranking: list[Ranked] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


# ---------------- hard filters ----------------

def apply_hard_filters(
    bundles: list[Bundle],
    members: list[Member],
    *,
    group_budget_cap_paise: int,
    routing: RoutingPort,
    buffer_margin_minutes: int,
    min_trust: float = 0.5,
) -> tuple[list[Bundle], list[Rejection]]:
    survivors: list[Bundle] = []
    rejections: list[Rejection] = []
    for b in bundles:
        bad: list[Rejection] = []
        if b.total_cost_paise > group_budget_cap_paise:
            bad.append(Rejection(b.id, None, "group_budget_cap", f"{b.total_cost_paise} > {group_budget_cap_paise}"))
        if b.trust < min_trust:
            bad.append(Rejection(b.id, None, "min_trust", f"{b.trust} < {min_trust}"))
        for t in b.transfers:
            c = check_transfer(t, routing, buffer_margin_minutes)
            if not c.ok:
                bad.append(Rejection(b.id, None, "unsafe_transfer", c.reason))
        for m in members:
            if b.per_person_cost_paise > m.budget_ceiling_paise:
                bad.append(Rejection(b.id, m.id, "budget_ceiling"))
            if b.start < m.available_from or b.end > m.available_to:
                bad.append(Rejection(b.id, m.id, "dates"))
            if not m.diet <= b.diet_options:
                bad.append(Rejection(b.id, m.id, "diet", f"missing {sorted(m.diet - b.diet_options)}"))
            if m.needs_ground_floor and not b.ground_floor:
                bad.append(Rejection(b.id, m.id, "accessibility"))
        (rejections.extend(bad) if bad else survivors.append(b))
    return survivors, rejections


# ---------------- utility ----------------

Scores = dict[str, dict[str, dict[str, float]]]  # member_id -> bundle_id -> factor -> 0..1


def utilities(
    bundles: list[Bundle],
    members: list[Member],
    scores: Scores,
    weights: dict[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """U_i(b) = (sum_k w_k * s_ik(b) / sum_k w_k) * trust(b) * confirm_prob(b)."""
    out: dict[str, dict[str, float]] = {}
    for m in members:
        out[m.id] = {}
        for b in bundles:
            try:
                factors = scores[m.id][b.id]
            except KeyError:
                raise ValueError(f"missing scores for member {m.id} and bundle {b.id}") from None
            if not factors:
                raise ValueError(f"no factors for member {m.id} and bundle {b.id}")
            for k, v in factors.items():
                if not 0.0 <= v <= 1.0:
                    raise ValueError(f"score {k}={v} outside 0..1")
            w = {k: (weights or {}).get(k, 1.0) for k in factors}
            total_w = sum(w.values())
            if total_w <= 0:
                raise ValueError("weights must sum to a positive number")
            base = sum(w[k] * factors[k] for k in factors) / total_w
            out[m.id][b.id] = base * b.trust * b.confirm_prob
    return out


def normalize(utils: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Scale each member to their own min-max range. An indifferent member scores 1.0 everywhere."""
    norm: dict[str, dict[str, float]] = {}
    for m, per_bundle in utils.items():
        lo, hi = min(per_bundle.values()), max(per_bundle.values())
        if hi == lo:
            norm[m] = {b: 1.0 for b in per_bundle}
        else:
            norm[m] = {b: round((u - lo) / (hi - lo), ROUND) for b, u in per_bundle.items()}
    return norm


# ---------------- selection ----------------

def rank_from_utilities(utils: dict[str, dict[str, float]], bundles: list[Bundle]) -> list[Ranked]:
    """Maximin over normalized scores; ties broken by leximin, then lower cost, earlier start, id.

    Comparing ascending-sorted score vectors lexicographically *is* maximin with leximin
    tie-break: the first element is the worst-off member, the second the next worst, and so on.
    """
    if not utils:
        raise ValueError("no committed members to optimize for")
    norm = normalize(utils)
    by_id = {b.id: b for b in bundles}
    ranked: list[Ranked] = []
    for bid in by_id:
        per_member = {m: norm[m][bid] for m in norm}
        ranked.append(Ranked(bid, per_member, tuple(sorted(per_member.values()))))
    ranked.sort(
        key=lambda r: (
            tuple(-x for x in r.vector),
            by_id[r.bundle_id].total_cost_paise,
            by_id[r.bundle_id].start,
            r.bundle_id,
        )
    )
    return ranked


def optimize(
    bundles: list[Bundle],
    members: list[Member],
    scores: Scores,
    *,
    group_budget_cap_paise: int,
    routing: RoutingPort,
    buffer_margin_minutes: int,
    min_trust: float = 0.5,
    weights: dict[str, float] | None = None,
) -> OptimizationResult:
    if not members:
        raise ValueError("optimize needs at least one committed member")
    survivors, rejections = apply_hard_filters(
        bundles,
        members,
        group_budget_cap_paise=group_budget_cap_paise,
        routing=routing,
        buffer_margin_minutes=buffer_margin_minutes,
        min_trust=min_trust,
    )
    if not survivors:
        return OptimizationResult(None, [], rejections)
    ranking = rank_from_utilities(utilities(survivors, members, scores, weights), survivors)
    chosen = next(b for b in survivors if b.id == ranking[0].bundle_id)
    return OptimizationResult(chosen, ranking, rejections)
