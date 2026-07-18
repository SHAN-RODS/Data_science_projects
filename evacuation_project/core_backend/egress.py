"""Grounding layer — connectivity, nearest exit, and approximate travel distance (deterministic).

This is *computation*, not AI. It organises the `space_boundaries` / `connected_elements` the parser
already extracts into a lightweight traversal graph and reads distances off the space centroids. It is
deliberately **not** a shortest-path metric engine (no Dijkstra / IndoorGML) — it does breadth-first
reachability and sums straight-line centroid segments along the path found.

Model:
  * A shared **door** between two spaces is a traversable edge; a door on a space that is also a
    **final exit** connects that space to OUTSIDE. A shared wall is adjacency, not traversable.
  * **Final exits** are exit-flagged doors on the entrance/ground storey only. Exit-flagged doors on
    upper floors (balcony/fire doors the coarse detector picks up) are not treated as ways to safety;
    upper-floor occupants must route down via the **stairs**, which are edges between stair spaces on
    adjacent storeys.
  * All travel distances are labelled **approx** — centroid-to-exit straight-line segments summed
    along the path, not compliance-grade measurements.

Every space that cannot be grounded (no occupant load, no path to a final exit) is surfaced in
`not_assessed` rather than silently passed.
"""

import math
from collections import deque, defaultdict

from core_backend.occupancy import occupant_load

OUTSIDE = "OUTSIDE"
GROUND_TOLERANCE_M = 0.5      # a storey within this of entrance level counts as ground
STAIR_MAX_DZ_M = 4.0          # two stair spaces this close vertically are one flight apart
STAIR_MAX_DXY_M = 12.0        # ...and this close horizontally are the same stair core


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


def build_graph(summary, classified, discounted_exits=frozenset()):
    """Build the traversal graph.

    Returns (adjacency, positions, final_exits) where:
      adjacency: node -> set(nodes); nodes are space GUIDs, ("EXIT", door_id) tuples, and OUTSIDE.
      positions: node -> (x, y, z) for spaces (centroid) and exit nodes (door position).
      final_exits: {door_id: {id, name, width_m, storey, position}} for ground-level exits kept.
    """
    spaces = {sp["id"]: sp for sp in summary["spaces"]}
    use_type = {c["guid"]: c["use_type"] for c in classified}
    storeys = summary.get("storeys", [])

    positions = {sp_id: sp["centroid"] for sp_id, sp in spaces.items()}
    adjacency = defaultdict(set)

    # --- final exits: ground-level, exit-flagged doors not discounted ---------------------------
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

    # --- door edges: doors linked to the spaces they connect (boundaries + hosting walls) --------
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
            # interior (or non-final) door: connect every pair of spaces it bounds
            for i in range(len(bounded)):
                for j in range(i + 1, len(bounded)):
                    adjacency[bounded[i]].add(bounded[j])
                    adjacency[bounded[j]].add(bounded[i])

    # --- stair edges: link stair spaces on adjacent storeys within the same core ----------------
    stair_spaces = [sp for sp in summary["spaces"]
                    if use_type.get(sp["id"]) == "stair" and sp["centroid"] is not None]
    for i in range(len(stair_spaces)):
        for j in range(i + 1, len(stair_spaces)):
            a, b = stair_spaces[i]["centroid"], stair_spaces[j]["centroid"]
            dz = abs(a[2] - b[2])
            if 0.1 < dz <= STAIR_MAX_DZ_M and _horizontal(a, b) <= STAIR_MAX_DXY_M:
                adjacency[stair_spaces[i]["id"]].add(stair_spaces[j]["id"])
                adjacency[stair_spaces[j]["id"]].add(stair_spaces[i]["id"])

    return adjacency, positions, final_exits


def _bfs_prev(start, adjacency):
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


def _reconstruct(prev, target):
    if target not in prev:
        return None
    path, node = [], target
    while node is not None:
        path.append(node)
        node = prev[node]
    return list(reversed(path))


