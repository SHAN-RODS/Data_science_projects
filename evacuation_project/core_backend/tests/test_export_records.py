"""The exported deliverable is a flat array of six-field records, one per scenario (no API / no IFC).

Each record carries the egress-simulation set-up: the IFC elements with this scenario's open/closed
states, the scenario's occupancy, and the three input blocks a study needs — pre-movement time,
movement characteristic and fire-related conditions — all inside those same six fields, and all as
JSON. The longer working (per-room placement, benchmark distances, narrative, unassessed rooms) stays
in the full building object and is deliberately kept out of the records.
"""

import json
import os

from core_backend.export_results import build_records, export_records

SIX_KEYS = {"unique_id", "description", "relevant_ifc_element",
            "regulatory_justification", "ai_explanation", "scenario"}


def _simulation():
    return {
        "movement_model": "steering",
        "simulation_settings": {
            "start_conditions": "occupants at rest in their own rooms, both exits open",
            "duration": {"seconds": 900.0, "basis": "long enough to clear the building"},
        },
        "pre_movement": {
            "detection": "automatic detection throughout, fire found within a minute",
            "alarm": "single-stage alarm, sounders throughout on detection",
            "recognition": "alert occupants recognise the alarm quickly",
            "response_delay": {"distribution": "normal", "mean_s": 60.0, "sd_s": 30.0,
                               "basis": "alert occupants, staff-assisted"},
        },
        "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                      "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
                      "basis": "able adults on the level"}],
        "occupancy_multipliers": [],
        "evacuation_time": {"estimated_total_s": 300.0,
                            "basis": "60 s pre-movement plus travel over the 15.0 m route"},
    }


def _fire(origin="Office", affected_exits=()):
    return {
        "fire_origin": origin,
        "fire_origin_storey": "G",
        "affected_exits": list(affected_exits),
        "affected_routes": [],
        "detection_and_alarm": "automatic detection throughout, alarm at detection",
        "smoke_conditions": "smoke held to the room of origin for the first two minutes",
        "basis": "origin beside the exit under test",
    }


def _obj():
    return {
        "model": {"source_ifc": "t.ifc", "units": "m",
                  "coordinate_system": "ifc_world_coordinates"},
        "provenance": {"distance_method": "geodesic raster"},
        "building": {"total_occupant_load": 20},
        "exits": [{"id": "E1", "name": "Front door", "type": "final_exit", "width_m": 1.0,
                   "position": [0.0, 0.0, 0.0]},
                  {"id": "E2", "name": "Rear door", "type": "final_exit", "width_m": 1.0,
                   "position": [20.0, 0.0, 0.0]}],
        "doors": [{"id": "D1", "name": "Office door", "type": "internal_door", "width_m": 0.8,
                   "position": [4.0, 1.5, 0.0], "connects": ["R1", "R2"]}],
        "circulation": [{"id": "ST1", "name": "Main stair", "type": "internal_stair", "width_m": 1.2,
                         "rise_m": 3.0, "going_m": 4.5, "connects_storeys": ["G", "First"]}],
        "elevators": [{"id": "LIFT1", "name": "Lift", "type": "elevator", "is_evac_lift": False,
                       "position": [12.0, 1.0, 0.0]}],
        "spaces": [
            {"guid": "R1", "name": "Office", "use_type": "office", "storey": "G", "area_m2": 100.0,
             "centroid": [2.0, 1.5, 0.0], "occupant_load": 20, "nearest_exit": "E1",
             "travel_distance_m": 15.0, "most_remote_point": [0.5, 0.5], "reachable": True},
        ],
        "not_assessed": [{"element": "R9", "missing": "no egress path"}],
        "scenarios": [
            {"id": "SCN-BASE", "type": "base_case", "title": "Base case — all exits",
             "scenario_objective": {"purpose": "the undegraded case to compare against",
                                    "conditions": {"exits_discounted": []}},
             "evacuation_routes": {
                 "exits_available": ["E1", "E2"],
                 "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E1", "note": ""}],
                 "restricted_areas": []},
             "occupancy": {"occupants_total": 20, "occupancy_state": "night"},
             "occupant_distribution": ["G: 20"], "assumptions": ["all exits usable"],
             "bottlenecks": [], "risks": [], "narrative": "All leave.", "simulation": _simulation(),
             "fire_conditions": _fire(),
             "regulatory_justification": "ENG-R11/R12", "ai_explanation": "baseline"},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "One exit discounted",
             "scenario_objective": {"purpose": "tests resilience to losing the front door",
                                    "conditions": {"exits_discounted": ["E1"]}},
             "evacuation_routes": {
                 "exits_available": ["E2"],
                 "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E2", "note": "reroute"}],
                 "restricted_areas": [{"area": "Store", "reason": "fire origin"}]},
             "occupancy": {"occupants_total": 20, "occupancy_state": "night"},
             "occupant_distribution": ["G: 20"], "assumptions": ["E1 blocked"],
             "bottlenecks": ["E2"], "risks": ["congestion"], "narrative": "Reroute.",
             "simulation": _simulation(), "fire_conditions": _fire("Store", affected_exits=["E1"]),
             "regulatory_justification": "ADB discounted-exit principle", "ai_explanation": "resilience"},
        ],
    }


