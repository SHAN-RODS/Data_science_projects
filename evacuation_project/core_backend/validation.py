
import re
from collections import defaultdict

from jsonschema import Draft202012Validator

from core_backend.exit_names import (evacuation_routes, exit_names, scenario_conditions,
                                     unknown_exit_references)
from core_backend.scenario_schema import scenario_schema
import sys
import json
import os
#from core_backend.scenario_generation_llm_1 import build_full_scenario
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

# In residential space the night is the busy state: residents are home and asleep, while by day they
# are out. A scenario set that has it the other way round is inverted, not merely conservative.
# The trigger is a material sleeping population, not a residential majority — a block of flats with a
# large communal amenity is still a building whose night is the demanding one.
SLEEPING_USE_TYPES = {"dwelling", "bedroom", "living", "kitchen", "kitchen_living", "dining", "sauna"}
SLEEPING_SHARE_THRESHOLD = 0.25
NIGHT_WORDS = ("night", "asleep", "sleeping", "overnight", "night-time", "nighttime")
DAY_WORDS = ("day", "daytime", "day-time", "working hours", "office hours", "waking")


def scenario_total(scenario):
    """The scenario's occupant total. It is stated once, in the occupancy block."""
    return (scenario.get("occupancy") or {}).get("occupants_total")


def response_delay(scenario):
    """The pre-travel activity distribution, one of pre_movement's four parts."""
    pre = (scenario.get("simulation") or {}).get("pre_movement") or {}
    return pre.get("response_delay") or {}


def run_duration(scenario):
    """How long the run is allowed to go for, in seconds."""
    settings = (scenario.get("simulation") or {}).get("simulation_settings") or {}
    return (settings.get("duration") or {}).get("seconds")


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
        add(scenario_total(scn))
        add(len(evacuation_routes(scn).get("exits_available", [])))
        add(len(scenario_conditions(scn).get("exits_discounted", [])))

        sim = scn.get("simulation") or {}
        add(run_duration(scn))
        add((sim.get("evacuation_time") or {}).get("estimated_total_s"))
        delay = response_delay(scn)
        add(delay.get("mean_s"))
        add(delay.get("sd_s"))
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
    routes = evacuation_routes(scn)
    parts = [scn.get("narrative", ""), scn.get("title", ""),
             (scn.get("scenario_objective") or {}).get("purpose", "")]
    parts += scn.get("occupant_distribution", []) or []
    parts += scn.get("assumptions", []) or []
    parts += scn.get("bottlenecks", []) or []
    parts += scn.get("risks", []) or []
    for r in routes.get("routes") or []:
        parts += [r.get("from_area", ""), r.get("via", ""), r.get("to_exit", ""), r.get("note", "")]
    for a in routes.get("restricted_areas") or []:
        parts += [a.get("area", ""), a.get("reason", "")]
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


