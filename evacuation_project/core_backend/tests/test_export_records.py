"""The exported deliverable is a flat array of six-field records, one per scenario (no API / no IFC).

Each record carries the egress-simulation set-up: the IFC elements with this scenario's open/closed
states, the AI-chosen simulation parameters and the scenario's occupancy — all inside those same six
fields, and all as JSON. The longer working (per-room placement, benchmark distances, narrative,
unassessed rooms) stays in the full building object and is deliberately kept out of the records.
"""

import json
import os

from core_backend.export_results import build_records, export_records

SIX_KEYS = {"unique_id", "description", "relevant_ifc_element",
            "regulatory_justification", "ai_explanation", "scenario"}


def _simulation():
    return {
        "movement_model": "steering",
        "end_time_s": 900.0,
        "pre_movement": {"distribution": "normal", "mean_s": 60.0, "sd_s": 30.0,
                         "basis": "alert occupants, staff-assisted"},
        "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                      "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
                      "basis": "able adults on the level"}],
        "occupancy_multipliers": [],
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
             "conditions": {"exits_available": ["E1", "E2"], "exits_discounted": [],
                            "occupancy_state": "night", "occupants_total": 20},
             "occupant_distribution": ["G: 20"], "assumptions": ["all exits usable"],
             "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E1", "note": ""}],
             "bottlenecks": [], "risks": [], "narrative": "All leave.", "simulation": _simulation(),
             "regulatory_justification": "ENG-R11/R12", "ai_explanation": "baseline"},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "One exit discounted",
             "conditions": {"exits_available": ["E2"], "exits_discounted": ["E1"],
                            "occupancy_state": "night", "occupants_total": 20},
             "occupant_distribution": ["G: 20"], "assumptions": ["E1 blocked"],
             "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E2", "note": "reroute"}],
             "bottlenecks": ["E2"], "risks": ["congestion"], "narrative": "Reroute.",
             "simulation": _simulation(),
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


def test_relevant_ifc_elements_resolve_real_ids():
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
    assert base["scenario"]["conditions"]["occupants_total"] == 20


# ---- the record as a simulation input --------------------------------------------------------------

def test_ifc_elements_cover_doors_stairs_and_lifts_with_a_state():
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


def test_the_simulation_set_up_stays_in_the_building_object():
    """The AI-chosen parameters are reviewed against the full object, not shipped in the record."""
    obj = _obj()
    assert obj["scenarios"][0]["simulation"]                    # still generated and validated
    for rec in build_records(obj):
        assert "simulation" not in rec["scenario"]


def test_the_record_body_is_the_slim_set_of_fields():
    """The working that stays in the building object must not leak back into the deliverable."""
    body = {"conditions", "occupancy", "occupant_distribution",
            "assumptions", "routes", "bottlenecks", "risks"}
    for rec in build_records(_obj()):
        assert set(rec["scenario"].keys()) == body


def test_occupancy_is_the_headline_total_and_state_only():
    for rec in build_records(_obj()):
        occupancy = rec["scenario"]["occupancy"]
        assert occupancy == {"occupants_total": 20, "occupancy_state": "night"}


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
