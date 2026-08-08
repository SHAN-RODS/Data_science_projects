"""Export the whole-building evacuation scenario object.

Two forms:
  * ``export_json``    — the full schema-shaped object (used for the in-app preview / reference).
  * ``export_records`` — the deliverable: a flat array of six-field records, ONE per scenario
    (unique_id, description, relevant_ifc_element, regulatory_justification, ai_explanation, scenario).

The deliverable is also the **input to an egress simulation study**. The simulator imports the
*geometry* from the same IFC, so no geometry is carried here — every element is keyed on the IFC
GlobalId, in metres, in IFC world coordinates.

  ``unique_id``             SCN-001, SCN-002 … numbered in generation order.
  ``relevant_ifc_element``  every door, stair and lift the scenario runs over, with this scenario's
                            open/closed state.
  ``scenario.occupancy``    how many occupants this scenario evacuates, and in which occupancy state.

The full building object (``export_json``) keeps the longer working — the AI-chosen simulation
set-up, per-room placement, benchmark distances, narrative and the unassessed rooms. The records
deliverable carries only the fields above.
"""

import json

from core_backend.exit_names import discounted_exit_ids, exit_names, named


def _ifc_elements(obj, scn):
    """Every IFC element the scenario runs over: final exits, internal doors, stairs and lifts.

    Wider than "the exits this scenario names" because an egress simulation uses all of them — and
    ``state`` is what makes the list scenario-specific: an exit the scenario discounts is ``closed``.

    Final exits also carry ``exit_name`` — the "Exit 3" the scenario's prose and conditions use, so a
    reader can match a closed door to the sentence that closed it without decoding a GlobalId.

    Neither ``position`` nor ``connects`` is carried: the simulator imports both from the same IFC
    with the geometry, keyed on the GlobalId here. They stay in the full building object (``exits``,
    ``doors``, ``circulation``) for anyone who wants to read them.
    """
    names = exit_names(obj.get("exits", []))
    discounted = set(discounted_exit_ids(scn))
    elements = []

    def add(item, ifc_type, kind, **extra):
        elements.append({
            "id": item["id"],
            "ifc_type": ifc_type,
            "kind": kind,
            "name": item.get("name"),
            "width_m": item.get("width_m"),
            "state": "closed" if item["id"] in discounted else "open",
            **extra,
        })

    for e in obj.get("exits", []):
        add(e, "IfcDoor", "final_exit", exit_name=named(e["id"], names))
    for d in obj.get("doors", []):
        add(d, "IfcDoor", "internal_door")
    for c in obj.get("circulation", []):
        add(c, "IfcStair", "stair",
            rise_m=c.get("rise_m"), going_m=c.get("going_m"), slope_m=c.get("slope_m"))
    for t in obj.get("elevators", []):
        add(t, "IfcTransportElement",
            "evacuation_elevator" if t.get("is_evac_lift") else "elevator",
            is_evac_lift=t.get("is_evac_lift"))
    return elements


def _occupancy(scn):
    """This scenario's occupancy line: how many people are being evacuated, in which occupancy state.

    Read from the computed occupancy block where one is attached, falling back to the conditions the
    scenario was written against.
    """
    occ = scn.get("occupancy") or {}
    conditions = scn.get("conditions") or {}
    return {
        "occupants_total": occ.get("occupants_total", conditions.get("occupants_total")),
        "occupancy_state": occ.get("occupancy_state", conditions.get("occupancy_state")),
    }


def build_records(obj):
    """One six-field record per AI-proposed scenario, numbered SCN-001, SCN-002 … in scenario order.

    The ids are assigned here rather than copied from the object, so the deliverable is uniformly
    numbered even when re-exported from an object generated before that rule. IFC elements are
    resolved deterministically from the object (real ids, real states); the reasoning fields come
    from the LLM.
    """
    records = []
    for number, scn in enumerate(obj.get("scenarios", []), start=1):
        records.append({
            "unique_id": f"SCN-{number:03d}",
            "description": scn.get("title"),
            "relevant_ifc_element": _ifc_elements(obj, scn),
            "regulatory_justification": scn.get("regulatory_justification"),
            "ai_explanation": scn.get("ai_explanation"),
            "scenario": {
                "conditions": scn.get("conditions", {}),
                "occupancy": _occupancy(scn),
                "occupant_distribution": scn.get("occupant_distribution", []),
                "assumptions": scn.get("assumptions", []),
                "routes": scn.get("routes", []),
                "bottlenecks": scn.get("bottlenecks", []),
                "risks": scn.get("risks", []),
            },
        })
    return records


def export_json(scenario_object):
    return json.dumps(scenario_object, indent=2, ensure_ascii=False)


def export_records(obj):
    return json.dumps(build_records(obj), indent=2, ensure_ascii=False)


if __name__ == "__main__":
    import os
    import sys

    from core_backend.validation import validate

    # a saved .json is re-exported without another generation call
    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = validate(json.load(f))
    else:
        from core_backend.scenario_generation_llm import build_full_scenario
        from core_backend.sample_paths import resolve_ifc

        args = [a for a in sys.argv if not a.startswith("--")]
        obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))
        scratch = os.path.join(os.environ.get("TEMP", "."), "evac_scenario.json")
        with open(scratch, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        print(f"(saved generated object to {scratch} — re-export for free with that path)\n")

    out = os.path.join(os.environ.get("TEMP", "."), "evacuation_scenario_records.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write(export_records(obj))
    print(f"wrote {out}\n")

    for record in build_records(obj):
        occ = record["scenario"]["occupancy"]
        closed = [e["id"] for e in record["relevant_ifc_element"] if e["state"] == "closed"]
        print(f"{record['unique_id']}: {len(record['relevant_ifc_element'])} IFC element(s) "
              f"({len(closed)} closed) | {occ['occupants_total']} occupant(s), "
              f"{occ['occupancy_state']} occupancy")
