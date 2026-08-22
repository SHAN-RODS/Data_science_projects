#It exports the whole building scenario having unique_Id, description, relevant ifc element, regulatory justification, ai explanation and
# the important inputs required in an evacuation scenario

import json
import os
import sys
from core_backend.exit_names import discounted_exit_ids, exit_names, named
from core_backend.validation import validate
from core_backend.scenario_generation_llm_1 import build_full_scenario
from core_backend.sample_paths import resolve_ifc

def ifc_elements(obj, scn):
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


def occupancy_summary(scn):
    occ = scn.get("occupancy") or {}
    return {
        "occupants_total": occ.get("occupants_total"),
        "occupancy_state": occ.get("occupancy_state"),
        "occupancy_state_label": occ.get("occupancy_state_label"),
        "rationale": scn.get("occupancy_rationale"),
    }


def pre_movement_time(scn):
    pre = (scn.get("simulation") or {}).get("pre_movement") or {}
    delay = pre.get("response_delay") or {}
    return {
        "detection": pre.get("detection"),
        "alarm": pre.get("alarm"),
        "recognition": pre.get("recognition"),
        "response_delay": {
            "distribution": delay.get("distribution"),
            "mean_s": delay.get("mean_s"),
            "sd_s": delay.get("sd_s"),
            "basis": delay.get("basis"),
        },
    }


def simulation_settings(scn):
    settings = (scn.get("simulation") or {}).get("simulation_settings") or {}
    duration = settings.get("duration") or {}
    return {
        "start_conditions": settings.get("start_conditions"),
        "duration": {"seconds": duration.get("seconds"), "basis": duration.get("basis")},
    }


def evacuation_time(scn):
    estimate = (scn.get("simulation") or {}).get("evacuation_time") or {}
    return {
        "estimated_total_s": estimate.get("estimated_total_s"),
        "basis": estimate.get("basis"),
        "source": "engineering estimate stated ahead of the run, not a simulation result",
    }


def movement_characteristic(scn):
    sim = scn.get("simulation") or {}
    return {
        "movement_model": sim.get("movement_model"),
        "profiles": [{
            "name": p.get("name"),
            "fraction": p.get("fraction"),
            "speed_distribution": p.get("speed_distribution"),
            "speed_ms_mean": p.get("speed_ms_mean"),
            "speed_ms_sd": p.get("speed_ms_sd"),
            "shoulder_width_m": p.get("shoulder_width_m"),
            "basis": p.get("basis"),
        } for p in sim.get("profiles") or []],
    }


def fire_related_conditions(scn):
    fire = scn.get("fire_conditions") or {}
    return {
        "fire_origin": fire.get("fire_origin"),
        "fire_origin_storey": fire.get("fire_origin_storey"),
        "affected_exits": fire.get("affected_exits", []),
        "affected_routes": fire.get("affected_routes", []),
        "detection_and_alarm": fire.get("detection_and_alarm"),
        "smoke_conditions": fire.get("smoke_conditions"),
        "basis": fire.get("basis"),
    }


def build_records(obj):
    records = []
    for number, scn in enumerate(obj.get("scenarios", []), start=1):
        records.append({
            "unique_id": f"SCN-{number:03d}",
            "description": scn.get("title"),
            "relevant_ifc_element": ifc_elements(obj, scn),
            "regulatory_justification": scn.get("regulatory_justification"),
            "ai_explanation": scn.get("ai_explanation"),
            "scenario": {
                "scenario_objective": scn.get("scenario_objective", {}),
                "evacuation_routes": scn.get("evacuation_routes", {}),
                "occupancy": occupancy_summary(scn),
                "occupant_distribution": scn.get("occupant_distribution", []),
                "simulation_settings": simulation_settings(scn),
                "pre_movement_time": pre_movement_time(scn),
                "movement_characteristic": movement_characteristic(scn),
                "evacuation_time": evacuation_time(scn),
                "fire_related_conditions": fire_related_conditions(scn),
                "assumptions": scn.get("assumptions", []),
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
    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = validate(json.load(f))
    else:
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