def evacuation_time_issues(sid, evacuation_time, duration, pre_movement_mean_s):
    """evacuation_time is the AI's own estimate, not a simulation result, so nothing downstream
    grounds it. These are the contradictions that make an estimate unusable on its face: a run that
    ends before the building is clear, and a clearance that beats its own pre-movement time."""
    issues = []
    estimate = evacuation_time.get("estimated_total_s")

    if not isinstance(estimate, (int, float)):
        issues.append({"scenario": sid, "field": "evacuation_time.estimated_total_s",
                       "issue": f"{estimate!r} is not a number of seconds"})
        return issues

    if not in_range(estimate, END_TIME_RANGE_S):
        issues.append({"scenario": sid, "field": "evacuation_time.estimated_total_s",
                       "issue": f"{estimate} s outside {END_TIME_RANGE_S}"})

    if isinstance(duration, (int, float)) and estimate > duration:
        issues.append({"scenario": sid, "field": "evacuation_time.estimated_total_s",
                       "issue": f"the estimate is {estimate} s but the run is only {duration} s "
                                f"long — the simulation would stop before the building is clear, so "
                                f"the run cannot test the estimate"})

    if isinstance(pre_movement_mean_s, (int, float)) and estimate < pre_movement_mean_s:
        issues.append({"scenario": sid, "field": "evacuation_time.estimated_total_s",
                       "issue": f"the estimate is {estimate} s but occupants take a mean "
                                f"{pre_movement_mean_s} s just to start moving — the building "
                                f"cannot clear before its own pre-movement time"})

    if not str(evacuation_time.get("basis") or "").strip():
        issues.append({"scenario": sid, "field": "evacuation_time.basis",
                       "issue": "no arithmetic given — an estimate with no working behind it cannot "
                                "be checked against the run that replaces it"})
    return issues


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
        settings = sim.get("simulation_settings") or {}
        if not str(settings.get("start_conditions") or "").strip():
            issues.append({"scenario": sid, "field": "simulation_settings.start_conditions",
                           "issue": "no starting state given — the run has no t=0 to begin from"})

        duration = run_duration(scn)
        if duration is not None and not in_range(duration, END_TIME_RANGE_S):
            issues.append({"scenario": sid, "field": "simulation_settings.duration.seconds",
                           "issue": f"{duration} s outside {END_TIME_RANGE_S}"})

        issues += evacuation_time_issues(sid, sim.get("evacuation_time") or {}, duration,
                                         response_delay(scn).get("mean_s"))

        delay = response_delay(scn)
        if not in_range(delay.get("mean_s"), PRE_MOVEMENT_RANGE_S):
            issues.append({"scenario": sid, "field": "pre_movement.response_delay.mean_s",
                           "issue": f"{delay.get('mean_s')} s outside {PRE_MOVEMENT_RANGE_S}"})
        if str(delay.get("distribution", "")).strip().lower() not in ALLOWED_DISTRIBUTIONS:
            issues.append({"scenario": sid, "field": "pre_movement.response_delay.distribution",
                           "issue": f"{delay.get('distribution')!r} is not a recognised distribution"})
        if not str(delay.get("basis") or "").strip():
            issues.append({"scenario": sid, "field": "pre_movement.response_delay.basis",
                           "issue": "no basis given"})

        pre = sim.get("pre_movement") or {}
        for part in ("detection", "alarm", "recognition"):
            if not str(pre.get(part) or "").strip():
                issues.append({"scenario": sid, "field": f"pre_movement.{part}",
                               "issue": "no assumption stated — pre-movement time is measured from "
                                        "detection, so the study cannot say what its clock starts at"})

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


def sleeping_share(obj):
    """Share of the computed occupant load sitting in space where occupants may be asleep."""
    total = sleeping = 0
    for s in obj.get("spaces", []):
        load = s.get("occupant_load") or 0
        total += load
        if s.get("use_type") in SLEEPING_USE_TYPES:
            sleeping += load
    return (sleeping / total) if total else 0.0


def state_of(scenario):
    state = str((scenario.get("occupancy") or {}).get("occupancy_state") or "").lower()
    if any(w in state for w in NIGHT_WORDS):
        return "night"
    if any(w in state for w in DAY_WORDS):
        return "day"
    return None


def occupancy_state_issues(obj):
    """Flag a sleeping-occupancy building whose night state holds fewer people than its day state."""
    share = sleeping_share(obj)
    if share < SLEEPING_SHARE_THRESHOLD:
        return []

    by_state = {"night": [], "day": []}
    for scn in obj.get("scenarios", []):
        state = state_of(scn)
        if state:
            total = scenario_total(scn)
            if isinstance(total, int):
                by_state[state].append((scn.get("id"), total))
    if not by_state["night"] or not by_state["day"]:
        return []

    night_id, night_total = max(by_state["night"], key=lambda r: r[1])
    day_id, day_total = max(by_state["day"], key=lambda r: r[1])
    if night_total >= day_total:
        return []
    return [{"scenario": night_id, "field": "occupants_total",
             "issue": f"night occupancy is {night_total} but daytime occupancy ({day_id}) is "
                      f"{day_total} — inverted for a building where {share:.0%} of the computed "
                      f"occupant load sleeps on site. Residents are home and asleep at night, so the "
                      f"night state should hold at least as many people as the day state; check this "
                      f"scenario's occupancy_multipliers"}]


def multiplier_signature(scenario):
    """The per-use_type multipliers, as a comparable key. Two scenarios sharing a signature seed
    occupants into the same rooms in the same proportions, whatever their exits do."""
    sim = scenario.get("simulation") or {}
    return tuple(sorted((m.get("use_type"), round(float(m.get("multiplier") or 0), 4))
                        for m in (sim.get("occupancy_multipliers") or [])))


