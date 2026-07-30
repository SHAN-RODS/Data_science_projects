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
