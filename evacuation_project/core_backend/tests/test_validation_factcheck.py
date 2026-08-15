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
             "scenario_objective": {"purpose": "p", "conditions": {"exits_discounted": []}},
             "evacuation_routes": {"exits_available": ["E1"], "routes": [],
                                   "restricted_areas": []},
             "occupancy": {"occupants_total": 10, "occupancy_state": "night"},
             "narrative": narrative},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "t",
             "scenario_objective": {"purpose": "p", "conditions": {"exits_discounted": ["E1"]}},
             "evacuation_routes": {"exits_available": [], "routes": [],
                                   "restricted_areas": []},
             "occupancy": {"occupants_total": 10, "occupancy_state": "night"},
             "narrative": "Occupants re-route."},
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
    assert inv["simulation_parametersin_range"] is None
    assert inv["every_occupant_placed_with_a_goal"] is None


# ---- the AI-chosen simulation parameters -----------------------------------------------------------

_GOOD_SIM = {
    "movement_model": "steering",
    "simulation_settings": {
        "start_conditions": "residents asleep in their flats, the single exit open",
        "duration": {"seconds": 900.0, "basis": "long enough to clear the building"},
    },
    "pre_movement": {
        "detection": "smoke detection in the common parts",
        "alarm": "single-stage alarm, sounders throughout",
        "recognition": "sleeping residents take longer to read the alarm as real",
        "response_delay": {"distribution": "lognormal", "mean_s": 120.0, "sd_s": 60.0,
                           "basis": "sleeping residential occupancy"},
    },
    "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                  "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
                  "basis": "able adults on the level"}],
    "occupancy_multipliers": [],
    "evacuation_time": {"estimated_total_s": 300.0,
                        "basis": "120 s pre-movement plus travel over the 15.0 m route"},
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
    assert obj["validation"]["invariants_checked"]["simulation_parametersin_range"] is True
    assert obj["validation"]["simulation_parameter_issues"] == []
    # the AI's own numbers are admitted to the allowed set, so quoting them back is not "invented"
    quoted = number_factcheck(_with_simulation())
    assert "120" not in {u["value"] for u in quoted}


def test_out_of_range_walking_speed_is_flagged():
    assert "profiles[adult].speed_ms_mean" in _fields(
        _with_simulation(lambda sim: sim["profiles"][0].update(speed_ms_mean=6.0)))


def test_absurd_pre_movement_time_is_flagged():
    assert "pre_movement.response_delay.mean_s" in _fields(
        _with_simulation(lambda sim: sim["pre_movement"]["response_delay"].update(mean_s=7200.0)))


def test_a_pre_movement_clock_with_no_starting_point_is_flagged():
    """Response delay runs from the moment occupants recognise the alarm. Drop detection, alarm or
    recognition and the study cannot say when that moment is."""
    for part in ("detection", "alarm", "recognition"):
        assert f"pre_movement.{part}" in _fields(
            _with_simulation(lambda sim, part=part: sim["pre_movement"].update({part: "  "})))


def test_fractions_that_do_not_sum_to_one_are_flagged():
    def mutate(sim):
        sim["profiles"].append({"name": "child", "fraction": 0.9, "speed_distribution": "normal",
                                "speed_ms_mean": 1.0, "speed_ms_sd": 0.2, "shoulder_width_m": 0.4,
                                "basis": "children"})
    assert "profiles[].fraction" in _fields(_with_simulation(mutate))


def testmissing_basis_is_flagged():
    assert "pre_movement.response_delay.basis" in _fields(
        _with_simulation(lambda sim: sim["pre_movement"]["response_delay"].update(basis="   ")))


def test_a_run_with_no_starting_state_is_flagged():
    assert "simulation_settings.start_conditions" in _fields(
        _with_simulation(lambda sim: sim["simulation_settings"].update(start_conditions=" ")))


def test_an_absurd_run_duration_is_flagged():
    assert "simulation_settings.duration.seconds" in _fields(
        _with_simulation(lambda sim: sim["simulation_settings"]["duration"].update(seconds=5.0)))


# ---- the one number nothing downstream grounds ------------------------------------------------------

def test_an_estimate_the_run_would_never_reach_is_flagged():
    """evacuation_time is the AI's own arithmetic, not a result. An estimate longer than the run is
    self-defeating: the simulation stops before the building is clear."""
    issues = simulation_parameter_issues(
        _with_simulation(lambda sim: sim["evacuation_time"].update(estimated_total_s=1200.0)))
    assert any("stop before the building is clear" in i["issue"] for i in issues)


def test_an_estimate_faster_than_its_own_pre_movement_is_flagged():
    """120 s mean response delay, so nobody is even moving before then — a 60 s clearance is not
    arithmetic, it is a slip."""
    issues = simulation_parameter_issues(
        _with_simulation(lambda sim: sim["evacuation_time"].update(estimated_total_s=60.0)))
    assert any("cannot clear before its own pre-movement time" in i["issue"] for i in issues)


def test_an_estimate_with_no_working_behind_it_is_flagged():
    assert "evacuation_time.basis" in _fields(
        _with_simulation(lambda sim: sim["evacuation_time"].update(basis="  ")))


def test_a_missing_estimate_is_flagged_rather_than_passed_over():
    assert "evacuation_time.estimated_total_s" in _fields(
        _with_simulation(lambda sim: sim.pop("evacuation_time")))


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
