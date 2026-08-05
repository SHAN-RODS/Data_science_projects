"""Known-answer test for the geodesic travel-distance engine (deterministic, no IFC / no API).

A synthetic single-storey layout with real footprint polygons and a hand-computable answer:

    room A (0..4 x 0..3)  --door D1 at x=4--  corridor B (4..14 x 0..3)  --exit E at x=14, y=1.5

The exit is at the far (east) end of the corridor. The most remote point of room A is its west wall
(x~0), so the walked distance is ~ (14 - 0) = ~14 m along the corridor centre — far longer than the
centroid-to-exit straight line, and measured from the far corner, not the centroid.
"""

from shapely.geometry import box

from core_backend.travel_distance import compute_travel_distances


def _summary():
    return {
        "spaces": [
            {"id": "A", "name": "A", "long_name": "Living", "area": 12.0,
             "centroid": (2.0, 1.5, 0.0), "footprint": box(0.0, 0.0, 4.0, 3.0),
             "storey": {"id": "S1", "name": "Ground"}},
            {"id": "B", "name": "B", "long_name": "Corridor", "area": 30.0,
             "centroid": (9.0, 1.5, 0.0), "footprint": box(4.0, 0.0, 14.0, 3.0),
             "storey": {"id": "S1", "name": "Ground"}},
        ],
        "doors": [
            {"id": "D1", "name": "D1", "width_m": 1.0, "position": (4.0, 1.5, 0.0)},
            {"id": "E", "name": "Exit", "width_m": 1.0, "position": (14.0, 1.5, 0.0)},
        ],
        "door_space_links": {"D1": ["A", "B"], "E": ["B"]},
        "stair_flights": [],
        "storeys": [{"id": "S1", "name": "Ground", "elevation_m": 0.0, "height_above_ground_m": 0.0}],
    }


def test_geodesic_most_remote_point_along_corridor():
    summary = _summary()
    classified = [{"guid": "A", "use_type": "living"}, {"guid": "B", "use_type": "circulation"}]
    final_exits = {"E": {"id": "E", "name": "Exit", "width_m": 1.0, "position": (14.0, 1.5, 0.0)}}

    result = compute_travel_distances(summary, classified, final_exits)

    a = result["A"]
    assert a["reachable"] is True
    assert a["travel_distance_method"] == "geodesic_grid"
    # exit is ~14 m from room A's far (west) wall; allow a small grid + clearance tolerance
    assert 12.5 <= a["travel_distance_m"] <= 15.0
    # the most remote point sits near the west wall of A, not at its centroid (x=2)
    assert a["most_remote_point"][0] < 1.5
    # the corridor's own most-remote distance is shorter than the room behind it
    assert result["B"]["travel_distance_m"] < a["travel_distance_m"]


def test_a_door_placed_off_the_wall_centreline_still_bridges():
    """A door's placement point is not reliably on the wall, so a bare disc can miss the room.

    D1 is pushed 0.45 m into the corridor -- beyond the reach of its own portal disc once room A has
    been inset by the body clearance. Without a connector A becomes its own island and reports no
    egress path at all, which is exactly the failure this guards.
    """
    summary = _summary()
    summary["doors"][0]["position"] = (4.45, 1.5, 0.0)
    classified = [{"guid": "A", "use_type": "living"}, {"guid": "B", "use_type": "circulation"}]
    final_exits = {"E": {"id": "E", "name": "Exit", "width_m": 1.0, "position": (14.0, 1.5, 0.0)}}

    result = compute_travel_distances(summary, classified, final_exits)

    assert result["A"]["reachable"] is True
    assert result["A"]["travel_distance_m"] > result["B"]["travel_distance_m"]


def test_a_door_the_hosting_wall_over_linked_does_not_bridge():
    """The hosting-wall rule attributes a door to every room its wall bounds, some metres away.

    Room C is listed against D1 but sits well beyond the tolerance, so no connector is run to it and
    it stays off the network -- a bridge that long would invent a doorway that does not exist.
    """
    summary = _summary()
    summary["spaces"].append(
        {"id": "C", "name": "C", "long_name": "Store", "area": 6.0,
         "centroid": (2.0, 8.0, 0.0), "footprint": box(0.0, 6.0, 4.0, 9.0),
         "storey": {"id": "S1", "name": "Ground"}})
    summary["door_space_links"]["D1"] = ["A", "B", "C"]
    classified = [{"guid": "A", "use_type": "living"}, {"guid": "B", "use_type": "circulation"},
                  {"guid": "C", "use_type": "storage"}]
    final_exits = {"E": {"id": "E", "name": "Exit", "width_m": 1.0, "position": (14.0, 1.5, 0.0)}}

    report = {}
    result = compute_travel_distances(summary, classified, final_exits, report=report)

    assert result["A"]["reachable"] is True
    assert result["C"]["reachable"] is False
    assert result["C"]["reason"] is not None
    # and the engine says so rather than staying quiet about it
    assert report["unbridged_door_room_pairs"].get("S1", 0) >= 1


def test_an_unreachable_room_says_why():
    summary = _summary()
    summary["door_space_links"] = {"E": ["B"]}          # room A is walled off
    classified = [{"guid": "A", "use_type": "living"}, {"guid": "B", "use_type": "circulation"}]
    final_exits = {"E": {"id": "E", "name": "Exit", "width_m": 1.0, "position": (14.0, 1.5, 0.0)}}

    result = compute_travel_distances(summary, classified, final_exits)

    assert result["A"]["reachable"] is False
    assert result["A"]["travel_distance_m"] is None
    assert "disconnected" in result["A"]["reason"]
