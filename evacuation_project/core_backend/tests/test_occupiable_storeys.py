"""Known-answer tests for datum levels (deterministic, no API, no IFC file).

Authoring tools write an IfcBuildingStorey for every level they measure against — foundations,
a roof, a sea-level survey mark — so a 5-storey block parses as 8 storeys. Those datums hold no
rooms, so counting them inflates the building, drags phantom floors into the fire checks and, if
the entrance tag is ever missing, drops the ground reference to the bottom of the sea-level datum.
The rule is that a level with at least one room on it is occupied; everything else is a datum.
"""

from core_backend.ifc_parser import occupiable_storeys
from core_backend.uk_regulation_checking import basement_manual_review, load_regs


def nordic_storeys():
    # the BIM4LCA-ARK model: a hillside block whose site sits 47.3m above the sea-level datum,
    # with the entrance on Floor_01 -- heights are relative to that
    return [
        {"id": "SEA", "name": "Sea level", "elevation_m": 0.0,
         "height_above_ground_m": -52.0, "occupiable": False, "space_count": 0},
        {"id": "FND", "name": "Foundations", "elevation_m": 47.3,
         "height_above_ground_m": -4.7, "occupiable": False, "space_count": 0},
        {"id": "BSM", "name": "Basement", "elevation_m": 49.07,
         "height_above_ground_m": -2.93, "occupiable": True, "space_count": 17},
        {"id": "F01", "name": "Floor_01", "elevation_m": 52.0,
         "height_above_ground_m": 0.0, "occupiable": True, "space_count": 16},
        {"id": "F04", "name": "Floor_04", "elevation_m": 61.13,
         "height_above_ground_m": 9.13, "occupiable": True, "space_count": 22},
        {"id": "ROF", "name": "Roof", "elevation_m": 64.035,
         "height_above_ground_m": 12.035, "occupiable": False, "space_count": 0},
    ]


def test_datum_levels_are_not_storeys():
    kept = occupiable_storeys(nordic_storeys())
    assert [s["name"] for s in kept] == ["Basement", "Floor_01", "Floor_04"]


def test_storeys_parsed_before_the_flag_existed_all_pass():
    # summaries without the key must not silently lose every storey
    legacy = [{"id": "S1", "name": "Ground", "elevation_m": 0.0, "height_above_ground_m": 0.0}]
    assert occupiable_storeys(legacy) == legacy


def test_a_building_with_no_occupied_level_keeps_its_storeys():
    # nothing to measure against is worse than measuring against a datum
    datums = [s for s in nordic_storeys() if not s["occupiable"]]
    assert len(occupiable_storeys(datums)) == len(datums)


def test_only_the_real_basement_is_flagged_for_review():
    # the check is "below the entrance", so a foundation slab at -4.7m and a sea-level mark at
    # -52.0m both used to be reported as basements that may contain habitable rooms
    rule = load_regs("wales")["WAL-R6"]
    flagged = basement_manual_review(occupiable_storeys(nordic_storeys()), rule)
    assert [f["element_name"] for f in flagged] == ["Basement"]
