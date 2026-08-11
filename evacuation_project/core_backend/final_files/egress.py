#Geometry helpers for egress: the connectivity graph, ground-level final-exit detection and (reference)
#nearest-exit search. Occupancy and travel distance are now produced by the LLM (scenario_generation_llm_1),
#so this module is pure geometry — build_graph is what the generator consumes.

import math
from collections import deque, defaultdict

import sys
from core_backend.ifc_parser import parser_summary
from core_backend.space_classifier import classify_spaces
from core_backend.sample_paths import resolve_ifc

OUTSIDE = "OUTSIDE"
ground_tolerance_m = 0.5      
stair_max_dz_m = 4.0          
stair_max_dxy_m = 12.0        


def _dist(a, b):
    if a is None or b is None:
        return None
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


defhorizontal(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nearest_storey_height(z, storeys):
    if z is None or not storeys:
        return None
    return min(storeys, key=lambda s: abs(s["elevation_m"] - z))["height_above_ground_m"]


def build_graph(summary, classified, discounted_exits=frozenset()):
    spaces = {sp["id"]: sp for sp in summary["spaces"]}
    use_type = {c["guid"]: c["use_type"] for c in classified}
    storeys = summary.get("storeys", [])

    positions = {sp_id: sp["centroid"] for sp_id, sp in spaces.items()}
    adjacency = defaultdict(set)

    final_exits = {}
    for door in summary.get("emergency_exits", []):
        if door["id"] in discounted_exits:
            continue
        p = door.get("position")
        height = nearest_storey_height(p[2], storeys) if p else None
        if height is not None and abs(height) <= ground_tolerance_m:
            final_exits[door["id"]] = {
                "id": door["id"], "name": door.get("name"), "width_m": door.get("width_m"),
                "position": p, "storey_height_m": height,
            }

    for door_id, bounded in summary.get("door_space_links", {}).items():
        bounded = [s for s in bounded if s in spaces]
        if not bounded:
            continue
        if door_id in final_exits:
            node = ("EXIT", door_id)
            positions[node] = final_exits[door_id]["position"]
            for s in bounded:
                adjacency[s].add(node)
                adjacency[node].add(s)
            adjacency[node].add(OUTSIDE)
            adjacency[OUTSIDE].add(node)
        else:
            for i in range(len(bounded)):
                for j in range(i + 1, len(bounded)):
                    adjacency[bounded[i]].add(bounded[j])
                    adjacency[bounded[j]].add(bounded[i])
 
    stair_spaces = [sp for sp in summary["spaces"]
                    if use_type.get(sp["id"]) == "stair" and sp["centroid"] is not None]
    for i in range(len(stair_spaces)):
        for j in range(i + 1, len(stair_spaces)):
            a, b = stair_spaces[i]["centroid"], stair_spaces[j]["centroid"]
            dz = abs(a[2] - b[2])
            if 0.1 < dz <= stair_max_dz_m andhorizontal(a, b) <= stair_max_dxy_m:
                adjacency[stair_spaces[i]["id"]].add(stair_spaces[j]["id"])
                adjacency[stair_spaces[j]["id"]].add(stair_spaces[i]["id"])

    return adjacency, positions, final_exits


def bfc_prev(start, adjacency):
    """BFS predecessor tree from start over the adjacency graph."""
    prev = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbour in adjacency[node]:
            if neighbour not in prev:
                prev[neighbour] = node
                queue.append(neighbour)
    return prev


def reconstruct(prev, target):
    if target not in prev:
        return None
    path, node = [], target
    while node is not None:
        path.append(node)
        node = prev[node]
    return list(reversed(path))


def path_distance(path, positions):
    total, previous = 0.0, None
    for node in path:
        p = positions.get(node)
        if p is None:
            continue
        if previous is not None:
            total += _dist(previous, p)
        previous = p
    return total


def nearest_exit(space_id, adjacency, positions):
    prev = bfc_prev(space_id, adjacency)
    best = None
    for node in prev:
        if isinstance(node, tuple) and node[0] == "EXIT":
            path = reconstruct(prev, node)
            distance = path_distance(path, positions)
            if best is None or distance < best[1]:
                best = (node[1], distance, path)
    if best is None:
        return None, None, None
    return best


if __name__ == "__main__":

    use_llm = "llm" in sys.argv
    args = [a for a in sys.argv if not a.startswith("--")]
    summary = parser_summary(resolve_ifc(args))
    classified = classify_spaces(summary["spaces"], use_llm=use_llm)

    adjacency, positions, final_exits = build_graph(summary, classified)
    spaces = summary["spaces"]
    reachable = sum(1 for sp in spaces if nearest_exit(sp["id"], adjacency, positions)[0] is not None)

    print(f"Spaces: {len(spaces)}")
    print(f"Final (ground-level) exits detected: {len(final_exits)}")
    for e in list(final_exits.values())[:10]:
        print(f"  {e['id']} name={e['name']} width_m={e['width_m']} height_m={round(e['storey_height_m'], 2)}")
    print(f"Spaces with a graph path to a final exit: {reachable}/{len(spaces)} "
          f"(reference connectivity; occupancy/distance are now produced by the LLM generator)")