def _path_distance(path, positions):
    """Sum straight-line segments between successive positioned nodes along the path (approx)."""
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
    """Return (exit_door_id, approx_travel_distance_m, path) for the min-distance reachable exit.

    Uses BFS reachability, then picks the exit with the smallest summed centroid-path distance among
    all reachable exits. Returns (None, None, None) if no final exit is reachable.
    """
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


def ground_spaces(summary, classified, discounted_exits=frozenset()):
    """Produce per-space grounded facts + a not_assessed list.

    Returns {"spaces": [...], "final_exits": [...], "not_assessed": [...]}.
    """
    adjacency, positions, final_exits = build_graph(summary, classified, discounted_exits)
    use_type = {c["guid"]: c["use_type"] for c in classified}

    grounded, not_assessed = [], []
    excluded_measurement_zones = 0
    for sp in summary["spaces"]:
        gid = sp["id"]
        ut = use_type.get(gid, "unknown")

        # BIM area/volume overlays are not egress spaces — exclude them rather than flag them.
        if ut == "measurement_zone":
            excluded_measurement_zones += 1
            continue

        occ = occupant_load(sp, ut)
        exit_id, distance, _ = nearest_exit(gid, adjacency, positions)
        reachable = exit_id is not None
        # a space is occupiable if it carries occupants or its use is still unresolved
        occupiable = occ["occupant_load"] is None or occ["occupant_load"] > 0

        grounded.append({
            "guid": gid,
            "name": sp["name"],
            "long_name": sp["long_name"],
            "use_type": ut,
            "storey": sp["storey"],
            "area_m2": sp["area"],
            "occupant_load": occ["occupant_load"],
            "occupant_basis": occ["occupant_basis"],
            "nearest_exit": exit_id,
            "approx_travel_distance_m": round(distance, 1) if distance is not None else None,
            "reachable": reachable,
        })

        if occ["not_assessed"]:
            not_assessed.append({"element": gid, "name": sp["name"],
                                 "missing": occ["not_assessed"],
                                 "action": "flagged, not silently passed"})
        # only an occupiable space that cannot reach a final exit is a safety gap worth surfacing
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


def discount_exit(summary, classified, exit_id):
    """Re-ground the building with one exit removed (for the one-exit-discounted scenario variant)."""
    return ground_spaces(summary, classified, discounted_exits=frozenset({exit_id}))


if __name__ == "__main__":
    import sys
    from collections import Counter
    from core_backend.ifc_parser import parser_summary
    from core_backend.space_classifier import classify_spaces
    from core_backend.sample_paths import resolve_ifc

    # dictionary-only by default (API-free spine check); pass --llm for the full classification
    use_llm = "--llm" in sys.argv
    args = [a for a in sys.argv if not a.startswith("--")]
    summary = parser_summary(resolve_ifc(args))
    classified = classify_spaces(summary["spaces"], use_llm=use_llm)
    grounded = ground_spaces(summary, classified)

    spaces = grounded["spaces"]
    reachable = [s for s in spaces if s["reachable"]]
    with_occ = [s for s in spaces if s["occupant_load"] is not None]
    total_occ = sum(s["occupant_load"] for s in with_occ)
    dists = [s["approx_travel_distance_m"] for s in reachable if s["approx_travel_distance_m"]]
    occupiable = [s for s in spaces if s["occupant_load"] is None or s["occupant_load"] > 0]
    occ_reach = [s for s in occupiable if s["reachable"]]

    print(f"Final (ground-level) exits kept: {len(grounded['final_exits'])}")
    print(f"Measurement-zone overlays excluded: {grounded['excluded_measurement_zones']}")
    print(f"Egress spaces: {len(spaces)} (after excluding overlays)")
    print(f"Spaces reachable to a final exit: {len(reachable)}/{len(spaces)}")
    print(f"Occupiable spaces reachable:      {len(occ_reach)}/{len(occupiable)}")
    print(f"Spaces with occupant load: {len(with_occ)}/{len(spaces)} | total occupant load: {total_occ}")
    if dists:
        print(f"Approx travel distance (m): min={min(dists):.1f} "
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
              f"dist={s['approx_travel_distance_m']}m")
