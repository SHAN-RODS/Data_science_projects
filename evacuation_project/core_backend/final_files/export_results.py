"""Export the whole-building evacuation scenario object.

Two forms:
  * ``export_json``    — the full schema-shaped object (used for the in-app preview / reference).
  * ``export_records`` — the deliverable: a flat array of six-field records, ONE per scenario
    (unique_id, description, relevant_ifc_element, regulatory_justification, ai_explanation, scenario).
"""

import json


def export_json(scenario_object):
    return json.dumps(scenario_object, indent=2, ensure_ascii=False)


def build_records(obj):
    """One six-field record per AI-proposed scenario. IFC elements are resolved deterministically from
    the object's exits/stairs (real ids, not model-invented); the reasoning fields come from the LLM."""
    exits = {e["id"]: e for e in obj.get("exits", [])}
    circulation = obj.get("circulation", [])
    records = []
    for scn in obj.get("scenarios", []):
        cond = scn.get("conditions", {})
        elems = []
        for eid in (cond.get("exits_available", []) + cond.get("exits_discounted", [])):
            e = exits.get(eid)
            elems.append({"id": eid, "name": (e or {}).get("name"), "ifc_type": "IfcDoor"})
        for c in circulation:
            elems.append({"id": c["id"], "name": c.get("name"), "ifc_type": "IfcStair"})
        records.append({
            "unique_id": scn.get("id"),
            "description": scn.get("title"),
            "relevant_ifc_element": elems,
            "regulatory_justification": scn.get("regulatory_justification"),
            "ai_explanation": scn.get("ai_explanation"),
            "scenario": {
                "conditions": cond,
                "occupant_distribution": scn.get("occupant_distribution", []),
                "assumptions": scn.get("assumptions", []),
                "routes": scn.get("routes", []),
                "bottlenecks": scn.get("bottlenecks", []),
                "risks": scn.get("risks", []),
                "narrative": scn.get("narrative"),
            },
        })
    return records


def export_records(obj):
    return json.dumps(build_records(obj), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    from core_backend.scenario_generation_llm import build_full_scenario
    from core_backend.validation import validate
    from core_backend.sample_paths import resolve_ifc

    args = [a for a in sys.argv if not a.startswith("--")]
    obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))
    print(export_records(obj))
