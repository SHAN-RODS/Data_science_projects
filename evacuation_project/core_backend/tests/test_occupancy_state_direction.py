"""What validate() still says about occupancy, now that the multipliers come from a table.

Generation can no longer produce an inverted set — occupancy_states holds every residential use type
at 1.0 in the night state, which is the schema maximum, so no daytime state can exceed it (proved in
test_occupancy_states.py). These checks stay because validate() also runs over objects this pipeline
did not just produce: an archived run from an earlier version, or a JSON somebody edited by hand.
They cost nothing and they are the only place that would catch either.

The variance check changed shape with the states. It used to flag a scenario at the full computed
load, and repeated totals, and repeated multiplier sets. The first two went: a night state on a
wholly residential building SHOULD sit at the computed load — that is the design case — and two
different states can coincide on a headcount while standing the people in different rooms, which is
a different evacuation. What is left is genuine duplication: the same occupancy state closing the
same exits.
"""

import copy

from core_backend.occupancy_states import multipliers_for
from core_backend.tests.test_validation_factcheck import minimal_object
from core_backend.validation import (occupancy_state_issues, occupancy_variance_issues,
                                     scenario_signature, sleeping_occupancy, sleeping_share,
                                     state_of, unknown_state_issues, validate)


def scenario(sid, state, dwelling, commercial, discounted=()):
    return {"id": sid,
            "occupancy": {"occupancy_state": state},
            "scenario_objective": {"conditions": {"exits_discounted": list(discounted)}},
            "simulation": {"occupancy_multipliers": [
                {"use_type": "dwelling", "multiplier": dwelling, "reason": "r"},
                {"use_type": "commercial", "multiplier": commercial, "reason": "r"}]}}


def sample_object(night_dwelling, day_dwelling, sleeping_load=30, other_load=10):
    """A block of flats with an amenity: `sleeping_load` in dwellings, `other_load` in commercial.

    The multipliers are set directly here rather than taken from a state, because the point of these
    tests is what happens to an object whose multipliers did NOT come from the table."""
    return {
        "building": {"total_occupant_load": sleeping_load + other_load},
        "spaces": [
            {"guid": "A", "use_type": "dwelling", "occupant_load": sleeping_load},
            {"guid": "B", "use_type": "commercial", "occupant_load": other_load},
        ],
        "scenarios": [scenario("SCN-NIGHT", "night", night_dwelling, 0.0),
                      scenario("SCN-DAY", "daytime peak", day_dwelling, 1.0)],
    }


# ---- the direction check, on objects the table did not build ---------------------------------

def test_night_below_day_is_flagged():
    issues = occupancy_state_issues(sample_object(night_dwelling=0.3, day_dwelling=1.0))
    assert len(issues) == 1
    assert issues[0]["scenario"] == "SCN-NIGHT"
    assert issues[0]["field"] == "simulation.occupancy_multipliers"
    assert "inverted" in issues[0]["issue"]


def test_night_above_day_passes():
    assert occupancy_state_issues(sample_object(night_dwelling=1.0, day_dwelling=0.4)) == []


def test_equal_residential_occupancy_passes():
    """Not every building thins its dwellings by day; only the inversion is wrong."""
    assert occupancy_state_issues(sample_object(night_dwelling=1.0, day_dwelling=1.0)) == []


def test_a_building_whose_amenity_dominates_is_not_an_inversion():
    """The regression this invariant was rewritten for. The amenity carries most of the load and
    empties at night, so the night TOTAL (30) sits far below the day's (78) — but the dwellings are
    full at night and thinned by day, which is exactly right. Comparing totals flagged this; the
    residential comparison must not."""
    obj = sample_object(night_dwelling=1.0, day_dwelling=0.4, sleeping_load=30, other_load=60)

    assert sleeping_occupancy(obj["spaces"], {"dwelling": 1.0, "commercial": 0.0}) == 30
    assert sleeping_occupancy(obj["spaces"], {"dwelling": 0.4, "commercial": 1.0}) == 12
    assert occupancy_state_issues(obj) == []       # residential: 30 >= 12, correct


def test_a_building_nobody_sleeps_in_is_left_alone():
    """An office is genuinely busier by day — the check must not fire on it."""
    obj = sample_object(night_dwelling=0.1, day_dwelling=1.0, sleeping_load=0, other_load=40)
    assert sleeping_share(obj) == 0.0
    assert occupancy_state_issues(obj) == []


