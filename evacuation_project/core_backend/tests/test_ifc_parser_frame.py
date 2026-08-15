"""Space-frame reconciliation: does the parser put rooms and doors in the same coordinate frame?

Some exporters lose the placement transform on a subset of IfcSpaces, so the space geometry comes back
in the raw authoring frame while doors keep correct world placements. Nothing then touches anything
and every room reports "no egress path". These tests pin the two properties that matter:

  * an arbitrary rigid mismatch is *solved* from the model's own door-to-room topology -- no angle is
    hardcoded, so a file tilted by any amount is handled;
  * a model whose frames already agree is left byte-for-byte alone.
"""

import math

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from core_backend.ifc_parser import augment_door_space_links, reconcile_space_frame

# A little three-room plan: two rooms side by side with a corridor along the top.
TRUE_ROOMS = {"A": box(0, 0, 4, 3), "B": box(4, 0, 14, 3), "C": box(0, 3, 14, 6)}
DOORS = [{"id": did, "name": did, "width_m": 1.0, "position": (x, y, 0.0)}
         for did, (x, y) in {"D1": (4.0, 1.5), "D2": (9.0, 3.0), "D3": (2.0, 3.0),
                             "D4": (14.0, 1.5), "D5": (7.0, 3.0), "D6": (12.0, 3.0)}.items()]
LINKS = {"D1": ["A", "B"], "D2": ["B", "C"], "D3": ["A", "C"],
         "D4": ["B"], "D5": ["B", "C"], "D6": ["B", "C"]}
STOREYS = [{"id": "S1", "name": "L1", "elevation_m": 0.0}]
LEVELS = [{"id": "S1", "name": "L1", "elevation_m": 0.0}]


def rigid_transform(degrees, dx, dy):
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    return lambda g: affine_transform(g, [c, -s, s, c, dx, dy])


def sample_spaces(rooms, transform):
    out = []
    for name, geometry in rooms.items():
        moved = transform(geometry)
        out.append({"id": name, "name": name, "storey": {"id": "S1", "name": "L1"},
                    "footprint": moved,
                    "centroid": (moved.centroid.x, moved.centroid.y, 1.2)})
    return out


@pytest.mark.parametrize("degrees,dx,dy", [
    (23.0, 11.0, -4.0),      # an arbitrary tilt
    (-60.6, -29.3, -13.2),   # the Nordic housing model's actual mismatch
    (0.0, 25.0, 8.0),        # pure translation, no rotation
    (137.5, -3.0, 40.0),     # past a quadrant boundary
])
def test_an_arbitrary_rigid_mismatch_is_solved_not_assumed(degrees, dx, dy):
    """Nothing about the angle is hardcoded, so any tilt is recovered from the model's own topology."""
    spaces = sample_spaces(TRUE_ROOMS, rigid_transform(degrees, dx, dy))
    report = reconcile_space_frame(spaces, DOORS, LINKS, STOREYS)

    assert len(report) == 1
    assert report[0]["corrected"] is True
    # the fit is the inverse of what was applied, to within a rounding of a degree
    assert report[0]["rotation_deg"] == pytest.approx(-degrees, abs=0.5)
    # and the rooms land back on the truth
    for space in spaces:
        assert space["footprint"].symmetric_difference(TRUE_ROOMS[space["id"]]).area < 0.05


def test_a_model_whose_frames_agree_is_left_untouched():
    """The correction can only fire when it demonstrably helps -- otherwise it is inert."""
    spaces = sample_spaces(TRUE_ROOMS, lambda g: g)
    before = [(s["footprint"].wkt, s["centroid"]) for s in spaces]

    report = reconcile_space_frame(spaces, DOORS, LINKS, STOREYS)

    assert report[0]["corrected"] is False
    assert "already agree" in report[0]["note"]
    assert [(s["footprint"].wkt, s["centroid"]) for s in spaces] == before


def test_too_few_correspondences_abstains_rather_than_guessing():
    spaces = sample_spaces(TRUE_ROOMS, rigid_transform(23.0, 11.0, -4.0))
    before = [s["footprint"].wkt for s in spaces]

    report = reconcile_space_frame(spaces, DOORS, {"D1": ["A", "B"]}, STOREYS)

    assert report[0]["corrected"] is False
    assert "too few" in report[0]["note"]
    assert [s["footprint"].wkt for s in spaces] == before


def test_a_storey_no_transform_explains_is_left_as_parsed():
    """Rooms scattered at random are not a rigid mismatch; the fit must decline, not invent one."""
    scattered = {"A": box(0, 0, 4, 3), "B": box(40, 80, 50, 83), "C": box(-70, 20, -56, 26)}
    spaces = sample_spaces(scattered, lambda g: g)
    before = [s["footprint"].wkt for s in spaces]

    report = reconcile_space_frame(spaces, DOORS, LINKS, STOREYS)

    assert report[0]["corrected"] is False
    assert [s["footprint"].wkt for s in spaces] == before


# An apartment zone *contains* its rooms; it is the same floor area modelled a second time.
FLAT = box(0.0, 0.0, 14.0, 3.0)
# A clearance marker *sits inside* one room. Same "no door links" symptom, opposite treatment.
TURNING_CIRCLE = box(8.0, 0.5, 9.5, 2.0)


def test_correction_is_applied_to_rooms_that_have_no_doors_of_their_own():
    """An apartment zone carries no space boundary, so it contributes no pair -- but it still moves."""
    rooms = dict(TRUE_ROOMS)
    rooms["FLAT"] = FLAT
    spaces = sample_spaces(rooms, rigid_transform(23.0, 11.0, -4.0))

    reconcile_space_frame(spaces, DOORS, LINKS, STOREYS)

    flat = next(s for s in spaces if s["id"] == "FLAT")
    assert flat["footprint"].symmetric_difference(FLAT).area < 0.05


def test_orphan_rooms_get_a_geometric_door_link():
    """door_space_links is topological; a room with no IfcRelSpaceBoundary would never route out."""
    rooms = dict(TRUE_ROOMS)
    rooms["FLAT"] = FLAT
    spaces = sample_spaces(rooms, lambda g: g)

    augmented, added = augment_door_space_links(LINKS, spaces, DOORS, LEVELS)

    assert added > 0
    assert any("FLAT" in bounded for bounded in augmented.values())
    # rooms topology already covered are not re-linked, and nothing existing is dropped
    for door_id, bounded in LINKS.items():
        assert set(bounded) <= set(augmented[door_id])


def test_an_overlay_inside_a_connected_room_is_not_linked():
    """A turning circle already inherits its room's connectivity; it is not a place to escape from."""
    rooms = dict(TRUE_ROOMS)
    rooms["TURN"] = TURNING_CIRCLE
    spaces = sample_spaces(rooms, lambda g: g)

    augmented, added = augment_door_space_links(LINKS, spaces, DOORS, LEVELS)

    assert added == 0
    assert not any("TURN" in bounded for bounded in augmented.values())


def test_no_orphans_means_no_geometric_links_are_invented():
    spaces = sample_spaces(TRUE_ROOMS, lambda g: g)

    augmented, added = augment_door_space_links(LINKS, spaces, DOORS, LEVELS)

    assert added == 0
    assert augmented == {k: sorted(v) for k, v in LINKS.items()}
