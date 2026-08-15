#It will tell where all the people can be placed in the entire building since the residential building can only give the layout for it.
import json
import sys
from core_backend.validation import validate
from core_backend.sample_paths import resolve_ifc
from core_backend.exit_names import discounted_exit_ids, exit_names, named

POSITION_NOTE = ("all occupants of a room share the room centroid as their seed point — enable the "
                 "simulator's 'randomize occupant positions' so they scatter within the room")

ALLOCATION_METHOD = ("computed per-room occupant load, scaled by the scenario's per-use_type "
                     "multipliers, then largest-remainder rounded onto occupants_total. "
                     "placed_total + unplaced_total + unallocated_total = occupants_total: no room is "
                     "ever given more people than its own computed load allows, so occupants that do "
                     "not fit are reported rather than seated somewhere they would not be")

REROUTE_REASON = ("their computed nearest exit is unavailable in this scenario, so their occupants are "
                  "given the generic nearest-available-exit goal rather than a closed door")

def scenario_weights(obj, scenario):
    sim = scenario.get("simulation") or {}
    multipliers = {m["use_type"]: m["multiplier"] for m in sim.get("occupancy_multipliers") or []}
    weights = {}
    for s in obj.get("spaces", []):
        base = s.get("occupant_load") or 0
        if base <= 0:
            continue
        weight = base * multipliers.get(s.get("use_type"), 1.0)
        if weight > 0:
            weights[s["guid"]] = weight
    return weights

def scenario_occupancy(obj, scenario):
    target = (scenario.get("occupancy") or {}).get("occupants_total") or 0
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}

    weights = scenario_weights(obj, scenario)
    capacity = sum(weights.values())
    if not weights or capacity <= 0 or target <= 0:
        return {}, {}, max(target, 0)

    placeable = min(target, int(capacity))

    exact = {guid: placeable * w / capacity for guid, w in weights.items()}
    counts = {guid: int(value) for guid, value in exact.items()}
    leftover = placeable - sum(counts.values())
    ranked = sorted(exact, key=lambda g: (-(exact[g] - counts[g]), g))
    for guid in ranked[:leftover]:
        counts[guid] += 1

    placed, unplaced = {}, {}
    for guid, n in counts.items():
        if n <= 0:
            continue
        space = space_by_guid[guid]
        seedable = space.get("reachable") and space.get("centroid")
        (placed if seedable else unplaced)[guid] = n
    return placed, unplaced, target - placeable

def allocate_occupants(obj, scenario):
    return scenario_occupancy(obj, scenario)[0]

def profile_sequence(profiles, total):
    if not profiles:
        return ["default"] * total
    fractions = {p["name"]: float(p.get("fraction") or 0) for p in profiles}

    exact = {name: total * f for name, f in fractions.items()}
    counts = {name: int(value) for name, value in exact.items()}
    leftover = total - sum(counts.values())
    for name in sorted(exact, key=lambda n: (-(exact[n] - counts[n]), n))[:leftover]:
        counts[name] += 1

    sequence, emitted = [], {name: 0 for name in counts}
    for i in range(total):
        available = [n for n in counts if emitted[n] < counts[n]]
        if not available:
            break
        name = max(available, key=lambda n: (fractions[n] * (i + 1) - emitted[n], n))
        sequence.append(name)
        emitted[name] += 1
    return sequence


NEAREST_AVAILABLE = "goto_nearest_available_exit"


def _goal(space, discounted):
    exit_id = space.get("nearest_exit")
    if exit_id and exit_id not in discounted:
        return f"goto_{exit_id}"
    return NEAREST_AVAILABLE


def rerouted_rooms(obj, scenario):
    discounted = set(discounted_exit_ids(scenario))
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    return sorted(guid for guid in allocate_occupants(obj, scenario)
                  if _goal(space_by_guid[guid], discounted) == NEAREST_AVAILABLE)

def why_unplaced(space, multipliers):
    if not space.get("reachable"):
        return "no egress path to a final exit was found — see not_assessed"
    if not space.get("centroid"):
        return "no centroid in the IFC geometry, so occupants cannot be seeded"
    if multipliers.get(space.get("use_type")) == 0:
        return f"emptied by this scenario's occupancy state ({space.get('use_type')} multiplier 0)"
    return "rounded to zero occupants by the allocation"

