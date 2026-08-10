#This is the calculation of the travel distance since giving this calculation by LLM is not feasible and
#tells about reaching the nearest exit from the corner of each room.

import math

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

CELL_M = 0.1   
CELL_M = 0.1              
body_clearance_m = 0.15      
portal_radium_min_m = 0.3    
ground_tol_m = 0.5           
default_pitch_k = 2.0        
non_walkable = {"measurement_zone", "plant"}   


bridge_tol_m = 0.6

METHOD = "geodesic_grid"


def pitch_factor(summary):
    flights = [f for f in summary.get("stair_flights", []) if f.get("slope_m") and f.get("rise_m")]
    total_rise = sum(f["rise_m"] for f in flights)
    if total_rise <= 0:
        return default_pitch_k
    return sum(f["slope_m"] for f in flights) / total_rise


def inset(poly):
    for margin in (body_clearance_m, 0.05):
        p = poly.buffer(-margin)
        if not p.is_empty and p.area > 0:
            return p
    return poly


class _Field:
    def _init_(self, walkable, seeds, cell_m=CELL_M):
        minx, miny, maxx, maxy = walkable.bounds
        self.minx, self.miny, self.cell = minx, miny, cell_m
        self.nx = int(math.ceil((maxx - minx) / cell_m)) + 1
        self.ny = int(math.ceil((maxy - miny) / cell_m)) + 1
        xs = minx + (np.arange(self.nx) + 0.5) * cell_m
        ys = miny + (np.arange(self.ny) + 0.5) * cell_m
        self.xs, self.ys = xs, ys
        gx, gy = np.meshgrid(xs, ys)                       
        self.mask = shapely.contains_xy(walkable, gx, gy)  

        ids = np.full((self.ny, self.nx), -1, dtype=np.int64)
        walk = np.flatnonzero(self.mask.ravel())
        ids.ravel()[walk] = np.arange(walk.size)
        self.ids = ids
        n = walk.size

        d = cell_m
        diag = cell_m * math.sqrt(2)
        src, dst, wt = [], [], []
        for dy, dx, w in [(0, 1, d), (1, 0, d), (1, 1, diag), (1, -1, diag)]:
            y0, y1 = max(0, -dy), self.ny - max(0, dy)
            x0, x1 = max(0, -dx), self.nx - max(0, dx)
            a = ids[y0:y1, x0:x1]
            b = ids[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
            good = (a >= 0) & (b >= 0)
            src.append(a[good]); dst.append(b[good]); wt.append(np.full(int(good.sum()), w))

        for geom, offset in seeds:
            sc = shapely.contains_xy(geom, gx, gy) & self.mask
            seed_ids = ids[sc]
            if seed_ids.size:
                src.append(np.full(seed_ids.size, n))
                dst.append(seed_ids)
                wt.append(np.full(seed_ids.size, float(offset)))

        if src:
            src = np.concatenate(src); dst = np.concatenate(dst); wt = np.concatenate(wt)
        else:
            src = dst = wt = np.empty(0)
        graph = csr_matrix((wt, (src, dst)), shape=(n + 1, n + 1))
        dist = dijkstra(graph, directed=False, indices=n)   
        self.dist = dist[:n]                                

    def at(self, x, y):
        ix = int(round((x - self.minx) / self.cell - 0.5))
        iy = int(round((y - self.miny) / self.cell - 0.5))
        ix = min(max(ix, 0), self.nx - 1)
        iy = min(max(iy, 0), self.ny - 1)
        cid = self.ids[iy, ix]
        if cid >= 0:
            return float(self.dist[cid])
        best = math.inf
        for r in range(1, 6):
            y0, y1 = max(0, iy - r), min(self.ny, iy + r + 1)
            x0, x1 = max(0, ix - r), min(self.nx, ix + r + 1)
            block = self.ids[y0:y1, x0:x1]
            valid = block[block >= 0]
            if valid.size:
                best = float(np.min(self.dist[valid]))
                break
        return best

    def room_distance(self, inset_poly):
        pminx, pminy, pmaxx, pmaxy = inset_poly.bounds
        ix0 = max(0, int((pminx - self.minx) / self.cell))
        ix1 = min(self.nx, int((pmaxx - self.minx) / self.cell) + 1)
        iy0 = max(0, int((pminy - self.miny) / self.cell))
        iy1 = min(self.ny, int((pmaxy - self.miny) / self.cell) + 1)
        if ix0 >= ix1 or iy0 >= iy1:
            return None, None
        gx, gy = np.meshgrid(self.xs[ix0:ix1], self.ys[iy0:iy1])
        inside = shapely.contains_xy(inset_poly, gx, gy) & self.mask[iy0:iy1, ix0:ix1]
        if not inside.any():
            return None, None
        cids = self.ids[iy0:iy1, ix0:ix1][inside]
        dvals = self.dist[cids]
        finite = np.isfinite(dvals)
        if not finite.any():
            return math.inf, None                         
        far = int(np.argmax(np.where(finite, dvals, -np.inf)))
        yy, xx = np.nonzero(inside)
        return float(dvals[far]), (float(gx[yy[far], xx[far]]), float(gy[yy[far], xx[far]]))


def door_portal(x, y, width_m):
    r = max((width_m or 0) / 2.0, portal_radium_min_m)
    return shapely.Point(x, y).buffer(r)


def connector(portal, x, y, inset, width_m, obstacles):
    if portal.intersects(inset) or inset.distance(portal) > bridge_tol_m:
        return None
    spine = shapely.shortest_line(shapely.Point(x, y), inset)
    if any(spine.intersects(other) for other in obstacles):
        return None
    return spine.buffer(max((width_m or 0) / 2.0, portal_radium_min_m))


def compute_travel_distances(summary, classified, final_exits, discounted=frozenset(), report=None):
    report = report if report is not None else {}
    skipped = {}
    spaces = [s for s in summary["spaces"] if s.get("footprint") is not None]
    use_by_id = {c["guid"]: c["use_type"] for c in classified}
    door_by_id = {d["id"]: d for d in summary.get("doors", [])}
    links = summary.get("door_space_links", {})
    space_by_id = {s["id"]: s for s in summary["spaces"]}

    height = {st["id"]: st["height_above_ground_m"] for st in summary.get("storeys", [])}
    pitch_k = pitch_factor(summary)

    def storey_id(s):
        return (s.get("storey") or {}).get("id")

    by_storey = {}
    for s in spaces:
        if use_by_id.get(s["id"]) in non_walkable:
            continue
        by_storey.setdefault(storey_id(s), []).append(s)

    insets = {s["id"]: inset (s["footprint"]) for rooms in by_storey.values() for s in rooms}
    insets_on_storey = {st: [insets[s["id"]] for s in rooms] for st, rooms in by_storey.items()}
    room_ids_on_storey = {st: [s["id"] for s in rooms] for st, rooms in by_storey.items()}

    doors_on_storey = {}
    door_stair_side = {}    
    unbridged = {}          
    for door_id, linked in links.items():
        linked = [sid for sid in linked if sid in space_by_id]
        if not linked:
            continue
        pos = (door_by_id.get(door_id) or {}).get("position")
        if pos is None:
            continue
        width = (door_by_id.get(door_id) or {}).get("width_m")
        portal = door_portal(pos[0], pos[1], width)
        stair_sides = [sid for sid in linked if use_by_id.get(sid) == "stair"]
        non_stair = [sid for sid in linked if use_by_id.get(sid) != "stair"]
        for sid in linked:
            st = storey_id(space_by_id[sid])
            doors_on_storey.setdefault(st, []).append(portal)
            inset = insets.get(sid)
            if inset is None:
                continue
            obstacles = [insets[other] for other in room_ids_on_storey.get(st, [])
                         if other not in linked]
            bridge = connector(portal, pos[0], pos[1], inset, width, obstacles)
            if bridge is not None:
                doors_on_storey[st].append(bridge)
            elif not portal.intersects(inset):
                unbridged[st] = unbridged.get(st, 0) + 1
        if stair_sides and non_stair:
            for stair_sid in stair_sides:
                st = storey_id(space_by_id[stair_sid])
                door_stair_side.setdefault(st, []).append((portal, space_by_id[stair_sid]))

    def is_ground(sid):
        return sid in height and abs(height[sid]) <= ground_tol_m

    ground_ids = [sid for sid in by_storey if is_ground(sid)]
    ground_field = None
    if ground_ids:
        walk_geoms, seeds = [], []
        for sid in ground_ids:
            walk_geoms += insets_on_storey.get(sid, [])
            walk_geoms += doors_on_storey.get(sid, [])
        for ex in final_exits.values():
            if ex["id"] in discounted:
                continue
            p = ex.get("position")
            if p is not None:
                seeds.append((door_portal(p[0], p[1], ex.get("width_m")), 0.0))
        if walk_geoms and seeds:
            ground_field = _Field(shapely.union_all(walk_geoms + [g for g, _ in seeds]), seeds)

    results = {}

    def measure_storey(sid, field):
        for s in by_storey[sid]:
            dist, point = field.room_distance(insets[s["id"]])
            reachable = dist is not None and math.isfinite(dist)
            results[s["id"]] = {
                "travel_distance_m": round(dist, 1) if reachable else None,
                "travel_distance_method": METHOD,
                "most_remote_point": [round(point[0], 2), round(point[1], 2)] if point else None,
                "reachable": reachable,
                "reason": None if reachable else (
                    "room is a disconnected part of the walkable network — no door bridged it"
                    if dist is not None else
                    "room footprint placed no cell on the storey raster"),
            }

    # ground storeys
    if ground_field is not None:
        for sid in ground_ids:
            measure_storey(sid, ground_field)

    for sid, rooms in by_storey.items():
        if is_ground(sid) or sid is None:
            continue
        stair_doors = door_stair_side.get(sid, [])
        if not stair_doors or ground_field is None:
            continue
        descent = abs(height.get(sid, 0.0)) * pitch_k
        seeds = []
        for portal, stair_space in stair_doors:
            base = stair_space.get("centroid")
            ground_portion = ground_field.at(base[0], base[1]) if base else math.inf
            if math.isfinite(ground_portion):
                seeds.append((portal, descent + ground_portion))
        if not seeds:
            continue
        walk_geoms = insets_on_storey.get(sid, []) + doors_on_storey.get(sid, [])
        field = _Field(shapely.union_all(walk_geoms + [g for g, _ in seeds]), seeds)
        measure_storey(sid, field)

    for sid in by_storey:
        if not any(s["id"] in results for s in by_storey[sid]):
            skipped[sid] = len(by_storey[sid])

    report.update({"unbridged_door_room_pairs": unbridged, "storeys_not_measured": skipped})
    return results
