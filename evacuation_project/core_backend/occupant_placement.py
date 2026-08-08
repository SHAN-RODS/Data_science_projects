"""Where a scenario's occupants start, room by room — the one thing an egress simulation needs that
neither the IFC nor the AI's prose supplies.

The simulator imports the **geometry** from the same IFC, so nothing here carries geometry. What it
cannot get from the IFC is this: how many people are in each room, which profile they belong to, and
which exit they are aiming at once this scenario's closures are taken into account. The result is a
plain dict attached to every scenario as ``scenario["occupancy"]``, so it travels inside the single JSON
deliverable rather than in a side file.

The allocation is deterministic: the AI chooses a scenario total and per-use_type multipliers, and this
module spreads them over the rooms by their computed occupant load, largest-remainder rounded so the
split conserves the total exactly. Occupants in rooms with no traced egress path are reported as
unplaced rather than moved into the rooms that can escape — redistributing them would hand the
simulation rooms holding several times the people they actually hold.
"""

from core_backend.exit_names import discounted_exit_ids, exit_names, named

# Occupants of one room all start at its centroid; the simulator should scatter them from there.
POSITION_NOTE = ("all occupants of a room share the room centroid as their seed point — enable the "
                 "simulator's 'randomize occupant positions' so they scatter within the room")

ALLOCATION_METHOD = ("computed per-room occupant load, scaled by the scenario's per-use_type "
                     "multipliers, then largest-remainder rounded onto occupants_total. "
                     "placed_total + unplaced_total + unallocated_total = occupants_total: no room is "
                     "ever given more people than its own computed load allows, so occupants that do "
                     "not fit are reported rather than seated somewhere they would not be")

REROUTE_REASON = ("their computed nearest exit is unavailable in this scenario, so their occupants are "
                  "given the generic nearest-available-exit goal rather than a closed door")


# ---------------------------------------------------------------------------------------------------
# Occupancy: the AI picks the total and the per-use_type multipliers, this places them room by room.
# ---------------------------------------------------------------------------------------------------
def _scenario_weights(obj, scenario):
    """Each room's occupant capacity under this scenario: its computed load scaled by the scenario's
    per-use_type multiplier. Rooms the scenario empties (multiplier 0) drop out entirely."""
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
    """``(placed, unplaced, unallocated)`` — two ``{space_guid: occupants}`` dicts and a count, which
    together account for every one of the scenario's ``occupants_total``.

    1. start from the computed per-room occupant load
    2. scale by the scenario's per-use_type multipliers (a use_type not listed is unchanged)
    3. cap the total at what those scaled rooms can hold
    4. largest-remainder rounding onto that, so nobody is invented or lost

    Two things are deliberately never done, because both amount to inventing a building we do not have:

    * occupants of rooms with no traced egress path (or no centroid to seed from) are **not**
      redistributed into the rooms that can escape — they land in ``unplaced`` instead, the same
      treatment ``not_assessed`` gives them upstream;
    * when the AI's ``occupants_total`` exceeds what its own multipliers leave room for, the rooms
      still in use are **not** scaled up to meet it. Doing so inflates every room by the same ratio:
      on the Nordic model a night scenario asking for 50 against a multiplied capacity of 35 put 4
      people in a 3-person sauna. The shortfall is reported as ``unallocated`` instead.
    """
    target = (scenario.get("conditions") or {}).get("occupants_total") or 0
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}

    weights = _scenario_weights(obj, scenario)
    capacity = sum(weights.values())
    if not weights or capacity <= 0 or target <= 0:
        return {}, {}, max(target, 0)

    placeable = min(target, int(capacity))

    # largest remainder: floor everyone, then hand the leftover people to the biggest fractions
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
    """``{space_guid: occupants}`` for the rooms this scenario can actually seed."""
    return scenario_occupancy(obj, scenario)[0]


