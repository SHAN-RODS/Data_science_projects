"""The exported deliverable is a flat array of six-field records, one per scenario (no API / no IFC)."""

from core_backend.export_results import build_records

SIX_KEYS = {"unique_id", "description", "relevant_ifc_element",
            "regulatory_justification", "ai_explanation", "scenario"}


def _obj():
    return {
        "exits": [{"id": "E1", "name": "Front door", "type": "final_exit", "width_m": 1.0},
                  {"id": "E2", "name": "Rear door", "type": "final_exit", "width_m": 1.0}],
        "circulation": [{"id": "ST1", "name": "Main stair", "type": "internal_stair", "width_m": 1.2}],
        "scenarios": [
            {"id": "SCN-BASE", "type": "base_case", "title": "Base case — all exits",
             "conditions": {"exits_available": ["E1", "E2"], "exits_discounted": [],
                            "occupancy_state": "night", "occupants_total": 20},
             "occupant_distribution": ["G: 20"], "assumptions": ["all exits usable"],
             "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E1", "note": ""}],
             "bottlenecks": [], "risks": [], "narrative": "All leave.",
             "regulatory_justification": "ENG-R11/R12", "ai_explanation": "baseline"},
            {"id": "SCN-EXIT-BLOCKED", "type": "one_exit_discounted", "title": "One exit discounted",
             "conditions": {"exits_available": ["E2"], "exits_discounted": ["E1"],
                            "occupancy_state": "night", "occupants_total": 20},
             "occupant_distribution": ["G: 20"], "assumptions": ["E1 blocked"],
             "routes": [{"from_area": "G", "via": "ST1", "to_exit": "E2", "note": "reroute"}],
             "bottlenecks": ["E2"], "risks": ["congestion"], "narrative": "Reroute.",
             "regulatory_justification": "ADB discounted-exit principle", "ai_explanation": "resilience"},
        ],
    }


def test_one_record_per_scenario_with_six_keys():
    recs = build_records(_obj())
    assert len(recs) == 2
    for r in recs:
        assert set(r.keys()) == SIX_KEYS


def test_relevant_ifc_elements_resolve_real_ids():
    recs = build_records(_obj())
    blocked = next(r for r in recs if r["unique_id"] == "SCN-EXIT-BLOCKED")
    ids = {e["id"] for e in blocked["relevant_ifc_element"]}
    assert {"E1", "E2", "ST1"} <= ids                       # available + discounted exits + stair
    types = {e["ifc_type"] for e in blocked["relevant_ifc_element"]}
    assert types == {"IfcDoor", "IfcStair"}


def test_scenario_body_is_nested():
    recs = build_records(_obj())
    base = recs[0]
    assert base["scenario"]["conditions"]["occupants_total"] == 20
    assert "narrative" in base["scenario"]
