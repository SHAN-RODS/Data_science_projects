"""Known-answer tests for the egress grounding logic (deterministic, no IFC / no API).

Rather than ship synthetic IFC files, these drive ``ground_spaces`` with hand-built graphs whose
travel distances can be computed by hand:

  * a rectangular room whose centroid-to-exit distance is the 3-4-5 straight line (5.0 m);
  * an L-shaped route where the path bends round a corridor and *exceeds* the Euclidean distance.

They assert the core invariant that the approximate travel distance is at least the straight line.
"""

import math

from core_backend.egress import ground_spaces


def _ground_storey():
    return [{"id": "S1", "name": "Ground", "elevation_m": 0.0, "height_above_ground_m": 0.0}]


def test_rectangular_room_straight_line():
    summary = {
        "spaces": [{"id": "A", "name": "A", "long_name": "Living", "area": 12.0,
                    "centroid": (0.0, 0.0, 0.0), "storey": {"id": "S1", "name": "Ground"}}],
        "emergency_exits": [{"id": "E", "name": "Exit", "width_m": 1.0, "position": (3.0, 4.0, 0.0)}],
        "door_space_links": {"E": ["A"]},
        "stairs": [],
        "storeys": _ground_storey(),
    }
    classified = [{"guid": "A", "use_type": "living"}]

    grounded = ground_spaces(summary, classified)
    a = next(s for s in grounded["spaces"] if s["guid"] == "A")

    assert a["reachable"] is True
    assert a["nearest_exit"] == "E"
    assert abs(a["approx_travel_distance_m"] - 5.0) < 1e-6  # 3-4-5 triangle


def test_l_corridor_exceeds_euclidean():
    summary = {
        "spaces": [
            {"id": "A", "name": "A", "long_name": "Living", "area": 12.0,
             "centroid": (0.0, 0.0, 0.0), "storey": {"id": "S1", "name": "Ground"}},
            {"id": "B", "name": "B", "long_name": "Corridor", "area": 8.0,
             "centroid": (10.0, 0.0, 0.0), "storey": {"id": "S1", "name": "Ground"}},
        ],
        "emergency_exits": [{"id": "E", "name": "Exit", "width_m": 1.0, "position": (10.0, 10.0, 0.0)}],
        "door_space_links": {"D1": ["A", "B"], "E": ["B"]},
        "stairs": [],
        "storeys": _ground_storey(),
    }
    classified = [{"guid": "A", "use_type": "living"}, {"guid": "B", "use_type": "circulation"}]

    grounded = ground_spaces(summary, classified)
    a = next(s for s in grounded["spaces"] if s["guid"] == "A")
    euclidean = math.dist((0.0, 0.0, 0.0), (10.0, 10.0, 0.0))  # ~14.14

    assert a["reachable"] is True
    assert abs(a["approx_travel_distance_m"] - 20.0) < 1e-6      # 10 (A->B) + 10 (B->exit)
    assert a["approx_travel_distance_m"] > euclidean            # the path bends past straight-line


def test_unreachable_space_is_flagged():
    summary = {
        "spaces": [{"id": "X", "name": "X", "long_name": "Living", "area": 12.0,
                    "centroid": (0.0, 0.0, 0.0), "storey": {"id": "S1", "name": "Ground"}}],
        "emergency_exits": [],          # no exits at all
        "door_space_links": {},
        "stairs": [],
        "storeys": _ground_storey(),
    }
    classified = [{"guid": "X", "use_type": "living"}]

    grounded = ground_spaces(summary, classified)
    x = next(s for s in grounded["spaces"] if s["guid"] == "X")

    assert x["reachable"] is False
    # an occupiable, unreachable space must be surfaced, never silently passed
    assert any(na["element"] == "X" for na in grounded["not_assessed"])
