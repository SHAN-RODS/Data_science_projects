"""Turn the whole-building scenario object into the input for an egress simulation study (Pathfinder).

The simulator imports the **geometry** from the same IFC, so nothing here carries geometry. What it
cannot get from the IFC is everything this module supplies, keyed on the IFC GlobalIds already in the
object: how many people are in each room and where they start, which exits are open in this scenario,
how fast people walk, and how long they take to react.

One bundle per scenario:

  ``<id>_occupants.csv``   one row PER OCCUPANT — x/y/z in metres, plus profile, behavior and
                           pre-movement time. This is the row shape Pathfinder's occupant CSV import
                           expects (it keys on the location columns).
  ``<id>_components.csv``  every door, stair and elevator with its per-scenario open/closed state.
  ``<id>_setup.json``      the parameters that are not per-row: movement model, profile definitions,
                           pre-movement distribution, the occupancy allocation and its provenance,
                           plus the computed travel distances as a BENCHMARK to check the simulator's
                           navigation mesh against (they are an output of the simulation, not an input).

The occupant allocation is deterministic: the AI chooses a scenario total and per-use_type multipliers,
and this module spreads them over the rooms by their computed occupant load, largest-remainder rounded
so the split conserves the total exactly. Occupants in rooms with no traced egress path are reported as
unplaced rather than moved into the rooms that can escape — redistributing them would hand the
simulation rooms holding several times the people they actually hold.

``export_results.build_records`` is untouched — the six-field record remains the report deliverable;
this is the simulation deliverable.
"""

import csv
import io
import json
import os

# Occupants of one room all start at its centroid; the simulator should scatter them from there.
POSITION_NOTE = ("all occupants of a room share the room centroid as their seed point — enable the "
                 "simulator's 'randomize occupant positions' so they scatter within the room")

BENCHMARK_NOTE = ("computed geodesic travel distance from each room's most remote point (our own "
                  "measurement over the IFC floor geometry). This is a VALIDATION TARGET, not a "
                  "simulation input: compare it against the distance the simulator's navigation mesh "
                  "produces for the same room.")

OCCUPANT_COLUMNS = ["name", "x", "y", "z", "room_guid", "room_name", "storey",
                    "profile", "behavior", "pre_movement_s"]

COMPONENT_COLUMNS = ["id", "ifc_type", "kind", "name", "width_m", "x", "y", "z",
                     "state", "connects", "storey"]


def _csv(columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _xyz(point):
    """A [x, y, z] (or [x, y]) list as three values, padding a missing z with 0.0."""
    if not point:
        return None, None, None
    values = list(point) + [0.0] * (3 - len(point))
    return values[0], values[1], values[2]


# ---------------------------------------------------------------------------------------------------
# Occupancy: the AI picks the total and the per-use_type multipliers, this places them room by room.
# ---------------------------------------------------------------------------------------------------
def scenario_occupancy(obj, scenario):
    """``(placed, unplaced)`` — two ``{space_guid: occupants}`` dicts that together sum to the
    scenario's ``occupants_total``.

    1. start from the computed per-room occupant load
    2. scale by the scenario's per-use_type multipliers (a use_type not listed is unchanged)
    3. largest-remainder rounding onto the scenario total, so nobody is invented or lost

    Rooms with no traced egress path (or no centroid to seed from) land in ``unplaced``. Their
    occupants are deliberately NOT redistributed into the rooms that can escape: doing so would
    quietly inflate the reachable rooms far past their computed load and hand the simulation a
    building it does not have. They are reported instead — the same treatment ``not_assessed`` gives
    them upstream.
    """
    sim = scenario.get("simulation") or {}
    multipliers = {m["use_type"]: m["multiplier"] for m in sim.get("occupancy_multipliers") or []}
    target = (scenario.get("conditions") or {}).get("occupants_total") or 0
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}

    weights = {}
    for s in obj.get("spaces", []):
        base = s.get("occupant_load") or 0
        if base <= 0:
            continue
        weight = base * multipliers.get(s.get("use_type"), 1.0)
        if weight > 0:
            weights[s["guid"]] = weight

    total_weight = sum(weights.values())
    if not weights or total_weight <= 0 or target <= 0:
        return {}, {}

    # largest remainder: floor everyone, then hand the leftover people to the biggest fractions
    exact = {guid: target * w / total_weight for guid, w in weights.items()}
    counts = {guid: int(value) for guid, value in exact.items()}
    leftover = target - sum(counts.values())
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
    return placed, unplaced


