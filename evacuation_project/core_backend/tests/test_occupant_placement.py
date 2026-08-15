"""Occupant placement: how many people start in each room, with what profile and aiming where
(no API / no IFC).

A synthetic two-storey object with a hand-checkable allocation:

    Ground:  Flat A (dwelling, 4 occ)  Shop (commercial, 3 occ)  Stair (0 occ)
    First:   Flat B (dwelling, 5 occ)  Unreachable store (2 occ, no path)

    exits E1 (open) and E2 (discounted in the degraded scenario)
"""

from core_backend.occupant_placement import (allocate_occupants, attach_occupancy, occupancy_block,
                                             rerouted_rooms, scenario_occupancy)


def _simulation(multipliers=None):
    return {
        "movement_model": "steering",
        "simulation_settings": {
            "start_conditions": "residents asleep in their flats, both exits open",
            "duration": {"seconds": 900.0, "basis": "long enough to clear the building"},
        },
        "pre_movement": {
            "detection": "smoke detection in the common parts",
            "alarm": "single-stage alarm, sounders throughout",
            "recognition": "sleeping residents take longer to read the alarm as real",
            "response_delay": {
                "distribution": "lognormal", "mean_s": 120.0, "sd_s": 60.0,
                "basis": "sleeping residential occupancy, alarm assumed audible in flats"},
        },
        "profiles": [
            {"name": "adult", "fraction": 0.8, "speed_distribution": "normal",
             "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
             "basis": "able adults on the level"},
            {"name": "reduced mobility", "fraction": 0.2, "speed_distribution": "normal",
             "speed_ms_mean": 0.8, "speed_ms_sd": 0.2, "shoulder_width_m": 0.5,
             "basis": "assumed share of slower occupants in a residential block"},
        ],
        "occupancy_multipliers": multipliers or [],
        "evacuation_time": {"estimated_total_s": 400.0,
                            "basis": "120 s pre-movement plus travel over the 22.0 m route"},
    }


