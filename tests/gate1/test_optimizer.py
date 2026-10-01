from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import assume, given, settings, strategies as st

from tripsync.core.connection import Transfer
from tripsync.core.optimizer import (
    Bundle,
    Member,
    apply_hard_filters,
    normalize,
    optimize,
    rank_from_utilities,
    utilities,
)
from tripsync.sims.routing import SimulatedRouting

ROUTING = SimulatedRouting()
D = date(2026, 10, 9)


def B(id, cost=100_000, start=D, trust=1.0, conf=1.0, **kw):
    return Bundle(id, "Rishikesh", start, start + timedelta(days=2), cost * 4, cost, trust, conf, **kw)


def M(id, ceiling=500_000, **kw):
    return Member(id, ceiling, date(2026, 10, 1), date(2026, 10, 31), **kw)


def winner(utils, bundles):
    return rank_from_utilities(utils, bundles)[0].bundle_id


# ---------------- golden cases (hand computed) ----------------

def test_golden_maximin_protects_the_member_a_majority_option_would_sacrifice():
    # A and B love X; C hates it. Y is decent for everyone. Z is C's pick.
    utils = {
        "A": {"X": 1.0, "Y": 0.6, "Z": 0.0},
        "B": {"X": 1.0, "Y": 0.6, "Z": 0.0},
        "C": {"X": 0.0, "Y": 0.5, "Z": 1.0},
    }
    bundles = [B("X"), B("Y"), B("Z")]
    avg = {b: sum(utils[m][b] for m in utils) / 3 for b in "XYZ"}
    assert max(avg, key=avg.get) == "X", "an averaging rule would steamroll C"
    r = rank_from_utilities(utils, bundles)
    assert r[0].bundle_id == "Y"
    assert r[0].vector == (0.5, 0.6, 0.6)
    assert [x.bundle_id for x in r] == ["Y", "X", "Z"]


def test_golden_leximin_breaks_a_maximin_tie_toward_the_second_worst_off():
    # Both bundles leave one member at 0; P keeps the other two at 1, Q only one.
    utils = {"m1": {"P": 1.0, "Q": 0.0}, "m2": {"P": 1.0, "Q": 0.0}, "m3": {"P": 0.0, "Q": 1.0}}
    r = rank_from_utilities(utils, [B("P"), B("Q")])
    assert (r[0].bundle_id, r[0].vector) == ("P", (0.0, 1.0, 1.0))
    assert r[1].vector == (0.0, 0.0, 1.0)


def test_generous_and_harsh_raters_count_equally_after_normalization():
    # m1 rates everything 8-10 (generous), m2 rates 1-3 (harsh), same preference order.
    utils = {"m1": {"X": 10.0, "Y": 9.0, "Z": 8.0}, "m2": {"X": 1.0, "Y": 2.0, "Z": 3.0}}
    n = normalize(utils)
    assert n["m1"] == {"X": 1.0, "Y": 0.5, "Z": 0.0}
    assert n["m2"] == {"X": 0.0, "Y": 0.5, "Z": 1.0}
    assert winner(utils, [B("X"), B("Y"), B("Z")]) == "Y"


def test_indifferent_member_scores_one_everywhere_and_cannot_veto():
    utils = {"calm": {"X": 0.7, "Y": 0.7}, "picky": {"X": 0.1, "Y": 0.9}}
    assert normalize(utils)["calm"] == {"X": 1.0, "Y": 1.0}
    assert winner(utils, [B("X"), B("Y")]) == "Y"


def test_ties_fall_through_to_lower_cost_then_earlier_start_then_id():
    same = {"m": {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0}}
    bundles = [
        B("D", cost=90_000, start=D),
        B("C", cost=100_000, start=D),
        B("B", cost=100_000, start=D + timedelta(days=1)),
        B("A", cost=100_000, start=D),
    ]
    assert [r.bundle_id for r in rank_from_utilities(same, bundles)] == ["D", "A", "C", "B"]


def test_single_bundle_is_chosen_even_if_everyone_is_indifferent():
    assert winner({"m": {"X": 0.3}}, [B("X")]) == "X"


# ---------------- utility ----------------

def test_utility_formula_with_trust_and_confirmation_discount():
    b = B("X", trust=0.8, conf=0.5)
    u = utilities([b], [M("m")], {"m": {"X": {"destination": 1.0, "pace": 0.5}}})
    assert u["m"]["X"] == pytest.approx(0.75 * 0.8 * 0.5)


def test_weights_change_the_base_score():
    b = B("X")
    s = {"m": {"X": {"destination": 1.0, "pace": 0.0}}}
    assert utilities([b], [M("m")], s)["m"]["X"] == pytest.approx(0.5)
    assert utilities([b], [M("m")], s, {"destination": 3.0})["m"]["X"] == pytest.approx(0.75)


