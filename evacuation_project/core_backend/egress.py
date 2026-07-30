#It just explains about the connectivity, the nearest exit and the approximate travel distance

import math
from collections import deque, defaultdict

from core_backend.occupancy import occupant_load
from core_backend.travel_distance import compute_travel_distances
import sys
from collections import Counter
from core_backend.ifc_parser import parser_summary
from core_backend.space_classifier import classify_spaces
from core_backend.sample_paths import resolve_ifc

OUTSIDE = "OUTSIDE"
GROUND_TOLERANCE_M = 0.5      
STAIR_MAX_DZ_M = 4.0          
STAIR_MAX_DXY_M = 12.0        


def _dist(a, b):
    if a is None or b is None:
        return None
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def _horizontal(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _nearest_storey_height(z, storeys):
    if z is None or not storeys:
        return None
    return min(storeys, key=lambda s: abs(s["elevation_m"] - z))["height_above_ground_m"]


def stair_adjacency(spaces, use_type):
    """Stair spaces that sit above/below each other close enough to be one shaft -> [(id_a, id_b), ...].

    Shared by build_graph and the Pathfinder export so the geometry rule (STAIR_MAX_DZ_M /
    STAIR_MAX_DXY_M) is stated in exactly one place.
    """
    stair_spaces = [sp for sp in spaces
                    if use_type.get(sp["id"]) == "stair" and sp["centroid"] is not None]
    pairs = []
    for i in range(len(stair_spaces)):
        for j in range(i + 1, len(stair_spaces)):
            a, b = stair_spaces[i]["centroid"], stair_spaces[j]["centroid"]
            dz = abs(a[2] - b[2])
            if 0.1 < dz <= STAIR_MAX_DZ_M and _horizontal(a, b) <= STAIR_MAX_DXY_M:
                pairs.append((stair_spaces[i]["id"], stair_spaces[j]["id"]))
    return pairs


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
        height = _nearest_storey_height(p[2], storeys) if p else None
        if height is not None and abs(height) <= GROUND_TOLERANCE_M:
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
 
    for a_id, b_id in stair_adjacency(summary["spaces"], use_type):
        adjacency[a_id].add(b_id)
        adjacency[b_id].add(a_id)

    return adjacency, positions, final_exits


def _node_key(node):
    """Stable sort key over the graph's mixed node types: space-id / OUTSIDE strings and
    ("EXIT", door_id) tuples."""
    return (1, node[0], node[1]) if isinstance(node, tuple) else (0, node, "")


def _bfs_prev(start, adjacency):
    """BFS predecessor tree from start over the adjacency graph.

    Neighbours are visited in a sorted order, not set order. Adjacency is built from sets, whose
    iteration order for strings changes with the interpreter's hash seed — leaving that unsorted made
    equidistant exits tie-break differently between runs, so the same model could report a different
    nearest_exit (and a different fallback distance) each time it was parsed.
    """
    prev = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbour in sorted(adjacency[node], key=_node_key):
            if neighbour not in prev:
                prev[neighbour] = node
                queue.append(neighbour)
    return prev


def _reconstruct(prev, target):
    if target not in prev:
        return None
    path, node = [], target
    while node is not None:
        path.append(node)
        node = prev[node]
    return list(reversed(path))


def _path_distance(path, positions):
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
    prev = _bfs_prev(space_id, adjacency)
    best = None
    for node in prev:
        if isinstance(node, tuple) and node[0] == "EXIT":
            path = _reconstruct(prev, node)
            distance = _path_distance(path, positions)
            if best is None or distance < best[1]:
                best = (node[1], distance, path)
    if best is None:
        return None, None, None
    return best


def ground_spaces(summary, classified, discounted_exits=frozenset(), jurisdiction="england"):
    adjacency, positions, final_exits = build_graph(summary, classified, discounted_exits)
    use_type = {c["guid"]: c["use_type"] for c in classified}

    travel = compute_travel_distances(summary, classified, final_exits, discounted_exits)

    dwelling_storeys = {(sp["storey"] or {}).get("id")
                        for sp in summary["spaces"] if use_type.get(sp["id"]) == "dwelling"}
    dwelling_storeys.discard(None)

    grounded, not_assessed = [], []
    excluded_measurement_zones = 0
    for sp in summary["spaces"]:
        gid = sp["id"]
        ut = use_type.get(gid, "unknown")

        if ut == "measurement_zone":
            excluded_measurement_zones += 1
            continue

        on_dwelling = (sp["storey"] or {}).get("id") in dwelling_storeys
        occ = occupant_load(sp, ut, on_dwelling_storey=on_dwelling, jurisdiction=jurisdiction)
        exit_id, bfs_distance, _ = nearest_exit(gid, adjacency, positions)

        td = travel.get(gid)
        if td is not None:
            distance = td["travel_distance_m"]
            reachable = td["reachable"]
            method = td["travel_distance_method"]
            remote_point = td["most_remote_point"]
        else:
            distance = round(bfs_distance, 1) if bfs_distance is not None else None
            reachable = exit_id is not None
            method = "fallback_centroid"
            remote_point = None
        occupiable = occ["occupant_load"] is None or occ["occupant_load"] > 0

        grounded.append({
            "guid": gid,
            "name": sp["name"],
            "long_name": sp["long_name"],
            "use_type": ut,
            "storey": sp["storey"],
            "area_m2": sp["area"],
            # (x, y, z) in metres, IFC world coords — the occupant seed point for an egress simulator
            "centroid": sp["centroid"],
            "occupant_load": occ["occupant_load"],
            "occupant_basis": occ["occupant_basis"],
            "nearest_exit": exit_id,
            "travel_distance_m": distance,
            "travel_distance_method": method,
            "most_remote_point": remote_point,
            "reachable": reachable,
        })

        if occ["not_assessed"]:
            not_assessed.append({"element": gid, "name": sp["name"],
                                 "missing": occ["not_assessed"],
                                 "action": "flagged, not silently passed"})
        if occupiable and not reachable:
            not_assessed.append({"element": gid, "name": sp["name"],
                                 "missing": "no egress path to a ground-level final exit was found",
                                 "action": "flagged, not silently passed"})

    return {
        "spaces": grounded,
        "final_exits": list(final_exits.values()),
        "not_assessed": not_assessed,
        "excluded_measurement_zones": excluded_measurement_zones,
    }


def discount_exit(summary, classified, exit_id, jurisdiction="england"):
    return ground_spaces(summary, classified, discounted_exits=frozenset({exit_id}),
                         jurisdiction=jurisdiction)


if __name__ == "__main__":

    use_llm = "--llm" in sys.argv
    args = [a for a in sys.argv if not a.startswith("--")]
    summary = parser_summary(resolve_ifc(args))
    classified = classify_spaces(summary["spaces"], use_llm=use_llm)
    grounded = ground_spaces(summary, classified)

    spaces = grounded["spaces"]
    reachable = [s for s in spaces if s["reachable"]]
    with_occ = [s for s in spaces if s["occupant_load"] is not None]
    total_occ = sum(s["occupant_load"] for s in with_occ)
    dists = [s["travel_distance_m"] for s in reachable if s["travel_distance_m"]]
    occupiable = [s for s in spaces if s["occupant_load"] is None or s["occupant_load"] > 0]
    occ_reach = [s for s in occupiable if s["reachable"]]

    print(f"Final (ground-level) exits kept: {len(grounded['final_exits'])}")
    print(f"Measurement-zone overlays excluded: {grounded['excluded_measurement_zones']}")
    print(f"Egress spaces: {len(spaces)} (after excluding overlays)")
    print(f"Spaces reachable to a final exit: {len(reachable)}/{len(spaces)}")
    print(f"Occupiable spaces reachable:      {len(occ_reach)}/{len(occupiable)}")
    print(f"Spaces with occupant load: {len(with_occ)}/{len(spaces)} | total occupant load: {total_occ}")
    if dists:
        print(f"Geodesic travel distance (m): min={min(dists):.1f} "
              f"mean={sum(dists)/len(dists):.1f} max={max(dists):.1f}")
    print(f"not_assessed entries: {len(grounded['not_assessed'])}")

    print("\nOccupant load by use_type:")
    by_use = Counter()
    for s in with_occ:
        by_use[s["use_type"]] += s["occupant_load"]
    for ut, occ in by_use.most_common():
        print(f"  {ut:18} {occ}")

    print("\nSample grounded spaces (reachable):")
    for s in reachable[:10]:
        st = s["storey"]["name"] if s["storey"] else "None"
        print(f"  {s['use_type']:14} storey={st:10} occ={s['occupant_load']} "
              f"exit={s['nearest_exit'][:8] if s['nearest_exit'] else None} "
              f"dist={s['travel_distance_m']}m ({s['travel_distance_method']})")
