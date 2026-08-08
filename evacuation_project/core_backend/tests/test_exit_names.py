"""Exits are referred to by name — "Exit 1" — and never by their IFC GlobalId (no API / no IFC).

The GlobalId is what an egress simulator keys on, so it is never dropped; what changed is that it is
no longer what a reader is shown. These tests pin the round trip: names are assigned from the plan,
the AI writes with them, and they resolve back to the same GlobalIds the record needs.
"""

from core_backend.exit_names import (available_exit_ids, discounted_exit_ids, exit_names,
                                     name_exit_ids, named, resolve_exit_ids,
                                     unknown_exit_references)
from core_backend.export_results import build_records
from core_backend.scenario_generation_llm import (ScenarioContent, _assemble_scenario, _exits_block,
                                                  _facts_block, _spaces_block)


def _exits():
    """Deliberately stored east-to-west, so file order and plan order disagree."""
    return [
        {"id": "3uMBEvBYD33AT9ygIhdZHw", "name": "UO-2:UO-2.10+4x21:1419803", "width_m": 1.41,
         "position": [20.0, 0.0, 0.0]},
        {"id": "0vwVLBnBr9meBshDw$RHGp", "name": "O-1:O-1.10x21:1429543", "width_m": 1.02,
         "position": [0.0, 0.0, 0.0]},
        {"id": "3T$cyq5XDFhgqPE_ZtOXlr", "name": "O-2:O-2.10+4x21:1507966", "width_m": 1.41,
         "position": [10.0, 5.0, 0.0]},
    ]


def test_exits_are_numbered_across_the_plan_not_in_file_order():
    names = exit_names(_exits())
    assert names["0vwVLBnBr9meBshDw$RHGp"] == "Exit 1"
    assert names["3T$cyq5XDFhgqPE_ZtOXlr"] == "Exit 2"
    assert names["3uMBEvBYD33AT9ygIhdZHw"] == "Exit 3"


def test_naming_is_stable_whatever_order_the_exits_arrive_in():
    exits = _exits()
    assert exit_names(exits) == exit_names(list(reversed(exits)))


def test_positionless_exits_still_get_a_name():
    """A fallback exit list carries no position; it must still be nameable, just numbered last."""
    exits = _exits() + [{"id": "NOPOS", "name": "Unplaced door", "width_m": 0.9}]
    assert exit_names(exits)["NOPOS"] == "Exit 4"


def test_names_resolve_back_to_the_globalids_a_simulator_keys_on():
    names = exit_names(_exits())
    assert resolve_exit_ids(["Exit 1", "exit 3"], names) == ["0vwVLBnBr9meBshDw$RHGp",
                                                            "3uMBEvBYD33AT9ygIhdZHw"]


def test_a_globalid_still_resolves_so_older_objects_keep_working():
    names = exit_names(_exits())
    assert resolve_exit_ids(["0vwVLBnBr9meBshDw$RHGp"], names) == ["0vwVLBnBr9meBshDw$RHGp"]


def test_an_unknown_exit_is_kept_rather_than_silently_dropped():
    """A discounted exit that vanished from the list would read as open — the one failure to avoid."""
    names = exit_names(_exits())
    assert resolve_exit_ids(["Exit 99"], names) == ["Exit 99"]
    assert named("Exit 99", names) == "Exit 99"


def test_a_globalid_quoted_in_prose_is_rewritten_as_the_name():
    """Scenarios written before the rename quote GUIDs in their bottlenecks; reading one back must
    not put them in front of the user again."""
    names = exit_names(_exits())
    scenario = {
        "narrative": "Occupants leave by 0vwVLBnBr9meBshDw$RHGp.",
        "bottlenecks": ["Exit 3uMBEvBYD33AT9ygIhdZHw (width: 1.41 m) may congest"],
        "routes": [{"from_area": "Ground", "to_exit": "3T$cyq5XDFhgqPE_ZtOXlr"}],
    }
    cleaned = name_exit_ids(scenario, names)
    assert cleaned["narrative"] == "Occupants leave by Exit 1."
    assert cleaned["bottlenecks"] == ["Exit 3 (width: 1.41 m) may congest"]
    assert cleaned["routes"][0]["to_exit"] == "Exit 2"
    assert scenario["narrative"].endswith("0vwVLBnBr9meBshDw$RHGp.")     # the original is untouched


# ---- what the model is shown, and what comes back ---------------------------------------------------

def _grounded():
    return {
        "spaces": [
            {"guid": "R1", "name": "Flat A", "long_name": None, "use_type": "dwelling",
             "storey": {"name": "Ground"}, "area_m2": 60.0, "centroid": [2.0, 1.5, 0.0],
             "occupant_load": 4, "occupant_basis": "NDSS", "nearest_exit": "0vwVLBnBr9meBshDw$RHGp",
             "travel_distance_m": 12.0, "travel_distance_method": "geodesic_grid",
             "most_remote_point": [0.5, 0.5], "reachable": True, "reachability_note": None},
        ],
        "not_assessed": [],
    }


def test_the_model_is_never_shown_an_exit_globalid():
    """The surest way to keep a GUID out of the AI's prose is to keep it out of the AI's facts."""
    exits = _exits()
    names = exit_names(exits)
    facts = _facts_block(
        {"project": "T", "storeys": 1, "total_floor_area_m2": 60.0, "total_occupant_load": 4},
        _grounded(), exits, [], [{"name": "Ground", "elevation_m": 0.0,
                                  "height_above_ground_m": 0.0}], [],
        [{"exit_discounted": "3uMBEvBYD33AT9ygIhdZHw", "per_storey":
          [{"storey": "Ground", "occupants": 4, "max_travel_distance_m": 18.0, "unreachable": 0}]}],
        names)

    assert not [e["id"] for e in exits if e["id"] in facts]
    assert "Exit 1 (width_m=1.02)" in facts
    assert "If Exit 3 is UNAVAILABLE:" in facts
    assert "exit=Exit 1" in facts