def allocate_occupants(obj, scenario):
    """``{space_guid: occupants}`` for the rooms this scenario can actually seed."""
    return scenario_occupancy(obj, scenario)[0]


def _profile_sequence(profiles, total):
    """A profile name per occupant, allotted by ``fraction`` with largest-remainder rounding, so the
    realised mix matches the requested one exactly rather than being sampled.

    The sequence is *interleaved*, not grouped: rows are emitted room by room, so handing out all the
    slower profiles at the end would park every reduced-mobility occupant in the last rooms and
    distort the result. Each position goes to whichever profile is furthest behind its target share.
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
    """The behavior name for a room's occupants.

    ``nearest_exit`` is computed once against the base-case egress graph, so in a scenario that
    discounts that very exit it would send everyone to a closed door. When that happens the room falls
    back to the generic 'nearest available exit' goal — the simulator re-routes to whatever is still
    open — and ``setup_json`` reports how many rooms had to.
    """
    exit_id = space.get("nearest_exit")
    if exit_id and exit_id not in discounted:
        return f"goto_{exit_id}"
    return NEAREST_AVAILABLE


def rerouted_rooms(obj, scenario):
    """Rooms whose computed nearest exit is unavailable in this scenario, so their goal is generic."""
    discounted = set((scenario.get("conditions") or {}).get("exits_discounted") or [])
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    return sorted(guid for guid in allocate_occupants(obj, scenario)
                  if _goal(space_by_guid[guid], discounted) == NEAREST_AVAILABLE)


def occupant_rows(obj, scenario):
    """One row per occupant, ready for the simulator's occupant CSV import."""
    allocation = allocate_occupants(obj, scenario)
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    discounted = set((scenario.get("conditions") or {}).get("exits_discounted") or [])
    sim = scenario.get("simulation") or {}
    pre_movement = (sim.get("pre_movement") or {}).get("mean_s")
    profiles = _profile_sequence(sim.get("profiles") or [], sum(allocation.values()))

    rows, index = [], 0
    for guid in sorted(allocation):
        space = space_by_guid[guid]
        x, y, z = _xyz(space.get("centroid"))
        # the goal comes from the COMPUTED nearest exit, not from the model's prose routes
        goal = _goal(space, discounted)
        for _ in range(allocation[guid]):
            rows.append({
                "name": f"OCC_{index + 1:05d}",
                "x": x, "y": y, "z": z,
                "room_guid": guid,
                "room_name": space.get("name"),
                "storey": space.get("storey"),
                "profile": profiles[index] if index < len(profiles) else "default",
                "behavior": goal,
                "pre_movement_s": pre_movement,
            })
            index += 1
    return rows


# ---------------------------------------------------------------------------------------------------
# Components: doors / stairs / elevators, with the per-scenario door states.
# ---------------------------------------------------------------------------------------------------
def component_rows(obj, scenario):
    discounted = set((scenario.get("conditions") or {}).get("exits_discounted") or [])
    rows = []

    def add(item, ifc_type, kind, connects, width_key="width_m", storey=None):
        x, y, z = _xyz(item.get("position"))
        rows.append({
            "id": item["id"], "ifc_type": ifc_type, "kind": kind, "name": item.get("name"),
            "width_m": item.get(width_key), "x": x, "y": y, "z": z,
            "state": "closed" if item["id"] in discounted else "open",
            "connects": "|".join(connects or []), "storey": storey,
        })

    for e in obj.get("exits", []):
        add(e, "IfcDoor", "final_exit", [])
    for d in obj.get("doors", []):
        add(d, "IfcDoor", "internal_door", d.get("connects"))
    for c in obj.get("circulation", []):
        add(c, "IfcStair", "stair", c.get("connects_storeys"))
    for t in obj.get("elevators", []):
        add(t, "IfcTransportElement",
            "evacuation_elevator" if t.get("is_evac_lift") else "elevator", [])
    return rows