def test_cheap_but_unlikely_to_confirm_loses_to_pricier_near_certain():
    cheap_risky = B("risky", cost=60_000, conf=0.35)
    pricey_safe = B("safe", cost=90_000, conf=0.95)
    s = {"m": {b: {"destination": 0.8} for b in ("risky", "safe")}}
    u = utilities([cheap_risky, pricey_safe], [M("m")], s)
    assert winner(u, [cheap_risky, pricey_safe]) == "safe"


def test_missing_or_invalid_scores_fail_loudly():
    with pytest.raises(ValueError, match="missing scores"):
        utilities([B("X")], [M("m")], {"m": {}})
    with pytest.raises(ValueError, match="outside 0..1"):
        utilities([B("X")], [M("m")], {"m": {"X": {"f": 1.5}}})
    with pytest.raises(ValueError, match="no factors"):
        utilities([B("X")], [M("m")], {"m": {"X": {}}})


def test_no_members_is_an_error_not_a_silent_pick():
    with pytest.raises(ValueError):
        rank_from_utilities({}, [B("X")])


# ---------------- hard filters ----------------

def flt(bundles, members, **kw):
    args = dict(group_budget_cap_paise=10_000_000, routing=ROUTING, buffer_margin_minutes=30)
    args.update(kw)
    return apply_hard_filters(bundles, members, **args)


def reasons(rej):
    return {(r.member_id, r.reason) for r in rej}


def test_personal_budget_ceiling_excludes_the_bundle():
    keep, rej = flt([B("X", cost=300_000)], [M("poor", ceiling=200_000), M("rich")])
    assert keep == [] and reasons(rej) == {("poor", "budget_ceiling")}


def test_group_budget_cap_excludes_the_bundle():
    keep, rej = flt([B("X", cost=100_000)], [M("m")], group_budget_cap_paise=399_999)
    assert keep == [] and (None, "group_budget_cap") in reasons(rej)


def test_dates_outside_a_members_availability_excludes_the_bundle():
    m = Member("m", 500_000, date(2026, 10, 12), date(2026, 10, 31))
    keep, rej = flt([B("X")], [m])
    assert keep == [] and ("m", "dates") in reasons(rej)


def test_diet_requirements_must_be_covered():
    keep, rej = flt([B("X", diet_options=frozenset({"veg"}))], [M("jain", diet=frozenset({"veg", "jain"}))])
    assert keep == [] and ("jain", "diet") in reasons(rej)
    keep, _ = flt([B("X", diet_options=frozenset({"veg", "jain"}))], [M("jain", diet=frozenset({"veg", "jain"}))])
    assert [b.id for b in keep] == ["X"]


def test_accessibility_requires_ground_floor():
    keep, rej = flt([B("X", ground_floor=False)], [M("senior", needs_ground_floor=True)])
    assert keep == [] and ("senior", "accessibility") in reasons(rej)
    keep, _ = flt([B("X", ground_floor=True)], [M("senior", needs_ground_floor=True)])
    assert len(keep) == 1


def test_low_trust_excludes_the_bundle():
    keep, rej = flt([B("X", trust=0.4)], [M("m")], min_trust=0.5)
    assert keep == [] and (None, "min_trust") in reasons(rej)


def test_unsafe_transfer_excludes_the_bundle_and_safe_one_passes():
    t0 = datetime(2026, 10, 9, 11, 5, tzinfo=timezone.utc)
    tight = Transfer("HW", "RSK", t0, t0 + timedelta(minutes=50))  # needs 45 + 30
    roomy = Transfer("HW", "RSK", t0, t0 + timedelta(minutes=120))
    keep, rej = flt([B("tight", transfers=(tight,)), B("roomy", transfers=(roomy,))], [M("m")])
    assert [b.id for b in keep] == ["roomy"]
    assert any(r.bundle_id == "tight" and r.reason == "unsafe_transfer" for r in rej)


def test_all_reasons_are_reported_not_just_the_first():
    keep, rej = flt(
        [B("X", cost=900_000, trust=0.1)], [M("m", ceiling=100_000)], group_budget_cap_paise=1
    )
    assert keep == []
    assert {r.reason for r in rej} >= {"group_budget_cap", "min_trust", "budget_ceiling"}


# ---------------- optimize end to end ----------------

def test_optimize_returns_none_with_rejections_when_nothing_is_feasible():
    res = optimize(
        [B("X", cost=900_000)], [M("m", ceiling=100_000)], {},
        group_budget_cap_paise=10_000_000, routing=ROUTING, buffer_margin_minutes=30,
    )
    assert res.chosen is None and res.ranking == [] and res.rejections