def test_the_object_carries_both_the_name_and_the_id():
    names = exit_names(_exits())
    exits_block = _exits_block(_exits(), names)
    assert [e["exit_name"] for e in exits_block] == ["Exit 1", "Exit 2", "Exit 3"]
    # the IFC's own label is kept so the door is still findable in the model
    assert exits_block[0]["name"] == "O-1:O-1.10x21:1429543"

    space = _spaces_block(_grounded(), [], names)[0]
    assert space["nearest_exit_name"] == "Exit 1"
    assert space["nearest_exit"] == "0vwVLBnBr9meBshDw$RHGp"


def test_the_ai_writes_names_and_assembly_pins_the_ids_to_them():
    """The round trip in one step: what the model returns, and what the object ends up carrying."""
    names = exit_names(_exits())
    content = ScenarioContent(
        type="one_exit_discounted", title="Exit 3 unavailable",
        conditions={"exits_available": ["Exit 1", "Exit 2"], "exits_discounted": ["Exit 3"],
                    "occupancy_state": "night", "occupants_total": 4},
        assumptions=["Exit 3 is assumed blocked"], occupant_distribution=["Ground: 4"],
        routes=[{"from_area": "Ground", "via": "corridor", "to_exit": "Exit 1", "note": "12.0 m"}],
        bottlenecks=["Exit 1 is the narrowest at 1.02 m"], risks=["longer travel"],
        narrative="With Exit 3 shut, everyone leaves by Exit 1.",
        simulation={"movement_model": "steering", "end_time_s": 600.0,
                    "pre_movement": {"distribution": "normal", "mean_s": 60.0, "sd_s": 10.0,
                                     "basis": "alert occupants"},
                    "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                                  "speed_ms_mean": 1.2, "speed_ms_sd": 0.0,
                                  "shoulder_width_m": 0.45, "basis": "able adults"}],
                    "occupancy_multipliers": []},
        regulatory_justification="ENG-R11", ai_explanation="tests the loss of Exit 3")

    scn = _assemble_scenario(content, 1, names)

    assert scn["conditions"]["exits_discounted"] == ["Exit 3"]
    assert scn["conditions"]["exits_discounted_ifc_ids"] == ["3uMBEvBYD33AT9ygIhdZHw"]
    assert scn["conditions"]["exits_available_ifc_ids"] == ["0vwVLBnBr9meBshDw$RHGp",
                                                            "3T$cyq5XDFhgqPE_ZtOXlr"]
    assert scn["routes"][0]["to_exit"] == "Exit 1"


# ---- the record still closes the right door ---------------------------------------------------------

def _obj_named():
    """An object written the new way: conditions in names, with the resolved ids beside them."""
    exits = _exits()
    names = exit_names(exits)
    return {
        "building": {"total_occupant_load": 4},
        "exits": _exits_block(exits, names),
        "doors": [], "circulation": [], "elevators": [],
        "spaces": _spaces_block(_grounded(), [], names),
        "not_assessed": [],
        "scenarios": [
            {"id": "SCN-001", "type": "one_exit_discounted", "title": "Exit 3 unavailable",
             "conditions": {"exits_available": ["Exit 1", "Exit 2"],
                            "exits_discounted": ["Exit 3"],
                            "exits_available_ifc_ids": resolve_exit_ids(["Exit 1", "Exit 2"], names),
                            "exits_discounted_ifc_ids": resolve_exit_ids(["Exit 3"], names),
                            "occupancy_state": "day", "occupants_total": 4},
             "assumptions": [], "occupant_distribution": [], "routes": [], "bottlenecks": [],
             "risks": [], "narrative": "Everyone leaves by Exit 1."},
        ],
    }


def test_a_scenario_written_in_names_closes_the_right_globalid():
    obj = _obj_named()
    scn = obj["scenarios"][0]
    assert discounted_exit_ids(scn) == ["3uMBEvBYD33AT9ygIhdZHw"]
    assert available_exit_ids(scn) == ["0vwVLBnBr9meBshDw$RHGp", "3T$cyq5XDFhgqPE_ZtOXlr"]

    states = {e["exit_name"]: e["state"] for e in build_records(obj)[0]["relevant_ifc_element"]}
    assert states == {"Exit 1": "open", "Exit 2": "open", "Exit 3": "closed"}


def test_an_exit_name_the_model_invented_is_reported_not_ignored():
    obj = _obj_named()
    obj["scenarios"][0]["conditions"]["exits_discounted"] = ["Exit 9"]
    assert unknown_exit_references(obj) == [{"scenario": "SCN-001", "unknown_exits": ["Exit 9"]}]


def test_an_object_written_before_names_existed_reports_nothing_unknown():
    obj = _obj_named()
    conditions = obj["scenarios"][0]["conditions"]
    conditions["exits_available"] = conditions.pop("exits_available_ifc_ids")
    conditions["exits_discounted"] = conditions.pop("exits_discounted_ifc_ids")
    assert unknown_exit_references(obj) == []
    assert discounted_exit_ids(obj["scenarios"][0]) == ["3uMBEvBYD33AT9ygIhdZHw"]