def _obj():
    return {
        "schema_version": "1.0",
        "model": {"source_ifc": "t.ifc", "units": "m", "coordinate_system": "ifc_world_coordinates"},
        "provenance": {"generated_by_model": "test", "distance_method": "geodesic", "llm_grounded": True},
        "building": {"project": "T", "storeys": 2, "total_floor_area_m2": 300.0,
                     "total_occupant_load": 14},
        "exits": [
            {"id": "E1", "name": "Front", "type": "final_exit", "width_m": 1.0,
             "position": [0.0, 0.0, 0.0]},
            {"id": "E2", "name": "Rear", "type": "final_exit", "width_m": 0.9,
             "position": [20.0, 0.0, 0.0]},
        ],
        "doors": [{"id": "D1", "name": "Flat A door", "type": "internal_door", "width_m": 0.8,
                   "position": [4.0, 1.5, 0.0], "connects": ["FLAT_A", "STAIR"]}],
        "circulation": [{"id": "ST1", "name": "Main stair", "type": "internal_stair", "width_m": 1.2,
                         "position": [10.0, 1.0, 0.0], "rise_m": 3.0, "going_m": 4.5, "slope_m": 5.4,
                         "connects_storeys": ["Ground", "First"]}],
        "stair_links": [{"space_a": "STAIR", "space_b": "STAIR_1",
                         "storey_a": "Ground", "storey_b": "First"}],
        "elevators": [{"id": "LIFT1", "name": "Lift", "type": "elevator", "is_evac_lift": False,
                       "position": [12.0, 1.0, 0.0]}],
        "spaces": [
            {"guid": "FLAT_A", "name": "Flat A", "use_type": "dwelling", "storey": "Ground",
             "area_m2": 60.0, "centroid": [2.0, 1.5, 0.0], "occupant_load": 4,
             "nearest_exit": "E1", "travel_distance_m": 12.0, "most_remote_point": [0.5, 0.5],
             "travel_distance_method": "geodesic_grid", "reachable": True},
            {"guid": "SHOP", "name": "Shop", "use_type": "commercial", "storey": "Ground",
             "area_m2": 40.0, "centroid": [15.0, 1.5, 0.0], "occupant_load": 3,
             "nearest_exit": "E2", "travel_distance_m": 8.0, "most_remote_point": [16.0, 0.5],
             "travel_distance_method": "geodesic_grid", "reachable": True},
            {"guid": "STAIR", "name": "Stair", "use_type": "stair", "storey": "Ground",
             "area_m2": 10.0, "centroid": [10.0, 1.5, 0.0], "occupant_load": 0,
             "nearest_exit": "E1", "travel_distance_m": 6.0, "most_remote_point": [10.0, 2.0],
             "travel_distance_method": "geodesic_grid", "reachable": True},
            {"guid": "FLAT_B", "name": "Flat B", "use_type": "dwelling", "storey": "First",
             "area_m2": 70.0, "centroid": [2.0, 1.5, 3.0], "occupant_load": 5,
             "nearest_exit": "E1", "travel_distance_m": 22.0, "most_remote_point": [0.5, 0.5],
             "travel_distance_method": "geodesic_grid", "reachable": True},
            # occupiable but with no egress path: must not swallow occupants
            {"guid": "STORE", "name": "Sealed store", "use_type": "storage", "storey": "First",
             "area_m2": 60.0, "centroid": [18.0, 1.5, 3.0], "occupant_load": 2,
             "nearest_exit": None, "travel_distance_m": None, "most_remote_point": None,
             "travel_distance_method": "geodesic_grid", "reachable": False},
        ],
        "scenarios": [
            {"id": "SCN-BASE", "type": "base_case", "title": "Base case",
             "scenario_objective": {"purpose": "undegraded comparison",
                                    "conditions": {"exits_discounted": []}},
             "evacuation_routes": {"exits_available": ["E1", "E2"], "routes": [],
                                   "restricted_areas": []},
             "occupancy": {"occupants_total": 12, "occupancy_state": "peak occupancy"},
             "assumptions": [], "occupant_distribution": [], "bottlenecks": [],
             "risks": [], "narrative": "All leave.", "simulation": _simulation(),
             "regulatory_justification": "ENG-R11", "ai_explanation": "baseline"},
            {"id": "SCN-NIGHT", "type": "night_one_exit_discounted", "title": "Night, rear exit lost",
             "scenario_objective": {"purpose": "loss of the rear exit at night",
                                    "conditions": {"exits_discounted": ["E2"]}},
             "evacuation_routes": {"exits_available": ["E1"], "routes": [],
                                   "restricted_areas": []},
             "occupancy": {"occupants_total": 9, "occupancy_state": "night"},
             "assumptions": [], "occupant_distribution": [], "bottlenecks": [],
             "risks": [], "narrative": "Shop is closed.",
             "simulation": _simulation([{"use_type": "commercial", "multiplier": 0.0,
                                         "reason": "shop shut at night"},
                                        {"use_type": "dwelling", "multiplier": 1.0,
                                         "reason": "residents at home"}]),
             "regulatory_justification": "ENG-R12", "ai_explanation": "degraded"},
        ],
        "regulation_check": {"jurisdiction": "england", "passed": True, "violations": [],
                             "manual_review": [], "basis": "test"},
        "validation": {},
        "not_assessed": [{"element": "STORE", "name": "Sealed store",
                          "missing": "no egress path to a ground-level final exit was found",
                          "action": "flagged, not silently passed"}],
    }


def _scenario(obj, sid):
    return next(s for s in obj["scenarios"] if s["id"] == sid)


# ---- occupant allocation ---------------------------------------------------------------------------

def test_allocation_conserves_the_scenario_total():
    """placed + unplaced + unallocated == occupants_total: nobody is invented, nobody disappears."""
    obj = _obj()
    for scn in obj["scenarios"]:
        placed, unplaced, unallocated = scenario_occupancy(obj, scn)
        assert (sum(placed.values()) + sum(unplaced.values()) + unallocated
                == scn["occupancy"]["occupants_total"])