# ---------------------------------------------------------------------------------------------------
# The per-scenario setup object.
# ---------------------------------------------------------------------------------------------------
def setup_json(obj, scenario):
    allocation, unplaced_counts = scenario_occupancy(obj, scenario)
    space_by_guid = {s["guid"]: s for s in obj.get("spaces", [])}
    conditions = scenario.get("conditions") or {}
    sim = scenario.get("simulation") or {}
    rerouted = rerouted_rooms(obj, scenario)

    multipliers = {m["use_type"]: m["multiplier"] for m in sim.get("occupancy_multipliers") or []}

    def why_unplaced(space):
        if not space.get("reachable"):
            return "no egress path to a final exit was found — see not_assessed"
        if not space.get("centroid"):
            return "no centroid in the IFC geometry, so occupants cannot be seeded"
        if multipliers.get(space.get("use_type")) == 0:
            return (f"emptied by this scenario's occupancy state "
                    f"({space.get('use_type')} multiplier 0)")
        return "rounded to zero occupants by the allocation"

    unplaced = [{"guid": guid, "name": space_by_guid[guid].get("name"),
                 "storey": space_by_guid[guid].get("storey"),
                 "use_type": space_by_guid[guid].get("use_type"),
                 "computed_occupant_load": space_by_guid[guid].get("occupant_load"),
                 "occupants_not_placed": n,
                 "why": why_unplaced(space_by_guid[guid])}
                for guid, n in sorted(unplaced_counts.items())]

    return {
        "scenario_id": scenario.get("id"),
        "description": scenario.get("title"),
        "scenario_type": scenario.get("type"),
        "model": obj.get("model", {}),
        "simulation": {
            "movement_model": sim.get("movement_model"),
            "end_time_s": sim.get("end_time_s"),
            "door_flow_note": ("door widths are the IFC values; leave the simulator's boundary-layer "
                               "and specific-flow defaults unless the study states otherwise"),
        },
        "pre_movement": sim.get("pre_movement", {}),
        "profiles": sim.get("profiles", []),
        "behaviors": [
            {"name": f"goto_{e['id']}", "goal": "exit", "exit_id": e["id"], "exit_name": e.get("name")}
            for e in obj.get("exits", []) if e["id"] not in set(conditions.get("exits_discounted") or [])
        ] + [
            {"name": NEAREST_AVAILABLE, "goal": "nearest available exit", "exit_id": None,
             "exit_name": None,
             "note": "used where a room's computed nearest exit is discounted in this scenario"}
        ],
        "rerouted_rooms": {
            "count": len(rerouted),
            "guids": rerouted,
            "why": ("their computed nearest exit is unavailable in this scenario, so their occupants "
                    "are given the generic nearest-available-exit goal rather than a closed door"),
        },
        "doors": {
            "open": [r["id"] for r in component_rows(obj, scenario)
                     if r["state"] == "open" and r["kind"] in ("final_exit", "internal_door")],
            "closed": sorted(set(conditions.get("exits_discounted") or [])),
        },
        "occupancy": {
            "occupants_total": conditions.get("occupants_total"),
            "occupancy_state": conditions.get("occupancy_state"),
            "placed_total": sum(allocation.values()),
            "unplaced_total": sum(unplaced_counts.values()),
            "building_computed_total": (obj.get("building") or {}).get("total_occupant_load"),
            "multipliers": sim.get("occupancy_multipliers", []),
            "allocation_method": ("computed per-room occupant load, scaled by the scenario's "
                                  "per-use_type multipliers, then largest-remainder rounded onto "
                                  "occupants_total. placed_total + unplaced_total = occupants_total: "
                                  "occupants in rooms with no traced egress path are reported, never "
                                  "moved into rooms that can escape"),
            "position_note": POSITION_NOTE,
            "by_room": [
                {"guid": guid, "name": space_by_guid[guid].get("name"),
                 "storey": space_by_guid[guid].get("storey"),
                 "use_type": space_by_guid[guid].get("use_type"),
                 "computed_occupant_load": space_by_guid[guid].get("occupant_load"),
                 "occupants": allocation[guid],
                 "goal_exit": space_by_guid[guid].get("nearest_exit")}
                for guid in sorted(allocation)
            ],
            "unplaced_rooms": unplaced,
        },
        "benchmark_travel_distances": {
            "note": BENCHMARK_NOTE,
            "method": (obj.get("provenance") or {}).get("distance_method"),
            "by_room": [
                {"guid": s["guid"], "storey": s.get("storey"),
                 "travel_distance_m": s.get("travel_distance_m"),
                 "most_remote_point": s.get("most_remote_point"),
                 "nearest_exit": s.get("nearest_exit")}
                for s in obj.get("spaces", []) if s.get("travel_distance_m") is not None
            ],
        },
        "provenance": obj.get("provenance", {}),
        "validation": obj.get("validation", {}),
        # an unreachable or unassessed room silently drops its occupants — this must stay visible
        "not_assessed": obj.get("not_assessed", []),
    }