def test_an_unmentioned_use_type_stays_at_full_load():
    """The default is 1.0, not 0 — a night state that names only 'commercial' has not emptied the
    dwellings, and must not be read as having done so."""
    assert sleeping_occupancy([{"use_type": "dwelling", "occupant_load": 30}],
                              {"commercial": 0.0}) == 30


def test_check_needs_both_states_to_compare():
    obj = sample_object(night_dwelling=0.3, day_dwelling=1.0)
    obj["scenarios"] = [obj["scenarios"][0]]      # night only, nothing to compare against
    assert occupancy_state_issues(obj) == []


def test_a_state_key_is_read_from_the_table():
    """A generated object names a state from occupancy_states, so the period is looked up rather
    than guessed at from the words in it."""
    assert state_of({"occupancy": {"occupancy_state": "night_sleeping"}}) == "night"
    assert state_of({"occupancy": {"occupancy_state": "working_day"}}) == "day"
    assert state_of({"occupancy": {"occupancy_state": "weekend_daytime"}}) == "day"


def test_a_transitional_state_is_outside_the_comparison():
    """early_morning and evening_communal sit between night and day by construction; comparing them
    against either says nothing."""
    assert state_of({"occupancy": {"occupancy_state": "evening_communal"}}) is None
    assert state_of({"occupancy": {"occupancy_state": "early_morning"}}) is None


def test_free_text_states_still_read_for_older_objects():
    """Objects written before the states existed held whatever phrase the model chose."""
    assert state_of({"occupancy": {"occupancy_state": "night, occupants asleep"}}) == "night"
    assert state_of({"occupancy": {"occupancy_state": "Daytime peak occupancy"}}) == "day"
    assert state_of({"occupancy": {"occupancy_state": "maintenance closure"}}) is None


def test_the_fullest_scenario_of_each_state_is_the_one_compared():
    """Several night scenarios: the fullest one carries the comparison, not whichever came first."""
    obj = sample_object(night_dwelling=0.2, day_dwelling=0.6)
    obj["scenarios"].append(scenario("SCN-NIGHT-2", "night", 1.0, 0.0))
    assert occupancy_state_issues(obj) == []


def test_a_table_built_set_cannot_be_inverted():
    """The end of the fault. Both scenarios take their multipliers from the states, and there is no
    pair of states that can produce the inversion."""
    def from_state(sid, state):
        return {"id": sid, "occupancy": {"occupancy_state": state},
                "scenario_objective": {"conditions": {"exits_discounted": []}},
                "simulation": {"occupancy_multipliers": multipliers_for(state)}}

    for day_state in ("working_day", "weekend_daytime"):
        obj = {"building": {"total_occupant_load": 40},
               "spaces": [{"guid": "A", "use_type": "dwelling", "occupant_load": 30},
                          {"guid": "B", "use_type": "commercial", "occupant_load": 10}],
               "scenarios": [from_state("SCN-001", "night_sleeping"),
                             from_state("SCN-002", day_state)]}
        assert occupancy_state_issues(obj) == []


# ---- variance: identity is the state and the exits it closes ---------------------------------

def varied_set():
    """Four scenarios that genuinely differ — three states, and one repeat of a state with a
    different exit discounted."""
    return {"building": {"total_occupant_load": 40},
            "spaces": [{"guid": "A", "use_type": "dwelling", "occupant_load": 30},
                       {"guid": "B", "use_type": "commercial", "occupant_load": 10}],
            "scenarios": [scenario("SCN-001", "night_sleeping", 1.0, 0.0),
                          scenario("SCN-002", "working_day", 0.3, 1.0),
                          scenario("SCN-003", "evening_communal", 0.9, 0.3),
                          scenario("SCN-004", "night_sleeping", 1.0, 0.0,
                                   discounted=("Exit 1",))]}


def test_a_genuinely_varied_set_passes():
    assert occupancy_variance_issues(varied_set()) == []


def test_the_same_state_closing_the_same_exits_is_flagged():
    obj = varied_set()
    obj["scenarios"][3]["scenario_objective"]["conditions"]["exits_discounted"] = []
    issues = occupancy_variance_issues(obj)

    flagged = next(i for i in issues if i["field"] == "occupancy_state")
    assert "SCN-001" in flagged["scenario"] and "SCN-004" in flagged["scenario"]
    assert "night_sleeping" in flagged["issue"]
    assert "every exit available" in flagged["issue"]


def test_the_same_state_with_a_different_exit_lost_is_not_a_duplicate():
    """The pair a discounted-exit study is built on: hold the population still, take an exit away.
    Keying identity on the multipliers alone used to call this a duplicate."""
    obj = varied_set()

    assert scenario_signature(obj["scenarios"][0]) != scenario_signature(obj["scenarios"][3])
    assert occupancy_variance_issues(obj) == []


