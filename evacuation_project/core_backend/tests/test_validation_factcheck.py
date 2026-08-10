"""Tests for the number fact-check (a narrative number absent from the record is quarantined) and for
the range check on the AI-chosen simulation parameters, which the fact-check cannot ground."""

import copy

from core_backend.validation import number_factcheck, simulation_parameter_issues, validate


def _minimal_object(narrative):
    return {
        "schema_version": "1.0",
        "provenance": {"generated_by_model": "test", "distance_method": "approx", "llm_grounded": True},
        "building": {"project": "T", "storeys": 2, "total_floor_area_m2": 100.0,
                     "total_occupant_load": 10},
        "exits": [{"id": "E1", "name": "e", "type": "final_exit", "width_m": 1.0}],
        "circulation": [],
        "spaces": [{"guid": "A", "name": "A", "use_type": "living", "storey": "Ground",
                    "area_m2": 50.0, "occupant_load": 10, "nearest_exit": "E1",
                    "travel_distance_m": 15.0, "travel_distance_method": "geodesic_grid",
                    "reachable": True}],
        "scenarios": [
            {"id": "SCN-BASE", "type": "base_case", "title": "t",
             "conditions": {"exits_available": ["E1"], "exits_discounted": [],
                            "occupancy_state": "night", "occupants_total": 10},
             "narrative": narrative, "routes": []},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "t",
             "conditions": {"exits_available": [], "exits_discounted": ["E1"],
                            "occupancy_state": "night", "occupants_total": 10},
             "narrative": "Occupants re-route.", "routes": []},
        ],
        "regulation_check": {"jurisdiction": "england", "passed": True,
                             "violations": [], "manual_review": [], "basis": "test"},
        "validation": {},
        "not_assessed": [],
    }


def test_factcheck_flags_invented_number():
    # 37 is clearly outside the rounding tolerance of every recorded number (10, 15.0, 50, 100, ...)
    obj = _minimal_object("Evacuate 10 occupants over 15.0 m; a fire fills the stair in 37 seconds.")
    values = {u["value"] for u in number_factcheck(obj)}
    assert "37" in values       # invented -> quarantined
    assert "15.0" not in values  # grounded: a space travel distance
    assert "10" not in values    # grounded: occupant total (also a small structural count)


def test_factcheck_passes_when_grounded():
    obj = _minimal_object("Evacuate 10 occupants; the longest travel distance is 15.0 m.")
    obj = validate(obj)
    assert obj["validation"]["number_factcheck"] == "passed"
    assert obj["validation"]["invariants_checked"]["at_least_two_scenarios"] is True
    assert obj["validation"]["schema_valid"] is True


def test_simulation_invariants_are_none_without_a_simulation_block():
    """Objects generated before the simulation block existed must not fail the new checks."""
    obj = validate(_minimal_object("Evacuate 10 occupants."))
    inv = obj["validation"]["invariants_checked"]
    assert inv["simulation_parameters_in_range"] is None
    assert inv["every_occupant_placed_with_a_goal"] is None


# ---- the AI-chosen simulation parameters -----------------------------------------------------------

_GOOD_SIM = {
    "movement_model": "steering",
    "end_time_s": 900.0,
    "pre_movement": {"distribution": "lognormal", "mean_s": 120.0, "sd_s": 60.0,
                     "basis": "sleeping residential occupancy"},
    "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                  "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
                  "basis": "able adults on the level"}],
    "occupancy_multipliers": [],
}


def _with_simulation(mutate=None):
    obj = _minimal_object("Evacuate 10 occupants over 15.0 m.")
    obj["spaces"][0]["centroid"] = [1.0, 1.0, 0.0]
    for scn in obj["scenarios"]:
        scn["simulation"] = copy.deepcopy(_GOOD_SIM)
    if mutate:
        mutate(obj["scenarios"][0]["simulation"])
    return obj


def _fields(obj):
    return {issue.get("field") for issue in simulation_parameter_issues(obj)}


def test_plausible_parameters_pass_and_are_not_called_ungrounded():
    obj = validate(_with_simulation())
    assert obj["validation"]["invariants_checked"]["simulation_parameters_in_range"] is True
    assert obj["validation"]["simulation_parameter_issues"] == []
    # the AI's own numbers are admitted to the allowed set, so quoting them back is not "invented"
    quoted = number_factcheck(_with_simulation())
    assert "120" not in {u["value"] for u in quoted}


def test_out_of_range_walking_speed_is_flagged():
    assert "profiles[adult].speed_ms_mean" in _fields(
        _with_simulation(lambda sim: sim["profiles"][0].update(speed_ms_mean=6.0)))


def test_absurd_pre_movement_time_is_flagged():
    assert "pre_movement.mean_s" in _fields(
        _with_simulation(lambda sim: sim["pre_movement"].update(mean_s=7200.0)))


def test_fractions_that_do_not_sum_to_one_are_flagged():
    def mutate(sim):
        sim["profiles"].append({"name": "child", "fraction": 0.9, "speed_distribution": "normal",
                                "speed_ms_mean": 1.0, "speed_ms_sd": 0.2, "shoulder_width_m": 0.4,
                                "basis": "children"})
    assert "profiles[].fraction" in _fields(_with_simulation(mutate))


def testmissing_basis_is_flagged():
    assert "pre_movement.basis" in _fields(
        _with_simulation(lambda sim: sim["pre_movement"].update(basis="   ")))


def test_unknown_movement_model_is_flagged():
    assert "movement_model" in _fields(
        _with_simulation(lambda sim: sim.update(movement_model="magic")))


def test_placement_invariant_catches_occupants_with_nowhere_to_go():
    obj = _with_simulation()
    obj["spaces"][0]["reachable"] = False       # the only occupiable room loses its egress path
    obj["not_assessed"] = [{"element": "A", "name": "A", "missing": "no path", "action": "flagged"}]
    obj = validate(obj)
    assert obj["validation"]["invariants_checked"]["every_occupant_placed_with_a_goal"] is False
    assert obj["validation"]["placement_issues"]