def export_bundle(obj):
    """``{filename: file contents}`` covering every scenario — the caller writes or zips them."""
    files = {}
    for scenario in obj.get("scenarios", []):
        sid = scenario.get("id") or "SCENARIO"
        files[f"{sid}_occupants.csv"] = _csv(OCCUPANT_COLUMNS, occupant_rows(obj, scenario))
        files[f"{sid}_components.csv"] = _csv(COMPONENT_COLUMNS, component_rows(obj, scenario))
        files[f"{sid}_setup.json"] = json.dumps(setup_json(obj, scenario), indent=2,
                                                ensure_ascii=False)
    return files


def write_bundle(obj, directory):
    os.makedirs(directory, exist_ok=True)
    written = []
    for filename, content in export_bundle(obj).items():
        path = os.path.join(directory, filename)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        written.append(path)
    return written


if __name__ == "__main__":
    import sys
    from core_backend.scenario_generation_llm import build_full_scenario
    from core_backend.validation import validate
    from core_backend.sample_paths import resolve_ifc

    # a saved .json is re-exported without another generation call
    json_args = [a for a in sys.argv[1:] if a.endswith(".json")]
    if json_args:
        with open(json_args[0], "r", encoding="utf-8") as f:
            obj = validate(json.load(f))
    else:
        args = [a for a in sys.argv if not a.startswith("--")]
        obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))

    out_dir = os.path.join(os.environ.get("TEMP", "."), "pathfinder_bundle")
    for path in write_bundle(obj, out_dir):
        print(f"wrote {path}")

    reference = os.path.join(out_dir, "_full_scenario_object.json")
    with open(reference, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"wrote {reference}  (re-export for free with that path)")

    print()
    for scenario in obj["scenarios"]:
        placed, unplaced = scenario_occupancy(obj, scenario)
        target = scenario["conditions"]["occupants_total"]
        components = component_rows(obj, scenario)
        closed = [c["id"] for c in components if c["state"] == "closed"]
        missing = sum(unplaced.values())
        print(f"{scenario['id']}: placed {sum(placed.values())}/{target} occupants across "
              f"{len(placed)} rooms"
              + (f" ({missing} unplaced in {len(unplaced)} sealed room(s))" if missing else "")
              + f" | {len(components)} components ({len(closed)} closed)"
              f" | movement={scenario['simulation']['movement_model']}"
              f" pre-movement={scenario['simulation']['pre_movement']['mean_s']}s")