def test_unreachable_room_is_never_seeded():
    obj = _obj()
    for scn in obj["scenarios"]:
        assert "STORE" not in allocate_occupants(obj, scn)


def test_unreachable_occupants_are_reported_not_redistributed():
    """The regression that matters: a sealed room's occupants must not be poured into the rooms that
    can escape — that would hand the simulation rooms holding far more people than they hold."""
    obj = _obj()
    placed, unplaced, _ = scenario_occupancy(obj, _scenario(obj, "SCN-BASE"))

    assert unplaced == {"STORE": 2}
    assert sum(placed.values()) == 10           # 12 asked for, 2 of them sealed in STORE
    # every seeded room stays at or under its own computed load (the total was scaled down, not up)
    loads = {s["guid"]: s["occupant_load"] for s in obj["spaces"]}
    assert all(n <= loads[guid] for guid, n in placed.items())


def test_multipliers_empty_the_shop_at_night():
    obj = _obj()
    night = allocate_occupants(obj, _scenario(obj, "SCN-NIGHT"))
    assert "SHOP" not in night                      # commercial multiplier 0.0
    assert set(night) == {"FLAT_A", "FLAT_B"}       # only the dwellings are seeded


def test_largest_remainder_handles_a_total_that_does_not_divide_evenly():
    # weights 4 : 3 : 5 : 2 over a total of 12 does not divide evenly -> remainders decide the split
    obj = _obj()
    placed, unplaced, unallocated = scenario_occupancy(obj, _scenario(obj, "SCN-BASE"))
    assert all(isinstance(n, int) and n > 0 for n in {**placed, **unplaced}.values())
    assert set(placed) == {"FLAT_A", "SHOP", "FLAT_B"}
    assert unallocated == 0                     # 12 asked for, 14 of room capacity


def test_no_room_is_ever_given_more_people_than_it_can_hold():
    """The scenario total is the AI's; the room capacities are computed. When the first exceeds the
    second, scaling every room up to meet it seats people in rooms that do not hold them — on the real
    model a night scenario asking for 50 against a capacity of 35 put 4 people in a 3-person sauna."""
    obj = _obj()
    scn = _scenario(obj, "SCN-NIGHT")
    # at night the dwellings (4 + 5) and the sealed store (2) are all that stay occupied: capacity 11
    scn["occupancy"]["occupants_total"] = 20

    placed, unplaced, unallocated = scenario_occupancy(obj, scn)
    loads = {s["guid"]: s["occupant_load"] for s in obj["spaces"]}
    assert all(n <= loads[guid] for guid, n in {**placed, **unplaced}.items())
    assert sum(placed.values()) + sum(unplaced.values()) + unallocated == 20
    assert unallocated == 9                     # 20 asked for, 11 of room capacity

    block = occupancy_block(obj, scn)
    assert block["unallocated_total"] == 9
    assert block["scenario_room_capacity"] == 11
    assert "cannot hold it" in block["unallocated_why"]


def test_nothing_is_unallocated_when_the_total_fits():
    obj = _obj()
    for scn in obj["scenarios"]:
        block = occupancy_block(obj, scn)
        assert block["unallocated_total"] == 0
        assert block["unallocated_why"] is None


# ---- the per-room occupancy block ------------------------------------------------------------------

def test_every_seeded_room_has_a_seed_point_and_a_real_goal():
    obj = _obj()
    scn = _scenario(obj, "SCN-BASE")
    block = occupancy_block(obj, scn)
    centroids = {s["guid"]: s["centroid"] for s in obj["spaces"]}
    exit_ids = {e["id"] for e in obj["exits"]}

    assert block["by_room"]
    assert sum(r["occupants"] for r in block["by_room"]) == block["placed_total"]
    for room in block["by_room"]:
        assert room["seed_point"] == centroids[room["guid"]]
        assert len(room["seed_point"]) == 3
        assert room["goal"].removeprefix("goto_") in exit_ids
        assert room["occupants"] <= room["computed_occupant_load"]