def test_one_record_per_scenario_with_six_keys():
    recs = build_records(_obj())
    assert len(recs) == 2
    for r in recs:
        assert set(r.keys()) == SIX_KEYS


def test_records_are_numbered_scn_001_upwards():
    """The deliverable numbers its own records, whatever ids the object happens to carry."""
    recs = build_records(_obj())
    assert [r["unique_id"] for r in recs] == ["SCN-001", "SCN-002"]


def test_relevantifc_elements_resolve_real_ids():
    recs = build_records(_obj())
    blocked = next(r for r in recs if r["unique_id"] == "SCN-002")
    ids = {e["id"] for e in blocked["relevant_ifc_element"]}
    assert {"E1", "E2", "ST1"} <= ids                       # available + discounted exits + stair
    types = {e["ifc_type"] for e in blocked["relevant_ifc_element"]}
    # an egress simulation runs over every opening, so lifts are listed too
    assert types == {"IfcDoor", "IfcStair", "IfcTransportElement"}


def test_scenario_body_is_nested():
    recs = build_records(_obj())
    base = recs[0]
    assert base["scenario"]["occupancy"]["occupants_total"] == 20
    assert base["scenario"]["scenario_objective"]["conditions"]["exits_discounted"] == []


# ---- the record as a simulation input --------------------------------------------------------------

def testifc_elements_cover_doors_stairs_and_lifts_with_a_state():
    recs = build_records(_obj())
    kinds = {e["id"]: e["kind"] for e in recs[0]["relevant_ifc_element"]}
    assert kinds == {"E1": "final_exit", "E2": "final_exit", "D1": "internal_door",
                     "ST1": "stair", "LIFT1": "elevator"}
    stair = next(e for e in recs[0]["relevant_ifc_element"] if e["id"] == "ST1")
    assert stair["rise_m"] == 3.0 and stair["going_m"] == 4.5


def test_geometry_the_simulator_reads_from_the_ifc_is_not_repeated_here():
    """``position`` and ``connects`` come with the geometry on IFC import, keyed on the same
    GlobalId — repeating them in the record only invites the two copies to disagree."""
    for element in build_records(_obj())[0]["relevant_ifc_element"]:
        assert "position" not in element
        assert "connects" not in element


def test_a_discounted_exit_is_closed_in_that_scenario_only():
    recs = {r["unique_id"]: r for r in build_records(_obj())}
    base = {e["id"]: e["state"] for e in recs["SCN-001"]["relevant_ifc_element"]}
    blocked = {e["id"]: e["state"] for e in recs["SCN-002"]["relevant_ifc_element"]}
    assert base["E1"] == base["E2"] == "open"
    assert blocked["E1"] == "closed" and blocked["E2"] == "open"


def test_the_raw_simulation_block_is_not_shipped_verbatim():
    """The record carries the parameters a study needs under named keys — simulation_settings,
    pre_movement_time, movement_characteristic and evacuation_time — not the whole internal
    simulation block with its working."""
    obj = _obj()
    assert obj["scenarios"][0]["simulation"]                    # still generated and validated
    for rec in build_records(obj):
        assert "simulation" not in rec["scenario"]
        movement = rec["scenario"]["movement_characteristic"]
        assert "occupancy_multipliers" not in movement
        # how long the run goes for is run configuration, and has its own block
        assert "duration" not in movement and "end_time_s" not in movement


def test_the_record_body_is_the_slim_set_of_fields():
    """The working that stays in the building object must not leak back into the deliverable."""
    body = {"scenario_objective", "evacuation_routes", "occupancy", "occupant_distribution",
            "simulation_settings", "pre_movement_time", "movement_characteristic",
            "evacuation_time", "fire_related_conditions", "assumptions", "bottlenecks", "risks"}
    for rec in build_records(_obj()):
        assert set(rec["scenario"].keys()) == body


# ---- the three scenario-input blocks ---------------------------------------------------------------

def test_pre_movement_time_ships_as_a_distribution():
    """Pre-movement is often the largest term in total evacuation time; a single number loses the
    spread, so the response delay carries the distribution and the basis behind it."""
    pre = build_records(_obj())[0]["scenario"]["pre_movement_time"]
    assert pre["response_delay"] == {"distribution": "normal", "mean_s": 60.0, "sd_s": 30.0,
                                     "basis": "alert occupants, staff-assisted"}


def test_pre_movement_time_breaks_the_clock_into_its_four_parts():
    """Total pre-movement is detection + alarm + recognition + response. A study that ships only the
    response delay cannot say what its clock started from."""
    pre = build_records(_obj())[0]["scenario"]["pre_movement_time"]
    assert set(pre) == {"detection", "alarm", "recognition", "response_delay"}
    assert all(pre[part] for part in ("detection", "alarm", "recognition"))


