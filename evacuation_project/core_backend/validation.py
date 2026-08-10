
import re
from collections import defaultdict

from jsonschema import Draft202012Validator

from core_backend.exit_names import exit_names, unknown_exit_references
from core_backend.scenario_schema import scenario_schema
import sys
import json
import os
from core_backend.scenario_generation_llm import build_full_scenario
from core_backend.sample_paths import default_ifc

EXIT_CAPACITY_PERSONS_PER_M = 200

NUM_RE = re.compile(r"\d+(?:\.\d+)?")

SPEED_RANGE_MS = (0.5, 2.0)
SHOULDER_RANGE_M = (0.30, 0.70)
PRE_MOVEMENT_RANGE_S = (0.0, 1800.0)
END_TIME_RANGE_S = (60.0, 86400.0)
ALLOWED_DISTRIBUTIONS = {"constant", "uniform", "normal", "lognormal", "log-normal", "log normal"}
ALLOWED_MOVEMENT_MODELS = {"steering", "sfpe"}
FRACTION_TOLERANCE = 0.01


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

        sim = scn.get("simulation") or {}
        add(sim.get("end_time_s"))
        pre = sim.get("pre_movement") or {}
        add(pre.get("mean_s"))
        add(pre.get("sd_s"))
        for profile in sim.get("profiles") or []:
            add(profile.get("speed_ms_mean"))
            add(profile.get("speed_ms_sd"))
            add(profile.get("shoulder_width_m"))
            add(profile.get("fraction"))
        for m in sim.get("occupancy_multipliers") or []:
            add(m.get("multiplier"))

    for case in obj.get("degraded_cases", []):
        for row in case.get("per_storey", []):
            add(row.get("occupants"))
            add(row.get("max_travel_distance_m"))
            add(row.get("unreachable"))

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


def scenario_text(scn, id_tokens):
    parts = [scn.get("narrative", ""), scn.get("title", "")]
    parts += scn.get("occupant_distribution", []) or []
    parts += scn.get("assumptions", []) or []
    parts += scn.get("bottlenecks", []) or []
    parts += scn.get("risks", []) or []
    for r in scn.get("routes", []) or []:
        parts += [r.get("from_area", ""), r.get("via", ""), r.get("to_exit", ""), r.get("note", "")]
    text = " ".join(str(p) for p in parts)
    for token in sorted((t for t in id_tokens if t), key=len, reverse=True):
        text = text.replace(token, " ")
    return text


def number_factcheck(obj):
    allowed = allowed_floats(obj)
    id_tokens = ({e.get("id") for e in obj["exits"]} | {s.get("guid") for s in obj["spaces"]}
                 | set(exit_names(obj["exits"]).values()))
    ungrounded = []
    for scn in obj["scenarios"]:
        for token in NUM_RE.findall(scenario_text(scn, id_tokens)):
            try:
                value = float(token)
            except ValueError:
                continue
            if not is_grounded(value, allowed):
                ungrounded.append({"scenario": scn["id"], "value": token})
    return ungrounded

def in_range(value, bounds):
    lo, hi = bounds
    return isinstance(value, (int, float)) and lo <= value <= hi


def simulation_parameter_issues(obj):
    issues = []
    for scn in obj.get("scenarios", []):
        sid = scn.get("id")
        sim = scn.get("simulation")
        if not sim:
            issues.append({"scenario": sid, "issue": "no simulation parameters given"})
            continue

        model = str(sim.get("movement_model", "")).strip().lower()
        if model not in ALLOWED_MOVEMENT_MODELS:
            issues.append({"scenario": sid, "field": "movement_model",
                           "issue": f"{sim.get('movement_model')!r} is not one of "
                                    f"{sorted(ALLOWED_MOVEMENT_MODELS)}"})
        if sim.get("end_time_s") is not None and not in_range(sim["end_time_s"], END_TIME_RANGE_S):
            issues.append({"scenario": sid, "field": "end_time_s",
                           "issue": f"{sim['end_time_s']} s outside {END_TIME_RANGE_S}"})

        pre = sim.get("pre_movement") or {}
        if not in_range(pre.get("mean_s"), PRE_MOVEMENT_RANGE_S):
            issues.append({"scenario": sid, "field": "pre_movement.mean_s",
                           "issue": f"{pre.get('mean_s')} s outside {PRE_MOVEMENT_RANGE_S}"})
        if str(pre.get("distribution", "")).strip().lower() not in ALLOWED_DISTRIBUTIONS:
            issues.append({"scenario": sid, "field": "pre_movement.distribution",
                           "issue": f"{pre.get('distribution')!r} is not a recognised distribution"})
        if not str(pre.get("basis") or "").strip():
            issues.append({"scenario": sid, "field": "pre_movement.basis", "issue": "no basis given"})

        profiles = sim.get("profiles") or []
        if not profiles:
            issues.append({"scenario": sid, "field": "profiles", "issue": "no occupant profiles given"})
        for p in profiles:
            name = p.get("name")
            if not in_range(p.get("speed_ms_mean"), SPEED_RANGE_MS):
                issues.append({"scenario": sid, "field": f"profiles[{name}].speed_ms_mean",
                               "issue": f"{p.get('speed_ms_mean')} m/s outside {SPEED_RANGE_MS}"})
            if not in_range(p.get("shoulder_width_m"), SHOULDER_RANGE_M):
                issues.append({"scenario": sid, "field": f"profiles[{name}].shoulder_width_m",
                               "issue": f"{p.get('shoulder_width_m')} m outside {SHOULDER_RANGE_M}"})
            if str(p.get("speed_distribution", "")).strip().lower() not in ALLOWED_DISTRIBUTIONS:
                issues.append({"scenario": sid, "field": f"profiles[{name}].speed_distribution",
                               "issue": f"{p.get('speed_distribution')!r} is not recognised"})
            if not str(p.get("basis") or "").strip():
                issues.append({"scenario": sid, "field": f"profiles[{name}].basis",
                               "issue": "no basis given"})
        if profiles:
            total = sum(float(p.get("fraction") or 0) for p in profiles)
            if abs(total - 1.0) > FRACTION_TOLERANCE:
                issues.append({"scenario": sid, "field": "profiles[].fraction",
                               "issue": f"fractions sum to {total:.3f}, not 1.0"})
    return issues


