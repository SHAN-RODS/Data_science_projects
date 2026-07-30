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
    # no footprint on these hand-built spaces -> engine falls back to the centroid BFS
    assert a["travel_distance_method"] == "fallback_centroid"
    assert abs(a["travel_distance_m"] - 5.0) < 1e-6  # 3-4-5 triangle


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
    assert abs(a["travel_distance_m"] - 20.0) < 1e-6      # 10 (A->B) + 10 (B->exit)
    assert a["travel_distance_m"] > euclidean            # the path bends past straight-line


def test_equidistant_exits_tie_break_the_same_way_every_time():
    """A room with two exits at exactly the same distance must always pick the same one.

    The adjacency graph is built from sets, so before the BFS sorted its neighbours the winner
    depended on the interpreter's string hash seed — the same model could hand a different
    nearest_exit (and a different simulation goal) to each run.
    """
    summary = {
        "spaces": [{"id": "A", "name": "A", "long_name": "Living", "area": 12.0,
                    "centroid": (0.0, 0.0, 0.0), "storey": {"id": "S1", "name": "Ground"}}],
        "emergency_exits": [
            {"id": "E_NORTH", "name": "North exit", "width_m": 1.0, "position": (0.0, 5.0, 0.0)},
            {"id": "E_SOUTH", "name": "South exit", "width_m": 1.0, "position": (0.0, -5.0, 0.0)},
            {"id": "E_EAST", "name": "East exit", "width_m": 1.0, "position": (5.0, 0.0, 0.0)},
        ],
        "door_space_links": {"E_NORTH": ["A"], "E_SOUTH": ["A"], "E_EAST": ["A"]},
        "stairs": [],
        "storeys": _ground_storey(),
    }
    classified = [{"guid": "A", "use_type": "living"}]

    chosen = {next(s for s in ground_spaces(summary, classified)["spaces"]
                   if s["guid"] == "A")["nearest_exit"] for _ in range(5)}
    assert len(chosen) == 1


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