def occupancy_variance_issues(obj):
    """A scenario set only earns its keep if the scenarios differ. Flag totals at the computed
    ceiling, repeated totals, and repeated distributions — each of which makes a scenario a
    duplicate run rather than a distinct study input."""
    scenarios = obj.get("scenarios") or []
    if len(scenarios) < 2:
        return []

    issues = []
    ceiling = (obj.get("building") or {}).get("total_occupant_load")

    by_total, by_signature = defaultdict(list), defaultdict(list)
    for scn in scenarios:
        sid = scn.get("id")
        total = scenario_total(scn)
        if isinstance(total, int):
            by_total[total].append(sid)
            if ceiling and total >= ceiling:
                issues.append({"scenario": sid, "field": "occupants_total",
                               "issue": f"occupants_total {total} is the building's whole computed "
                                        f"load ({ceiling}) — that is a capacity ceiling, not an "
                                        f"occupancy state; no evacuation happens with every room at "
                                        f"its code capacity at once"})
        by_signature[multiplier_signature(scn)].append(sid)

    for total, ids in sorted(by_total.items()):
        if len(ids) > 1:
            issues.append({"scenario": ", ".join(str(i) for i in ids), "field": "occupants_total",
                           "issue": f"{len(ids)} scenarios all evacuate {total} occupant(s) — vary the "
                                    f"occupant load across the set so the scenarios test different "
                                    f"populations"})

    for signature, ids in by_signature.items():
        if len(ids) > 1:
            where = "no occupancy_multipliers at all" if not signature else "identical " \
                    "occupancy_multipliers"
            issues.append({"scenario": ", ".join(str(i) for i in ids),
                           "field": "simulation.occupancy_multipliers",
                           "issue": f"{len(ids)} scenarios have {where}, so they seed occupants into "
                                    f"the same rooms in the same proportions — the occupant "
                                    f"distribution does not vary across the set"})
    return issues


def fire_condition_issues(obj):
    """An exit is discounted because something makes it unusable, and fire_conditions is where the
    scenario says what. Flag a missing block, and a fire that disagrees with the exits it closed."""
    issues = []
    for scn in obj.get("scenarios", []):
        sid = scn.get("id")
        fire = scn.get("fire_conditions")
        if not fire:
            issues.append({"scenario": sid, "field": "fire_conditions",
                           "issue": "no fire-related conditions given — the study has no fire origin, "
                                    "detection assumption or smoke condition to run against"})
            continue

        discounted = set(scenario_conditions(scn).get("exits_discounted") or [])
        affected = set(fire.get("affected_exits") or [])
        if discounted != affected:
            unexplained = sorted(discounted - affected)
            unclosed = sorted(affected - discounted)
            detail = []
            if unexplained:
                detail.append(f"{', '.join(unexplained)} discounted but not attributed to the fire")
            if unclosed:
                detail.append(f"{', '.join(unclosed)} hit by the fire but still counted as available")
            issues.append({"scenario": sid, "field": "fire_conditions.affected_exits",
                           "issue": "fire conditions and discounted exits disagree — " +
                                    "; ".join(detail)})
    return issues


def placement_issues(obj):
    from core_backend.occupant_placement import scenario_occupancy

    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    issues = []
    for scn in obj.get("scenarios", []):
        sid = scn.get("id")
        target = scenario_total(scn) or 0
        allocation, unplaced, unallocated = scenario_occupancy(obj, scn)
        placed, missing = sum(allocation.values()), sum(unplaced.values())

        # the seed block carries only the total and the state; placed_total appears once the full
        # block has been attached, and only then is there anything to compare against.
        stored = scn.get("occupancy") or {}
        if "placed_total" in stored and stored.get("placed_total") != placed:
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
    direction_issues = occupancy_state_issues(obj)
    variance_issues = occupancy_variance_issues(obj)
    fire_issues = fire_condition_issues(obj)

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
            "night_occupancy_not_below_day": not direction_issues,
            "occupancy_varies_across_scenarios": not variance_issues,
            "fire_conditions_match_discounted_exits": not fire_issues,
        },
        "unknown_exit_references": unknown_exits,
        "exit_capacity_persons": round(capacity),
        "number_factcheck": "passed" if not ungrounded else "review",
        "ungrounded_numbers": ungrounded,
        "simulation_parameter_issues": sim_issues,
        "placement_issues": place_issues,
        "occupancy_state_issues": direction_issues + variance_issues,
        "fire_condition_issues": fire_issues,
        "not_assessed_count": len(obj.get("not_assessed", [])),
    }
    return obj


if __name__ == "__main__":

    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = json.load(f)
    else:
        from core_backend.scenario_generation_llm_1 import build_full_scenario
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
