"""Tests for the number fact-check: a narrative number absent from the record is quarantined."""

from core_backend.validation import number_factcheck, validate


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
                    "approx_travel_distance_m": 15.0, "reachable": True}],
        "scenarios": [
            {"id": "SCN-BASE", "type": "base_case", "title": "t",
             "conditions": {"exits_available": ["E1"], "exits_discounted": [], "occupants_total": 10},
             "narrative": narrative, "routes": []},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "t",
             "conditions": {"exits_available": [], "exits_discounted": ["E1"], "occupants_total": 10},
             "narrative": "Occupants re-route.", "routes": []},
        ],
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