def placement_issues(obj):
    from core_backend.occupant_placement import scenario_occupancy

    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    issues = []
    for scn in obj.get("scenarios", []):
        sid = scn.get("id")
        target = (scn.get("conditions") or {}).get("occupants_total") or 0
        allocation, unplaced, unallocated = scenario_occupancy(obj, scn)
        placed, missing = sum(allocation.values()), sum(unplaced.values())

        stored = scn.get("occupancy")
        if stored and stored.get("placed_total") != placed:
            issues.append({"scenario": sid, "field": "occupancy.placed_total",
                           "issue": f"the stored occupancy block places {stored.get('placed_total')} "
                                    f"occupant(s) but the allocation recomputes to {placed} — the "
                                    f"block is stale or was edited by hand"})

        if target and placed + missing + unallocated != target:
            issues.append({"scenario": sid, "field": "occupants_total",
                           "issue": f"allocation accounts for {placed + missing + unallocated} of "
                                    f"{target} occupant(s) — the split does not conserve the total"})
        if missing:
            issues.append({"scenario": sid, "field": "occupancy.unplaced_rooms",
                           "issue": f"{missing} of {target} occupant(s) sit in {len(unplaced)} "
                                    f"room(s) with no traced egress path and cannot be simulated",
                           "rooms": sorted(unplaced)[:5]})

        if unallocated:
            capacity = target - unallocated
            issues.append({"scenario": sid, "field": "occupancy.unallocated_total",
                           "issue": f"occupants_total is {target} but this scenario's occupancy "
                                    f"multipliers leave rooms holding only {capacity} — "
                                    f"{unallocated} occupant(s) could not be allocated without "
                                    f"putting rooms over their computed load"})

        goalless = sorted(g for g in allocation if not space_by_guid[g].get("nearest_exit"))
        if goalless:
            issues.append({"scenario": sid, "field": "behavior",
                           "issue": f"{len(goalless)} seeded room(s) have no nearest_exit to aim at",
                           "rooms": goalless[:5]})
    return issues


def validate(obj):
    validator = Draft202012Validator(scenario_schema)
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

    unknown_exits = unknown_exit_references(obj)

    has_sim = any(scn.get("simulation") for scn in obj["scenarios"])
    sim_issues = simulation_parameter_issues(obj) if has_sim else []
    place_issues = placement_issues(obj) if has_sim else []

    obj["validation"] = {
        "schema_valid": not schema_errors,
        "schema_errors": schema_errors,
        "invariants_checked": {
            "every_space_reachable_or_flagged": every_space_ok,
            "at_least_two_scenarios": two_scenarios,
            "occupant_totals_reconcile": reconcile,
            "occupants_within_exit_capacity": within_capacity,
            "travel_distance_non_negative": dist_ok,
            "every_exit_named_exists": not unknown_exits,
            "simulation_parametersin_range": (not sim_issues) if has_sim else None,
            "every_occupant_placed_with_a_goal": (not place_issues) if has_sim else None,
        },
        "unknown_exit_references": unknown_exits,
        "exit_capacity_persons": round(capacity),
        "number_factcheck": "passed" if not ungrounded else "review",
        "ungrounded_numbers": ungrounded,
        "simulation_parameter_issues": sim_issues,
        "placement_issues": place_issues,
        "not_assessed_count": len(obj.get("not_assessed", [])),
    }
    return obj


if __name__ == "__main__":

    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = json.load(f)
    else:
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
    print(f"simulation_parameter_issues: {v['simulation_parameter_issues'][:5]}")
    print(f"placement_issues: {v['placement_issues'][:5]}")
    print(f"not_assessed_count: {v['not_assessed_count']}")
