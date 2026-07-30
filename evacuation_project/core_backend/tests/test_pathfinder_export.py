"""The Pathfinder bundle: occupant placement, door states and components (no API / no IFC).

A synthetic two-storey object with a hand-checkable allocation:

    Ground:  Flat A (dwelling, 4 occ)  Shop (commercial, 3 occ)  Stair (0 occ)
    First:   Flat B (dwelling, 5 occ)  Unreachable store (2 occ, no path)

    exits E1 (open) and E2 (discounted in the degraded scenario)
"""

import json

from core_backend.pathfinder_export import (allocate_occupants, component_rows, export_bundle,
                                            occupant_rows, rerouted_rooms, scenario_occupancy,
                                            setup_json)


def _simulation(multipliers=None):
    return {
        "movement_model": "steering",
        "end_time_s": 900.0,
        "pre_movement": {"distribution": "lognormal", "mean_s": 120.0, "sd_s": 60.0,
                         "basis": "sleeping residential occupancy, alarm assumed audible in flats"},
        "profiles": [
            {"name": "adult", "fraction": 0.8, "speed_distribution": "normal",
             "speed_ms_mean": 1.19, "speed_ms_sd": 0.24, "shoulder_width_m": 0.46,
             "basis": "able adults on the level"},
            {"name": "reduced mobility", "fraction": 0.2, "speed_distribution": "normal",
             "speed_ms_mean": 0.8, "speed_ms_sd": 0.2, "shoulder_width_m": 0.5,
             "basis": "assumed share of slower occupants in a residential block"},
        ],
        "occupancy_multipliers": multipliers or [],
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
             "conditions": {"exits_available": ["E1", "E2"], "exits_discounted": [],
                            "occupancy_state": "peak occupancy", "occupants_total": 12},
             "assumptions": [], "occupant_distribution": [], "routes": [], "bottlenecks": [],
             "risks": [], "narrative": "All leave.", "simulation": _simulation(),
             "regulatory_justification": "ENG-R11", "ai_explanation": "baseline"},
            {"id": "SCN-NIGHT", "type": "night_one_exit_discounted", "title": "Night, rear exit lost",
             "conditions": {"exits_available": ["E1"], "exits_discounted": ["E2"],
                            "occupancy_state": "night", "occupants_total": 9},
             "assumptions": [], "occupant_distribution": [], "routes": [], "bottlenecks": [],
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
    """placed + unplaced == occupants_total: nobody is invented and nobody quietly disappears."""
    obj = _obj()
    for scn in obj["scenarios"]:
        placed, unplaced = scenario_occupancy(obj, scn)
        assert sum(placed.values()) + sum(unplaced.values()) == scn["conditions"]["occupants_total"]


def test_unreachable_room_is_never_seeded():
    obj = _obj()
    for scn in obj["scenarios"]:
        assert "STORE" not in allocate_occupants(obj, scn)


def test_unreachable_occupants_are_reported_not_redistributed():
    """The regression that matters: a sealed room's occupants must not be poured into the rooms that
    can escape — that would hand the simulation rooms holding far more people than they hold."""
    obj = _obj()
    placed, unplaced = scenario_occupancy(obj, _scenario(obj, "SCN-BASE"))

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
    placed, unplaced = scenario_occupancy(obj, _scenario(obj, "SCN-BASE"))
    assert all(isinstance(n, int) and n > 0 for n in {**placed, **unplaced}.values())
    assert set(placed) == {"FLAT_A", "SHOP", "FLAT_B"}


# ---- occupant rows ---------------------------------------------------------------------------------

def test_one_row_per_occupant_with_coordinates_and_a_real_goal():
    obj = _obj()
    scn = _scenario(obj, "SCN-BASE")
    rows = occupant_rows(obj, scn)
    exit_ids = {e["id"] for e in obj["exits"]}

    assert len(rows) == sum(allocate_occupants(obj, scn).values())
    for r in rows:
        assert all(isinstance(r[axis], float) for axis in ("x", "y", "z"))
        assert r["behavior"].startswith("goto_")
        assert r["behavior"].removeprefix("goto_") in exit_ids
        assert r["pre_movement_s"] == 120.0
    assert len({r["name"] for r in rows}) == len(rows)      # names are unique


def test_profile_mix_matches_the_requested_fractions():
    obj = _obj()
    rows = occupant_rows(obj, _scenario(obj, "SCN-BASE"))
    adults = sum(1 for r in rows if r["profile"] == "adult")
    # 80% of 12 = 9.6 -> largest remainder gives 10; allow a one-occupant tolerance either way
    assert abs(adults - 0.8 * len(rows)) <= 1
    assert {r["profile"] for r in rows} == {"adult", "reduced mobility"}


def test_occupants_are_never_sent_to_a_discounted_exit():
    """The regression that matters most: nearest_exit is computed against the base-case graph, so a
    scenario that closes that exit would otherwise march everyone into a locked door."""
    obj = _obj()
    scn = _scenario(obj, "SCN-NIGHT")            # discounts E2
    rows = occupant_rows(obj, scn)

    assert rows                                   # the scenario does place people
    assert not any(r["behavior"] == "goto_E2" for r in rows)
    # SHOP was the only room aiming at E2 and the night multiplier empties it, so nobody reroutes here
    assert all(r["behavior"] == "goto_E1" for r in rows)


def test_a_room_aiming_at_a_closed_exit_falls_back_to_the_generic_goal():
    obj = _obj()
    scn = _scenario(obj, "SCN-NIGHT")
    scn["simulation"]["occupancy_multipliers"] = []   # keep the shop open so it must reroute off E2

    rows = occupant_rows(obj, scn)
    shop_goals = {r["behavior"] for r in rows if r["room_guid"] == "SHOP"}
    assert shop_goals == {"goto_nearest_available_exit"}
    assert "SHOP" in rerouted_rooms(obj, scn)

    setup = setup_json(obj, scn)
    assert setup["rerouted_rooms"]["count"] == 1
    # a closed exit must not be offered as a behavior either
    assert "goto_E2" not in {b["name"] for b in setup["behaviors"]}


def test_profile_mix_is_spread_across_rooms_not_clustered():
    """Rows come out room by room, so a grouped profile sequence would park every slower occupant in
    the last room and skew the egress time."""
    obj = _obj()
    rows = occupant_rows(obj, _scenario(obj, "SCN-BASE"))
    rooms_with_slow = {r["room_guid"] for r in rows if r["profile"] == "reduced mobility"}
    assert len(rooms_with_slow) > 1


# ---- components ------------------------------------------------------------------------------------

def test_discounted_exit_is_closed_and_the_rest_open():
    obj = _obj()
    rows = {r["id"]: r for r in component_rows(obj, _scenario(obj, "SCN-NIGHT"))}
    assert rows["E2"]["state"] == "closed"
    assert rows["E1"]["state"] == "open"
    assert rows["D1"]["state"] == "open"


def test_components_cover_doors_stairs_and_lifts():
    obj = _obj()
    rows = component_rows(obj, _scenario(obj, "SCN-BASE"))
    kinds = {r["id"]: r["kind"] for r in rows}
    assert kinds == {"E1": "final_exit", "E2": "final_exit", "D1": "internal_door",
                     "ST1": "stair", "LIFT1": "elevator"}
    stair = next(r for r in rows if r["id"] == "ST1")
    assert stair["connects"] == "Ground|First"
    door = next(r for r in rows if r["id"] == "D1")
    assert door["connects"] == "FLAT_A|STAIR"


# ---- setup json + bundle ---------------------------------------------------------------------------

def test_setup_json_reports_the_unplaced_room_and_the_benchmark():
    obj = _obj()
    setup = setup_json(obj, _scenario(obj, "SCN-BASE"))
    occupancy = setup["occupancy"]

    assert occupancy["placed_total"] + occupancy["unplaced_total"] == occupancy["occupants_total"]
    assert occupancy["unplaced_total"] == 2
    unplaced = {u["guid"]: u for u in occupancy["unplaced_rooms"]}
    assert set(unplaced) == {"STORE"}
    assert unplaced["STORE"]["occupants_not_placed"] == 2
    assert "no egress path" in unplaced["STORE"]["why"]
    assert setup["not_assessed"]                       # rides along with the bundle

    benchmark = {b["guid"]: b["travel_distance_m"] for b in
                 setup["benchmark_travel_distances"]["by_room"]}
    assert benchmark["FLAT_B"] == 22.0
    assert "STORE" not in benchmark                    # no measured distance to benchmark against
    assert "not a simulation input" in setup["benchmark_travel_distances"]["note"]


def test_bundle_has_three_files_per_scenario_and_valid_json():
    obj = _obj()
    files = export_bundle(obj)
    assert len(files) == 3 * len(obj["scenarios"])
    for scn in obj["scenarios"]:
        sid = scn["id"]
        assert f"{sid}_occupants.csv" in files
        assert f"{sid}_components.csv" in files
        setup = json.loads(files[f"{sid}_setup.json"])
        assert setup["scenario_id"] == sid
        # header + one line per placed occupant
        lines = files[f"{sid}_occupants.csv"].strip().split("\n")
        assert len(lines) == setup["occupancy"]["placed_total"] + 1
