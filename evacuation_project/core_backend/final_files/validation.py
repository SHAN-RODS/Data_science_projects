
import re
from collections import defaultdict

from jsonschema import Draft202012Validator

from core_backend.scenario_schema import SCENARIO_SCHEMA

EXIT_CAPACITY_PERSONS_PER_M = 200

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def allowed_floats(obj):
    allowed = set()

    def add(value):
        if isinstance(value, (int, float)):
            allowed.add(float(value))

    b = obj["building"]
    add(b.get("storeys"))
    add(b.get("total_floor_area_m2"))
    add(b.get("total_occupant_load"))
    for e in obj["exits"]:
        add(e.get("width_m"))
    for s in obj["spaces"]:
        add(s.get("area_m2"))
        add(s.get("occupant_load"))
        add(s.get("travel_distance_m"))
    for scn in obj["scenarios"]:
        c = scn.get("conditions", {})
        add(c.get("occupants_total"))
        add(len(c.get("exits_available", [])))
        add(len(c.get("exits_discounted", [])))

    by_storey_occ = defaultdict(int)
    by_storey_dist = defaultdict(float)
    for s in obj["spaces"]:
        storey = s.get("storey")
        if s.get("occupant_load"):
            by_storey_occ[storey] += s["occupant_load"]
        if s.get("travel_distance_m"):
            by_storey_dist[storey] = max(by_storey_dist[storey], s["travel_distance_m"])
    for value in by_storey_occ.values():
        add(value)
    for value in by_storey_dist.values():
        add(round(value, 1))
    return allowed


def is_grounded(value, allowed):
    if value <= 12 and value == int(value):
        return True
    for a in allowed:
        if abs(value - a) <= max(0.05 * abs(a), 0.5):  
            return True
    return False


def _scenario_text(scn, id_tokens):
    parts = [scn.get("narrative", ""), scn.get("title", "")]
    parts += scn.get("occupant_distribution", []) or []
    parts += scn.get("assumptions", []) or []
    parts += scn.get("bottlenecks", []) or []
    parts += scn.get("risks", []) or []
    for r in scn.get("routes", []) or []:
        parts += [r.get("from_area", ""), r.get("via", ""), r.get("to_exit", ""), r.get("note", "")]
    text = " ".join(str(p) for p in parts)
    for token in id_tokens:
        if token:
            text = text.replace(token, " ")
    return text


def number_factcheck(obj):
    """Return the list of narrative numbers that do not trace to the structured record."""
    allowed = allowed_floats(obj)
    id_tokens = {e.get("id") for e in obj["exits"]} | {s.get("guid") for s in obj["spaces"]}
    ungrounded = []
    for scn in obj["scenarios"]:
        for token in NUM_RE.findall(_scenario_text(scn, id_tokens)):
            try:
                value = float(token)
            except ValueError:
                continue
            if not is_grounded(value, allowed):
                ungrounded.append({"scenario": scn["id"], "value": token})
    return ungrounded

def validate(obj):
    validator = Draft202012Validator(SCENARIO_SCHEMA)
    schema_errors = [f"{'/'.join(str(p) for p in e.path)}: {e.message}"
                     for e in validator.iter_errors(obj)]

    spaces = obj["spaces"]
    flagged = {na.get("element") for na in obj.get("not_assessed", [])}
    occupiable = [s for s in spaces if s.get("occupant_load") is None or s.get("occupant_load", 0) > 0]
    every_space_ok = all(s["reachable"] or s["guid"] in flagged for s in occupiable)
    two_scenarios = len(obj["scenarios"]) >= 2

    space_occ = sum(s["occupant_load"] for s in spaces if s.get("occupant_load"))
    reconcile = space_occ == obj["building"]["total_occupant_load"]

    capacity = sum((e.get("width_m") or 0) for e in obj["exits"]) * EXIT_CAPACITY_PERSONS_PER_M
    total_occ = obj["building"]["total_occupant_load"]
    within_capacity = (total_occ <= capacity) if capacity > 0 else None

    dist_ok = all(s.get("travel_distance_m") is None or s["travel_distance_m"] >= 0
                  for s in spaces)

    ungrounded = number_factcheck(obj)

    obj["validation"] = {
        "schema_valid": not schema_errors,
        "schema_errors": schema_errors,
        "invariants_checked": {
            "every_space_reachable_or_flagged": every_space_ok,
            "at_least_two_scenarios": two_scenarios,
            "occupant_totals_reconcile": reconcile,
            "occupants_within_exit_capacity": within_capacity,
            "travel_distance_non_negative": dist_ok,
        },
        "exit_capacity_persons": round(capacity),
        "number_factcheck": "passed" if not ungrounded else "review",
        "ungrounded_numbers": ungrounded,
        "not_assessed_count": len(obj.get("not_assessed", [])),
    }
    return obj


if __name__ == "__main__":
    import sys
    import json
    import os

    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = json.load(f)
    else:
        from core_backend.scenario_generation_llm import build_full_scenario
        from core_backend.sample_paths import default_ifc
        obj = build_full_scenario(default_ifc(), jurisdiction="england")
        scratch = os.path.join(os.environ.get("TEMP", "."), "evac_scenario.json")
        with open(scratch, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        print(f"(saved generated object to {scratch} — re-validate for free with that path)")

    obj = validate(obj)
    v = obj["validation"]
    print("\nValidation report:")
    print(f"schema_valid: {v['schema_valid']}  errors: {v['schema_errors'][:3]}")
    for name, result in v["invariants_checked"].items():
        print(f"{name}: {result}")
    print(f"exit_capacity_persons: {v['exit_capacity_persons']}  "
          f"(total occupants: {obj['building']['total_occupant_load']})")
    print(f"number_factcheck: {v['number_factcheck']}  "
          f"ungrounded: {v['ungrounded_numbers'][:8]}")
    print(f"not_assessed_count: {v['not_assessed_count']}")