def occupancy_block(obj, scenario):
    allocation, unplaced_counts, unallocated = scenario_occupancy(obj, scenario)
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    # the scenario's own total and state — stated here and nowhere else in the scenario
    seeded = scenario.get("occupancy") or {}
    sim = scenario.get("simulation") or {}
    discounted = set(discounted_exit_ids(scenario))
    names = exit_names(obj.get("exits", []))
    multipliers = {m["use_type"]: m["multiplier"] for m in sim.get("occupancy_multipliers") or []}
    capacity = int(sum(scenario_weights(obj, scenario).values()))

    sequence = profile_sequence(sim.get("profiles") or [], sum(allocation.values()))

    by_room, taken, rerouted = [], 0, []
    for guid in sorted(allocation):
        space = space_by_guid[guid]
        n = allocation[guid]
        mix = {}
        for name in sequence[taken:taken + n]:
            mix[name] = mix.get(name, 0) + 1
        taken += n
        goal = _goal(space, discounted)
        if goal == NEAREST_AVAILABLE:
            rerouted.append(guid)
        by_room.append({
            "guid": guid,
            "name": space.get("name"),
            "storey": space.get("storey"),
            "use_type": space.get("use_type"),
            "computed_occupant_load": space.get("occupant_load"),
            "occupants": n,
            "seed_point": space.get("centroid"),
            "profiles": dict(sorted(mix.items())),
            "goal": goal,
            "goal_exit": ("nearest available exit" if goal == NEAREST_AVAILABLE
                          else named(space.get("nearest_exit"), names)),
        })

    unplaced_rooms = [{
        "guid": guid,
        "name": space_by_guid[guid].get("name"),
        "storey": space_by_guid[guid].get("storey"),
        "use_type": space_by_guid[guid].get("use_type"),
        "computed_occupant_load": space_by_guid[guid].get("occupant_load"),
        "occupants_not_placed": n,
        "why": why_unplaced(space_by_guid[guid], multipliers),
    } for guid, n in sorted(unplaced_counts.items())]

    return {
        "occupants_total": seeded.get("occupants_total"),
        "occupancy_state": seeded.get("occupancy_state"),
        "placed_total": sum(allocation.values()),
        "unplaced_total": sum(unplaced_counts.values()),
        "unallocated_total": unallocated,
        "unallocated_why": (
            f"this scenario asks for {seeded.get('occupants_total')} occupants, but its own "
            f"occupancy multipliers leave rooms holding only {capacity}. The shortfall is reported "
            f"rather than scaled into rooms that cannot hold it — review occupants_total against the "
            f"multipliers before running the study." if unallocated else None),
        "scenario_room_capacity": capacity,
        "building_computed_total": (obj.get("building") or {}).get("total_occupant_load"),
        "allocation_method": ALLOCATION_METHOD,
        "position_note": POSITION_NOTE,
        "by_room": by_room,
        "unplaced_rooms": unplaced_rooms,
        "rerouted_rooms": {"count": len(rerouted), "guids": rerouted, "why": REROUTE_REASON},
    }

def attach_occupancy(obj):
    for scenario in obj.get("scenarios", []):
        scenario["occupancy"] = occupancy_block(obj, scenario)
    return obj

if __name__ == "__main__":
    
    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = validate(json.load(f))
    
    else:
        from core_backend.scenario_generation_llm_1 import build_full_scenario
        args = [a for a in sys.argv if not a.startswith("--")]
        obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))

    for scenario in obj["scenarios"]:
        occ = scenario.get("occupancy") or occupancy_block(obj, scenario)
        missing = occ["unplaced_total"]
        print(f"\n{scenario['id']}: placed {occ['placed_total']}/{occ['occupants_total']} occupants "
              f"across {len(occ['by_room'])} rooms"
              + (f" ({missing} unplaced in {len(occ['unplaced_rooms'])} sealed room(s))"
                 if missing else "")
              + (f" ({occ['unallocated_total']} unallocated — rooms hold only "
                 f"{occ['scenario_room_capacity']})" if occ["unallocated_total"] else "")
              + f" | rerouted rooms: {occ['rerouted_rooms']['count']}")
        for room in occ["by_room"]:
            print(f"    {room['guid']}  {room['occupants']:>3} occ  {room['profiles']}  "
                  f"-> {room['goal']}  ({room['storey']})")
