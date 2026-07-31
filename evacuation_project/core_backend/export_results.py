"""Export the whole-building evacuation scenario object.

Two forms:
  * ``export_json``    — the full schema-shaped object (used for the in-app preview / reference).
  * ``export_records`` — the deliverable: a flat array of six-field records, ONE per scenario
    (unique_id, description, relevant_ifc_element, regulatory_justification, ai_explanation, scenario).

The deliverable is also the **input to an egress simulation study**, so each record is written to be
self-contained: everything needed to set one simulation up and run it, and nothing that has to be
looked up in another file. The simulator imports the *geometry* from the same IFC, so no geometry is
carried here — every element is keyed on the IFC GlobalId, in metres, in IFC world coordinates.

  ``relevant_ifc_element``  every door, stair and lift the scenario runs over, with this scenario's
                            open/closed state.
  ``scenario.simulation``   the AI-chosen set-up: movement model, pre-movement distribution and
                            occupant profiles, each with its written basis.
  ``scenario.occupancy``    computed per-room occupant counts, seed points, profile mix and goals.
  ``scenario.benchmark_travel_distances``  our own measured distances — a validation target to check
                            the simulator's navigation mesh against, NOT an input.

That self-containment costs some repetition (``model``, the benchmark and ``not_assessed`` are the same
in every record). It is worth it: one record is one runnable study.
"""

import json

from core_backend.occupant_placement import BENCHMARK_NOTE, occupancy_block

DOOR_FLOW_NOTE = ("door widths are the IFC values; leave the simulator's boundary-layer and "
                  "specific-flow defaults unless the study states otherwise")


# ---------------------------------------------------------------------------------------------------
# relevant_ifc_element — the components, with the per-scenario door states.
# ---------------------------------------------------------------------------------------------------
def _ifc_elements(obj, scn):
    """Every IFC element the scenario runs over: final exits, internal doors, stairs and lifts.

    Wider than "the exits this scenario names" because an egress simulation uses all of them — and
    ``state`` is what makes the list scenario-specific: an exit the scenario discounts is ``closed``.
    """
    discounted = set((scn.get("conditions") or {}).get("exits_discounted") or [])
    elements = []

    def add(item, ifc_type, kind, connects, **extra):
        elements.append({
            "id": item["id"],
            "ifc_type": ifc_type,
            "kind": kind,
            "name": item.get("name"),
            "width_m": item.get("width_m"),
            "position": item.get("position"),
            "state": "closed" if item["id"] in discounted else "open",
            "connects": connects or [],
            **extra,
        })

    for e in obj.get("exits", []):
        add(e, "IfcDoor", "final_exit", [])
    for d in obj.get("doors", []):
        add(d, "IfcDoor", "internal_door", d.get("connects"))
    for c in obj.get("circulation", []):
        add(c, "IfcStair", "stair", c.get("connects_storeys"),
            rise_m=c.get("rise_m"), going_m=c.get("going_m"), slope_m=c.get("slope_m"))
    for t in obj.get("elevators", []):
        add(t, "IfcTransportElement",
            "evacuation_elevator" if t.get("is_evac_lift") else "elevator", [],
            is_evac_lift=t.get("is_evac_lift"))
    return elements


def _benchmark(obj):
    """Our measured travel distances, labelled as what they are: something to check the simulation
    against afterwards, not a number to feed into it."""
    return {
        "note": BENCHMARK_NOTE,
        "method": (obj.get("provenance") or {}).get("distance_method"),
        "by_room": [{"guid": s["guid"], "storey": s.get("storey"),
                     "travel_distance_m": s.get("travel_distance_m"),
                     "most_remote_point": s.get("most_remote_point"),
                     "nearest_exit": s.get("nearest_exit")}
                    for s in obj.get("spaces", []) if s.get("travel_distance_m") is not None],
    }


def _simulation(scn):
    """The scenario's AI-chosen simulation set-up, plus the one note the simulator needs about doors."""
    sim = dict(scn.get("simulation") or {})
    if sim:
        sim["door_flow_note"] = DOOR_FLOW_NOTE
    return sim


def build_records(obj):
    """One six-field record per AI-proposed scenario. IFC elements, occupant placement and travel
    distances are resolved deterministically from the object (real ids, real computed numbers); the
    reasoning fields and the simulation parameters come from the LLM."""
    model = obj.get("model", {})
    benchmark = _benchmark(obj)
    not_assessed = obj.get("not_assessed", [])

    records = []
    for scn in obj.get("scenarios", []):
        records.append({
            "unique_id": scn.get("id"),
            "description": scn.get("title"),
            "relevant_ifc_element": _ifc_elements(obj, scn),
            "regulatory_justification": scn.get("regulatory_justification"),
            "ai_explanation": scn.get("ai_explanation"),
            "scenario": {
                # how to line this record up with the geometry the simulator imports from the IFC
                "model": model,
                "conditions": scn.get("conditions", {}),
                "simulation": _simulation(scn),
                # computed at generation time; recomputed here only for an object that predates it
                "occupancy": scn.get("occupancy") or occupancy_block(obj, scn),
                "benchmark_travel_distances": benchmark,
                "occupant_distribution": scn.get("occupant_distribution", []),
                "assumptions": scn.get("assumptions", []),
                "routes": scn.get("routes", []),
                "bottlenecks": scn.get("bottlenecks", []),
                "risks": scn.get("risks", []),
                "narrative": scn.get("narrative"),
                # an unassessed room silently drops its occupants — this must travel with the record
                "not_assessed": not_assessed,
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
              f"({len(closed)} closed) | placed {occ['placed_total']}/{occ['occupants_total']} "
              f"occupants across {len(occ['by_room'])} room(s), {occ['unplaced_total']} unplaced")