def test_optimize_never_ranks_a_filtered_bundle():
    bundles = [B("ok"), B("pricey", cost=900_000)]
    scores = {"m": {"ok": {"f": 0.2}, "pricey": {"f": 1.0}}}
    res = optimize(
        bundles, [M("m", ceiling=200_000)], scores,
        group_budget_cap_paise=10_000_000, routing=ROUTING, buffer_margin_minutes=30,
    )
    assert res.chosen.id == "ok" and [r.bundle_id for r in res.ranking] == ["ok"]


def test_optimize_requires_committed_members():
    with pytest.raises(ValueError):
        optimize([B("X")], [], {}, group_budget_cap_paise=1, routing=ROUTING, buffer_margin_minutes=0)


# ---------------- properties ----------------

util_values = st.integers(min_value=0, max_value=1000)


@st.composite
def util_tables(draw, min_members=1, max_members=4, min_bundles=1, max_bundles=6):
    n_m = draw(st.integers(min_members, max_members))
    n_b = draw(st.integers(min_bundles, max_bundles))
    bids = [f"b{i}" for i in range(n_b)]
    return {
        f"m{i}": {b: float(draw(util_values)) for b in bids} for i in range(n_m)
    }


def bundles_for(utils):
    ids = list(next(iter(utils.values())))
    return [B(i, cost=100_000 + k * 1000) for k, i in enumerate(ids)]


@given(util_tables(), st.randoms())
def test_property_result_does_not_depend_on_input_order(utils, rnd):
    bundles = bundles_for(utils)
    base = [r.bundle_id for r in rank_from_utilities(utils, bundles)]
    shuffled_bundles = bundles[:]
    rnd.shuffle(shuffled_bundles)
    members = list(utils)
    rnd.shuffle(members)
    reordered = {m: dict(sorted(utils[m].items(), key=lambda _: rnd.random())) for m in members}
    assert [r.bundle_id for r in rank_from_utilities(reordered, shuffled_bundles)] == base


@given(util_tables(), st.data())
def test_property_each_members_own_scale_and_offset_never_matters(utils, data):
    bundles = bundles_for(utils)
    base = [r.bundle_id for r in rank_from_utilities(utils, bundles)]
    rescaled = {}
    for m, per_b in utils.items():
        a = data.draw(st.integers(1, 7))
        c = data.draw(st.integers(0, 50))
        rescaled[m] = {b: a * u + c for b, u in per_b.items()}
    assert [r.bundle_id for r in rank_from_utilities(rescaled, bundles)] == base


@given(util_tables(min_bundles=2))
def test_property_the_winner_has_the_best_possible_worst_case(utils):
    bundles = bundles_for(utils)
    ranking = rank_from_utilities(utils, bundles)
    assert ranking[0].vector[0] == max(r.vector[0] for r in ranking)


@given(util_tables(min_bundles=2), st.data())
@settings(max_examples=200)
def test_property_adding_a_bundle_the_winner_strictly_dominates_never_changes_the_winner(utils, data):
    bundles = bundles_for(utils)
    win = winner(utils, bundles)
    lows = {m: min(per_b.values()) for m, per_b in utils.items()}
    new_utils = {}
    reduced_any = False
    for m, per_b in utils.items():
        w = per_b[win]
        # stays inside the member's existing range, so min-max normalization is unchanged
        new = data.draw(st.integers(int(lows[m]), int(w)))
        reduced_any = reduced_any or new < w
        new_utils[m] = new
    assume(reduced_any)
    extended = {m: {**per_b, "dominated": float(new_utils[m])} for m, per_b in utils.items()}
    assert winner(extended, bundles + [B("dominated", cost=1_000)]) == win


@given(util_tables())
def test_property_deterministic(utils):
    bundles = bundles_for(utils)
    assert rank_from_utilities(utils, bundles) == rank_from_utilities(utils, bundles)


def test_documented_limit_adding_a_bundle_can_lower_an_existing_bundles_score():
    """The PRD once claimed 'adding an unrelated bundle never lowers the winner's minimum'.

    That is false under per-member min-max normalization: a new bundle that extends a member's
    range rescales every other bundle for that member. Only bundles inside every member's
    existing range (and dominated by the winner) are guaranteed not to change the result; that
    is the property tested above.
    """
    base = {"m1": {"X": 5.0, "Y": 2.0, "Z": 6.0}, "m2": {"X": 10.0, "Y": 0.0, "Z": 1.0}}
    bundles = [B("X", 100_000), B("Y", 101_000), B("Z", 102_000)]
    before = rank_from_utilities(base, bundles)
    assert before[0].bundle_id == "X" and before[0].vector[0] == 0.75

    extended = {"m1": {**base["m1"], "N": 8.0}, "m2": {**base["m2"], "N": 15.0}}
    after = rank_from_utilities(extended, bundles + [B("N", 103_000)])
    x_after = next(r for r in after if r.bundle_id == "X")
    assert x_after.vector[0] == 0.5  # X's worst-case fell from 0.75 to 0.5
    assert after[0].bundle_id == "N" and after[0].vector == (1.0, 1.0)