def test_simulation_settings_carry_the_starting_state_and_the_run_length():
    settings = build_records(_obj())[0]["scenario"]["simulation_settings"]
    assert settings["start_conditions"]
    assert settings["duration"] == {"seconds": 900.0, "basis": "long enough to clear the building"}


def test_evacuation_time_is_labelled_an_estimate_not_a_result():
    """The figure is the AI's arithmetic, stated before the run. A reader who mistakes it for the
    Pathfinder result would be reading an unmeasured number as a measured one."""
    estimate = build_records(_obj())[0]["scenario"]["evacuation_time"]
    assert estimate["estimated_total_s"] == 300.0
    assert estimate["basis"]
    assert "not a simulation result" in estimate["source"]


def test_movement_characteristic_carries_the_model_and_the_profile_mix():
    movement = build_records(_obj())[0]["scenario"]["movement_characteristic"]
    assert movement["movement_model"] == "steering"
    profile = movement["profiles"][0]
    assert profile["speed_ms_mean"] == 1.19
    assert profile["shoulder_width_m"] == 0.46
    assert profile["fraction"] == 1.0
    assert profile["basis"]                                     # every value states its reasoning


def test_fire_related_conditions_carry_origin_detection_and_smoke():
    fire = build_records(_obj())[1]["scenario"]["fire_related_conditions"]
    assert fire["fire_origin"] == "Store"
    assert fire["affected_exits"] == ["E1"]                     # the exit this scenario discounts
    assert fire["detection_and_alarm"] and fire["smoke_conditions"] and fire["basis"]


def test_the_three_blocks_survive_a_scenario_that_never_got_them():
    """A hand-edited or older object must export a shaped block, not blow up or drop the key."""
    obj = _obj()
    del obj["scenarios"][0]["simulation"]
    del obj["scenarios"][0]["fire_conditions"]
    body = build_records(obj)[0]["scenario"]
    assert body["pre_movement_time"]["response_delay"]["mean_s"] is None
    assert body["movement_characteristic"]["profiles"] == []
    assert body["fire_related_conditions"]["fire_origin"] is None
    assert body["simulation_settings"]["duration"]["seconds"] is None
    assert body["evacuation_time"]["estimated_total_s"] is None


def test_occupancy_is_the_headline_total_and_state_only():
    for rec in build_records(_obj()):
        occupancy = rec["scenario"]["occupancy"]
        assert occupancy == {"occupants_total": 20, "occupancy_state": "night"}


def test_the_population_is_stated_once_not_beside_the_conditions():
    """occupants_total and occupancy_state belong to the occupancy block. Repeating them in the
    conditions gives the record two copies that can drift apart."""
    for rec in build_records(_obj()):
        conditions = rec["scenario"]["scenario_objective"]["conditions"]
        assert "occupants_total" not in conditions
        assert "occupancy_state" not in conditions


def test_routes_and_available_exits_sit_under_evacuation_routes():
    """Where people may go is one subject: the exits left open, the paths to them, and the areas
    they must keep out of."""
    body = build_records(_obj())[1]["scenario"]
    assert "routes" not in body                                  # not a loose sibling key
    routes = body["evacuation_routes"]
    assert routes["exits_available"] == ["E2"]
    assert routes["routes"][0]["to_exit"] == "E2"
    assert routes["restricted_areas"] == [{"area": "Store", "reason": "fire origin"}]
    assert "exits_available" not in body["scenario_objective"]["conditions"]


def test_the_deliverable_is_json_all_the_way_down():
    """The project's output format is JSON — nothing may be smuggled out as delimited text."""
    payload = export_records(_obj())
    records = json.loads(payload)                      # must parse as JSON on its own

    def leaves(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from leaves(v)
        elif isinstance(node, list):
            for v in node:
                yield from leaves(v)
        elif isinstance(node, str):
            yield node

    # a comma- or pipe-delimited row would show up as a string carrying separators
    assert not [s for s in leaves(records) if "|" in s]
    assert not [s for s in leaves(records) if "\t" in s or "\r\n" in s]


def test_nothing_in_the_package_reads_or_writes_csv():
    """The guard behind the JSON-only rule: CSV crept in once as a simulator import format, and the
    only reliable way to keep it out is to fail the build when it comes back."""
    this_file = os.path.abspath(__file__)
    project = os.path.dirname(os.path.dirname(os.path.dirname(this_file)))
    offenders = []
    # only our own source — the virtualenv lives beside it in evacuation_project/
    for source_dir in ("core_backend", "frontend"):
        for root, dirs, files in os.walk(os.path.join(project, source_dir)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = os.path.join(root, name)
                if not name.endswith(".py") or path == this_file:   # this file names CSV to ban it
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    source = f.read()
                if "import csv" in source or ".csv" in source:
                    offenders.append(os.path.relpath(path, project))
    assert offenders == [], f"CSV has crept back into: {offenders}"