def _profile_sequence(profiles, total):
    """A profile name per occupant, allotted by ``fraction`` with largest-remainder rounding, so the
    realised mix matches the requested one exactly rather than being sampled.

    The sequence is *interleaved*, not grouped: it is consumed room by room in guid order, so handing
    out all the slower profiles at the end would park every reduced-mobility occupant in the last rooms
    and distort the result. Each position goes to whichever profile is furthest behind its target share.
    """
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
    """The behaviour name for a room's occupants.

    ``nearest_exit`` is computed once against the base-case egress graph, so in a scenario that
    discounts that very exit it would send everyone to a closed door. When that happens the room falls
    back to the generic 'nearest available exit' goal — the simulator re-routes to whatever is still
    open — and the occupancy block reports how many rooms had to.
    """
    exit_id = space.get("nearest_exit")
    if exit_id and exit_id not in discounted:
        return f"goto_{exit_id}"
    return NEAREST_AVAILABLE


def rerouted_rooms(obj, scenario):
    """Rooms whose computed nearest exit is unavailable in this scenario, so their goal is generic."""
    discounted = set(discounted_exit_ids(scenario))
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    return sorted(guid for guid in allocate_occupants(obj, scenario)
                  if _goal(space_by_guid[guid], discounted) == NEAREST_AVAILABLE)


def _why_unplaced(space, multipliers):
    if not space.get("reachable"):
        return "no egress path to a final exit was found — see not_assessed"
    if not space.get("centroid"):
        return "no centroid in the IFC geometry, so occupants cannot be seeded"
    if multipliers.get(space.get("use_type")) == 0:
        return f"emptied by this scenario's occupancy state ({space.get('use_type')} multiplier 0)"
    return "rounded to zero occupants by the allocation"


def occupancy_block(obj, scenario):
    """The per-room occupant placement for one scenario — counts, seed points, profile mix and goal.

    This is what gets attached to the scenario as ``scenario["occupancy"]`` and carried in the exported
    record. Per room rather than per occupant: a simulator adds N occupants to a selected room, so the
    count plus that room's seed point is the whole instruction, and the profile mix is a tally of the
    interleaved sequence over the room's share of it.
    """
    allocation, unplaced_counts, unallocated = scenario_occupancy(obj, scenario)
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    conditions = scenario.get("conditions") or {}
    sim = scenario.get("simulation") or {}
    discounted = set(discounted_exit_ids(scenario))
    names = exit_names(obj.get("exits", []))
    multipliers = {m["use_type"]: m["multiplier"] for m in sim.get("occupancy_multipliers") or []}
    capacity = int(sum(_scenario_weights(obj, scenario).values()))

    sequence = _profile_sequence(sim.get("profiles") or [], sum(allocation.values()))

    by_room, taken, rerouted = [], 0, []
    for guid in sorted(allocation):
        space = space_by_guid[guid]
        n = allocation[guid]
        mix = {}
        for name in sequence[taken:taken + n]:
            mix[name] = mix.get(name, 0) + 1
        taken += n
        # the goal comes from the COMPUTED nearest exit, not from the model's prose routes
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
            # `goal` keys on the GlobalId a simulator needs; `goal_exit` is the same instruction in
            # words, so the block can be read without looking a GUID up
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
        "why": _why_unplaced(space_by_guid[guid], multipliers),
    } for guid, n in sorted(unplaced_counts.items())]

    return {
        "occupants_total": conditions.get("occupants_total"),
        "occupancy_state": conditions.get("occupancy_state"),
        "placed_total": sum(allocation.values()),
        "unplaced_total": sum(unplaced_counts.values()),
        "unallocated_total": unallocated,
        "unallocated_why": (
            f"this scenario asks for {conditions.get('occupants_total')} occupants, but its own "
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
    """Attach ``occupancy`` to every scenario in the object, in place — called once the spaces and the
    AI-chosen scenarios are both present."""
    for scenario in obj.get("scenarios", []):
        scenario["occupancy"] = occupancy_block(obj, scenario)
    return obj


if __name__ == "__main__":
    import json
    import sys

    from core_backend.scenario_generation_llm import build_full_scenario
    from core_backend.validation import validate
    from core_backend.sample_paths import resolve_ifc

    # a saved .json is re-checked without another generation call
    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = validate(json.load(f))
    else:
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
