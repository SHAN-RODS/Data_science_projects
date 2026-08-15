"""Fire-related conditions are the one part of an egress scenario neither the IFC nor the computed
facts can supply: what burns, where, what it takes out, and when people find out. The scenario says an
exit is discounted; this block is where it says why. The two must agree."""

import copy

from core_backend.exit_names import unknown_exit_references
from core_backend.tests.test_validation_factcheck import _minimal_object
from core_backend.validation import fire_condition_issues, validate


def _fire(affected_exits=()):
    return {"fire_origin": "Office", "fire_origin_storey": "G",
            "affected_exits": list(affected_exits), "affected_routes": [],
            "detection_and_alarm": "automatic detection, alarm on activation",
            "smoke_conditions": "smoke fills the lobby within two minutes",
            "basis": "origin beside the exit under test"}


def _obj(discounted=(), affected=(), with_fire=True):
    obj = {"exits": [{"id": "E1", "name": "Front", "position": [0.0, 0.0, 0.0]},
                     {"id": "E2", "name": "Rear", "position": [20.0, 0.0, 0.0]}],
           "spaces": [],
           "scenarios": [{"id": "SCN-001",
                          "scenario_objective": {
                              "conditions": {"exits_discounted": list(discounted)}},
                          "evacuation_routes": {"exits_available": ["Exit 2"]}}]}
    if with_fire:
        obj["scenarios"][0]["fire_conditions"] = _fire(affected)
    return obj


def test_a_fire_that_explains_the_discounted_exit_passes():
    assert fire_condition_issues(_obj(discounted=["Exit 1"], affected=["Exit 1"])) == []


def test_a_scenario_with_no_fire_at_all_passes_when_nothing_is_discounted():
    assert fire_condition_issues(_obj()) == []


def test_a_missing_fire_block_is_flagged():
    issues = fire_condition_issues(_obj(with_fire=False))
    assert len(issues) == 1
    assert issues[0]["field"] == "fire_conditions"


def test_an_exit_discounted_for_no_stated_reason_is_flagged():
    issues = fire_condition_issues(_obj(discounted=["Exit 1"], affected=[]))
    assert "Exit 1 discounted but not attributed to the fire" in issues[0]["issue"]


def test_an_exit_the_fire_hits_but_the_scenario_still_uses_is_flagged():
    """The dangerous direction: the study would send occupants at an exit the fire has taken."""
    issues = fire_condition_issues(_obj(discounted=[], affected=["Exit 1"]))
    assert "Exit 1 hit by the fire but still counted as available" in issues[0]["issue"]


def test_both_directions_are_reported_together():
    issues = fire_condition_issues(_obj(discounted=["Exit 1"], affected=["Exit 2"]))
    assert len(issues) == 1
    assert "Exit 1 discounted" in issues[0]["issue"]
    assert "Exit 2 hit by the fire" in issues[0]["issue"]


def test_a_fire_naming_an_exit_that_does_not_exist_is_caught():
    """The fire block goes through the same unknown-exit check as the conditions."""
    obj = _obj(discounted=["Exit 9"], affected=["Exit 9"])
    assert unknown_exit_references(obj) == [{"scenario": "SCN-001", "unknown_exits": ["Exit 9"]}]


def test_validate_reports_the_fire_invariant():
    obj = _minimal_object("Evacuate 10 occupants.")           # carries no fire_conditions at all
    out = validate(copy.deepcopy(obj))
    assert out["validation"]["invariants_checked"]["fire_conditions_match_discounted_exits"] is False
    assert len(out["validation"]["fire_condition_issues"]) == 2      # one per scenario