def test_a_night_state_at_the_full_computed_load_is_no_longer_flagged():
    """On a wholly residential building the night state IS the computed load — every dwelling at
    its code-derived capacity, with everyone asleep. That is the design case, not a fault."""
    obj = {"building": {"total_occupant_load": 30},
           "spaces": [{"guid": "A", "use_type": "dwelling", "occupant_load": 30}],
           "scenarios": [scenario("SCN-001", "night_sleeping", 1.0, 0.0),
                         scenario("SCN-002", "working_day", 0.3, 1.0)]}
    obj["scenarios"][0]["occupancy"]["occupants_total"] = 30

    assert occupancy_variance_issues(obj) == []


def test_two_states_landing_on_the_same_total_are_not_duplicates():
    """Coinciding totals are an arithmetic accident, not duplication — the states stand the same
    number of people in different rooms."""
    obj = varied_set()
    for scn in obj["scenarios"]:
        scn["occupancy"]["occupants_total"] = 20

    assert occupancy_variance_issues(obj) == []


def test_exit_order_does_not_count_as_variance():
    obj = varied_set()
    obj["scenarios"][0]["scenario_objective"]["conditions"]["exits_discounted"] = ["Exit 2",
                                                                                   "Exit 1"]
    obj["scenarios"][3]["scenario_objective"]["conditions"]["exits_discounted"] = ["Exit 1",
                                                                                   "Exit 2"]

    assert scenario_signature(obj["scenarios"][0]) == scenario_signature(obj["scenarios"][3])
    assert any(i["field"] == "occupancy_state" for i in occupancy_variance_issues(obj))


def test_a_single_scenario_has_nothing_to_vary_against():
    obj = varied_set()
    obj["scenarios"] = obj["scenarios"][:1]
    assert occupancy_variance_issues(obj) == []


def test_a_state_outside_the_table_is_reported():
    """Generation cannot produce one — the field is an enum. An edited or pre-states object can,
    and its multipliers then mean whatever was written rather than what a state defines."""
    obj = varied_set()
    obj["scenarios"][1]["occupancy"]["occupancy_state"] = "some state I made up"

    issues = unknown_state_issues(obj)

    assert len(issues) == 1
    assert issues[0]["scenario"] == "SCN-002"
    assert issues[0]["field"] == "occupancy.occupancy_state"
    assert issues[0] in occupancy_variance_issues(obj)


# ---- and what validate() reports on the whole object -----------------------------------------

def test_validate_reports_the_direction_invariant():
    obj = minimal_object("Evacuate 10 occupants.")     # one 'living' space, computed load 10
    obj["scenarios"][1]["occupancy"]["occupancy_state"] = "day"
    # night thins the living space to a third while day leaves it full: inverted
    obj["scenarios"][0]["simulation"] = {"occupancy_multipliers": [
        {"use_type": "living", "multiplier": 0.3, "reason": "r"}]}
    obj["scenarios"][1]["simulation"] = {"occupancy_multipliers": [
        {"use_type": "living", "multiplier": 1.0, "reason": "r"}]}

    out = validate(copy.deepcopy(obj))
    assert out["validation"]["invariants_checked"]["night_occupancy_not_below_day"] is False
    assert any(i["scenario"] == "SCN-BASE" and "inverted" in i["issue"]
               for i in out["validation"]["occupancy_state_issues"])


def test_validate_reports_the_variance_invariant():
    obj = minimal_object("Evacuate 10 occupants.")
    for scn in obj["scenarios"]:                       # same state, same exits: one run twice
        scn["occupancy"]["occupancy_state"] = "night_sleeping"
        scn["scenario_objective"]["conditions"]["exits_discounted"] = []

    out = validate(copy.deepcopy(obj))

    invariants = out["validation"]["invariants_checked"]
    assert invariants["occupancy_varies_across_scenarios"] is False
    assert any(i["field"] == "occupancy_state"
               for i in out["validation"]["occupancy_state_issues"])


def test_validate_passes_a_set_whose_states_differ():
    obj = minimal_object("Evacuate 10 occupants.")
    obj["scenarios"][0]["occupancy"]["occupancy_state"] = "night_sleeping"
    obj["scenarios"][1]["occupancy"]["occupancy_state"] = "working_day"

    out = validate(copy.deepcopy(obj))

    assert out["validation"]["invariants_checked"]["occupancy_varies_across_scenarios"] is True
