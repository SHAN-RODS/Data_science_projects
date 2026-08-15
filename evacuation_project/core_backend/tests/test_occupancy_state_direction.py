"""Night is the busy state where people sleep on site: residents are home and asleep at night and out
during the day. A scenario set whose night holds fewer people than its day has the direction inverted,
and must not reach a Pathfinder study unflagged."""

import copy

from core_backend.tests.test_validation_factcheck import _minimal_object
from core_backend.validation import (multiplier_signature, occupancy_state_issues,
                                     occupancy_variance_issues, sleeping_share, state_of, validate)


def _object(night_total, day_total, sleeping_load=30, other_load=10):
    """A block of flats with an amenity: `sleeping_load` in dwellings, `other_load` in commercial."""
    return {
        "building": {"total_occupant_load": sleeping_load + other_load},
        "spaces": [
            {"guid": "A", "use_type": "dwelling", "occupant_load": sleeping_load},
            {"guid": "B", "use_type": "commercial", "occupant_load": other_load},
        ],
        "scenarios": [
            {"id": "SCN-NIGHT", "conditions": {"occupancy_state": "night",
                                               "occupants_total": night_total}},
            {"id": "SCN-DAY", "conditions": {"occupancy_state": "daytime peak",
                                             "occupants_total": day_total}},
        ],
    }


def test_night_below_day_is_flagged():
    issues = occupancy_state_issues(_object(night_total=10, day_total=40))
    assert len(issues) == 1
    assert issues[0]["scenario"] == "SCN-NIGHT"
    assert issues[0]["field"] == "occupants_total"
    assert "inverted" in issues[0]["issue"]


def test_night_above_day_passes():
    assert occupancy_state_issues(_object(night_total=30, day_total=12)) == []


def test_equal_totals_pass():
    """Not every building thins by day; only the inversion is wrong."""
    assert occupancy_state_issues(_object(night_total=40, day_total=40)) == []


def test_a_building_nobody_sleeps_in_is_left_alone():
    """An office is genuinely busier by day — the check must not fire on it."""
    obj = _object(night_total=5, day_total=40, sleeping_load=0, other_load=40)
    assert sleeping_share(obj) == 0.0
    assert occupancy_state_issues(obj) == []


def test_check_needs_both_states_to_compare():
    obj = _object(night_total=10, day_total=40)
    obj["scenarios"] = [obj["scenarios"][0]]      # night only, nothing to compare against
    assert occupancy_state_issues(obj) == []


def test_states_are_read_from_free_text():
    assert state_of({"conditions": {"occupancy_state": "night, occupants asleep"}}) == "night"
    assert state_of({"conditions": {"occupancy_state": "Daytime peak occupancy"}}) == "day"
    assert state_of({"conditions": {"occupancy_state": "maintenance closure"}}) is None


def test_the_worst_scenario_of_each_state_is_the_one_compared():
    """Several night scenarios: the fullest one carries the comparison, not whichever came first."""
    obj = _object(night_total=8, day_total=20)
    obj["scenarios"].append({"id": "SCN-NIGHT-2",
                             "conditions": {"occupancy_state": "night", "occupants_total": 35}})
    assert occupancy_state_issues(obj) == []


def _varied_set():
    """Four scenarios that genuinely differ: different totals, different multipliers."""
    def scn(sid, state, total, dwelling, commercial):
        return {"id": sid, "conditions": {"occupancy_state": state, "occupants_total": total},
                "simulation": {"occupancy_multipliers": [
                    {"use_type": "dwelling", "multiplier": dwelling, "reason": "r"},
                    {"use_type": "commercial", "multiplier": commercial, "reason": "r"}]}}

    return {"building": {"total_occupant_load": 40},
            "spaces": [{"guid": "A", "use_type": "dwelling", "occupant_load": 30},
                       {"guid": "B", "use_type": "commercial", "occupant_load": 10}],
            "scenarios": [scn("SCN-001", "night", 27, 0.9, 0.0),
                          scn("SCN-002", "daytime", 13, 0.3, 1.0),
                          scn("SCN-003", "evening", 21, 0.7, 0.0),
                          scn("SCN-004", "night, one exit lost", 24, 0.8, 0.0)]}