def test_per_room_profile_counts_sum_to_that_rooms_occupants():
    obj = _obj()
    block = occupancy_block(obj, _scenario(obj, "SCN-BASE"))
    for room in block["by_room"]:
        assert sum(room["profiles"].values()) == room["occupants"]


def test_profile_mix_matches_the_requested_fractions():
    obj = _obj()
    block = occupancy_block(obj, _scenario(obj, "SCN-BASE"))
    totals = {}
    for room in block["by_room"]:
        for name, n in room["profiles"].items():
            totals[name] = totals.get(name, 0) + n

    assert set(totals) == {"adult", "reduced mobility"}
    # 80% of the 10 placed occupants = 8; allow a one-occupant rounding tolerance
    assert abs(totals["adult"] - 0.8 * block["placed_total"]) <= 1


def test_profile_mix_is_spread_across_rooms_not_clustered():
    """The sequence is consumed room by room, so a grouped one would park every slower occupant in the
    last room and skew the egress time."""
    obj = _obj()
    block = occupancy_block(obj, _scenario(obj, "SCN-BASE"))
    rooms_with_slow = [r["guid"] for r in block["by_room"] if r["profiles"].get("reduced mobility")]
    assert len(rooms_with_slow) > 1


def test_occupants_are_never_sent_to_a_discounted_exit():
    """The regression that matters most: nearest_exit is computed against the base-case graph, so a
    scenario that closes that exit would otherwise march everyone into a locked door."""
    obj = _obj()
    scn = _scenario(obj, "SCN-NIGHT")            # discounts E2
    block = occupancy_block(obj, scn)

    assert block["by_room"]                       # the scenario does place people
    assert not any(r["goal"] == "goto_E2" for r in block["by_room"])
    # SHOP was the only room aiming at E2 and the night multiplier empties it, so nobody reroutes here
    assert all(r["goal"] == "goto_E1" for r in block["by_room"])
    assert block["rerouted_rooms"]["count"] == 0


def test_a_room_aiming_at_a_closed_exit_falls_back_to_the_generic_goal():
    obj = _obj()
    scn = _scenario(obj, "SCN-NIGHT")
    scn["simulation"]["occupancy_multipliers"] = []   # keep the shop open so it must reroute off E2

    block = occupancy_block(obj, scn)
    shop = next(r for r in block["by_room"] if r["guid"] == "SHOP")
    assert shop["goal"] == "goto_nearest_available_exit"
    assert block["rerouted_rooms"] == {"count": 1, "guids": ["SHOP"],
                                       "why": block["rerouted_rooms"]["why"]}
    assert rerouted_rooms(obj, scn) == ["SHOP"]


def test_the_block_reports_the_unplaced_room_and_says_why():
    obj = _obj()
    block = occupancy_block(obj, _scenario(obj, "SCN-BASE"))

    assert (block["placed_total"] + block["unplaced_total"] + block["unallocated_total"]
            == block["occupants_total"])
    assert block["unplaced_total"] == 2
    unplaced = {u["guid"]: u for u in block["unplaced_rooms"]}
    assert set(unplaced) == {"STORE"}
    assert unplaced["STORE"]["occupants_not_placed"] == 2
    assert "no egress path" in unplaced["STORE"]["why"]


def test_a_room_emptied_by_a_multiplier_is_not_reported_as_unreachable():
    obj = _obj()
    block = occupancy_block(obj, _scenario(obj, "SCN-NIGHT"))
    # SHOP is zeroed by the commercial multiplier, so it never reaches unplaced_rooms at all
    assert "SHOP" not in {u["guid"] for u in block["unplaced_rooms"]}
    assert "SHOP" not in {r["guid"] for r in block["by_room"]}


def test_attach_occupancy_reaches_every_scenario():
    obj = attach_occupancy(_obj())
    for scn in obj["scenarios"]:
        block = scn["occupancy"]
        assert (block["placed_total"] + block["unplaced_total"] + block["unallocated_total"]
                == scn["occupancy"]["occupants_total"])
        assert block["allocation_method"] and block["position_note"]