def test_a_genuinely_varied_set_passes():
    assert occupancy_variance_issues(_varied_set()) == []


def test_a_scenario_at_the_full_computed_load_is_flagged():
    obj = _varied_set()
    obj["scenarios"][0]["conditions"]["occupants_total"] = 40      # the whole computed ceiling
    issues = occupancy_variance_issues(obj)
    assert any("capacity ceiling" in i["issue"] for i in issues)


def test_repeated_totals_are_flagged():
    obj = _varied_set()
    obj["scenarios"][2]["conditions"]["occupants_total"] = 27      # same as SCN-001
    issues = occupancy_variance_issues(obj)
    flagged = next(i for i in issues if i["field"] == "occupants_total")
    assert "SCN-001" in flagged["scenario"] and "SCN-003" in flagged["scenario"]
    assert "27" in flagged["issue"]


def test_repeated_distributions_are_flagged_even_when_totals_differ():
    """The multipliers are what actually place people; equal ones mean an identical distribution."""
    obj = _varied_set()
    obj["scenarios"][2]["simulation"]["occupancy_multipliers"] = [
        {"use_type": "dwelling", "multiplier": 0.9, "reason": "r"},
        {"use_type": "commercial", "multiplier": 0.0, "reason": "r"}]      # same as SCN-001
    issues = occupancy_variance_issues(obj)
    flagged = next(i for i in issues if i["field"] == "simulation.occupancy_multipliers")
    assert "identical" in flagged["issue"]
    assert not any(i["field"] == "occupants_total" for i in issues)         # totals still differ


def test_missing_multipliers_across_the_set_are_flagged():
    """Empty multipliers everywhere is the full-load default — the case we no longer want."""
    obj = _varied_set()
    for scn in obj["scenarios"]:
        scn["simulation"]["occupancy_multipliers"] = []
    issues = occupancy_variance_issues(obj)
    assert any("no occupancy_multipliers at all" in i["issue"] for i in issues)


def test_multiplier_order_does_not_count_as_variance():
    obj = _varied_set()
    obj["scenarios"][2]["simulation"]["occupancy_multipliers"] = [
        {"use_type": "commercial", "multiplier": 0.0, "reason": "r"},
        {"use_type": "dwelling", "multiplier": 0.9, "reason": "r"}]      # SCN-001 reordered
    assert multiplier_signature(obj["scenarios"][2]) == multiplier_signature(obj["scenarios"][0])
    assert any(i["field"] == "simulation.occupancy_multipliers"
               for i in occupancy_variance_issues(obj))


def test_a_single_scenario_has_nothing_to_vary_against():
    obj = _varied_set()
    obj["scenarios"] = obj["scenarios"][:1]
    assert occupancy_variance_issues(obj) == []


def test_validate_reports_the_invariant():
    obj = _minimal_object("Evacuate 10 occupants.")
    obj["scenarios"][1]["conditions"]["occupancy_state"] = "day"
    obj["scenarios"][1]["conditions"]["occupants_total"] = 10
    obj["scenarios"][0]["conditions"]["occupants_total"] = 4     # night quieter than day: inverted

    out = validate(copy.deepcopy(obj))
    assert out["validation"]["invariants_checked"]["night_occupancy_not_below_day"] is False
    assert any(i["scenario"] == "SCN-BASE" and "inverted" in i["issue"]
               for i in out["validation"]["occupancy_state_issues"])


def test_validate_reports_the_variance_invariant():
    obj = _minimal_object("Evacuate 10 occupants.")     # both scenarios: 10 occupants, no multipliers
    out = validate(copy.deepcopy(obj))
    invariants = out["validation"]["invariants_checked"]
    assert invariants["occupancy_varies_across_scenarios"] is False
    fields = {i["field"] for i in out["validation"]["occupancy_state_issues"]}
    assert fields == {"occupants_total", "simulation.occupancy_multipliers"}
