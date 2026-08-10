import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.unit as unit_util
import ifcopenshell.util.placement as placement_util
import ifcopenshell.geom as geom_util
import ifcopenshell.util.shape as shape_util
from collections import defaultdict
import math
import sys
from core_backend.sample_paths import resolve_ifc


def find_property(psets, keys):
    for pset in psets.values():
        for key in keys:
            if key in pset and pset[key] is not None:
                return pset[key]
    return None

def emergency_exit(name, all_psets):
    clear_name = (name or "").lower()
    if any(word in clear_name for word in ["exit", "escape", "emergency"]):
        return True

    for pset in all_psets.values():
        for key, value in pset.items():
            if any(word in str(key).lower() or word in str(value).lower()
                   for word in ["exit", "escape", "emergency"]):
                return True
    return False

def possible_evacuation_lifts(name, psets):
    all_evac_words = ["evacuation", "firefighter", "fire lift", "fire-rated lift", "evac lift"]
    name_lower = (name or "").lower()

    if any(word in name_lower for word in all_evac_words):
        return True

    for pset in psets.values():
        for key, value in pset.items():
            if any(word in str(key).lower() or word in str(value).lower() for word in all_evac_words):
                return True
    return False


def get_position(element_name, scale):
    if getattr(element_name, "ObjectPlacement", None) is None:
        return None
    try:
        matrix = placement_util.get_local_placement(element_name.ObjectPlacement)
        return (matrix[0][3] * scale, matrix[1][3] * scale, matrix[2][3] * scale)
    except Exception:
        return None
    

def load_project_name(model):
    project = model.by_type("IfcProject")
    if project:
        return project[0].Name or "No Name"
    return "No IFC project name found."

#this will project the space solid's triangles or nothing will be done. This will be used by the travel distance to find the room's remote point.
def _footprint_polygon(verts, faces): 
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    tris = []
    for i in range(0, len(faces), 3):
        a, b, c = faces[i] * 3, faces[i + 1] * 3, faces[i + 2] * 3
        pa, pb, pc = (verts[a], verts[a + 1]), (verts[b], verts[b + 1]), (verts[c], verts[c + 1])
        if abs((pb[0] - pa[0]) * (pc[1] - pa[1]) - (pc[0] - pa[0]) * (pb[1] - pa[1])) < 1e-6:
            continue
        tris.append(Polygon((pa, pb, pc)))
    if not tris:
        return None
    try:
        footprint = unary_union(tris).buffer(0)
    except Exception:
        return None
    return footprint if (not footprint.is_empty and footprint.area > 0) else None


def space_geometry(space, settings):
    try:
        shape = geom_util.create_shape(settings, space)
        g = shape.geometry
        verts = g.verts
    except Exception:
        return None, None, None
    if not verts:
        return None, None, None
    xs, ys, zs = verts[0::3], verts[1::3], verts[2::3]
    centroid = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        (min(zs) + max(zs)) / 2.0,
    )
    try:
        area = shape_util.get_footprint_area(g)
    except Exception:
        area = None
    footprint = _footprint_polygon(verts, g.faces)
    return centroid, area, footprint


def resolve_storey(space):
    storey = util.get_container(space) or util.get_aggregate(space)
    guard = 0
    while storey is not None and not storey.is_a("IfcBuildingStorey") and guard < 10:
        storey = util.get_aggregate(storey)
        guard += 1
    if storey is not None and storey.is_a("IfcBuildingStorey"):
        return storey
    return None


def _nearest_storey(z, storey_levels):
    if z is None or not storey_levels:
        return None
    return min(storey_levels, key=lambda s: abs(s["elevation_m"] - z))


def space_extract(model, scale, settings, storey_levels):
    results = []
    for space in model.by_type("IfcSpace"):
        psets = util.get_psets(space)
        area = find_property(psets, ["GrossFloorArea", "NetFloorArea", "Area", "FloorArea"])
        occupancy_code = find_property(psets, ["OccupancyType", "OccupancyNumber"])

        raw, geom_area, footprint = space_geometry(space, settings)
        if raw is None: 
            raw = get_position(space, scale)

        if area is not None:
            area_m2 = float(area) * (scale ** 2)
            area_source = "ifc_property"
        elif geom_area is not None:
            area_m2 = geom_area
            area_source = "geometry_footprint"
        else:
            area_m2 = None
            area_source = None

        storey_el = resolve_storey(space)
        centroid = None
        storey = None
        if storey_el is not None:
            elevation_m = (storey_el.Elevation or 0.0) * scale
            storey = {"id": storey_el.GlobalId, "name": storey_el.Name or "Unnamed Storey"}
            if raw is not None:
                gx, gy, gz = raw
                z = elevation_m + gz if gz < elevation_m - 1 else gz
                centroid = (gx, gy, z)
        else:
            if raw is not None:
                centroid = raw
                near = _nearest_storey(raw[2], storey_levels)
                if near is not None:
                    storey = {"id": near["id"], "name": near["name"]}

        results.append({
            "id": space.GlobalId,
            "name": space.Name or "No Room",             
            "long_name": space.LongName or None,         
            "occupancy_code": str(occupancy_code) if occupancy_code is not None else None,
            "area": area_m2,                             
            "area_source": area_source,                  
            "centroid": centroid,                        
            "footprint": footprint,                      
            "storey": storey,                            
        })
    return results

def each_corridor(model, scale):
    corridor = []
    for space in model.by_type("IfcSpace"):
        name = space.Name or ""
        clean_name = name.lower()
        if not any(word in clean_name for word in ["corridor", "hallway", "passage", "lobby", "landing"]):
            continue
        psets = util.get_psets(space)
        width = find_property(psets, ["Width", "ClearWidth"])
        corridor.append({
            "id": space.GlobalId,
            "name": name or "No Corridor",
            "width": float(width) * scale if width is not None else None
        })
    return corridor

def doors(model, scale):
    results = []
    for door in model.by_type("IfcDoor"):
        psets = util.get_psets(door)
        width = door.OverallWidth or find_property(psets, ["Width", "ClearWidth", "OverallWidth"])
        results.append({
            "id": door.GlobalId,
            "name": door.Name or "Standard Door",
            "width_m": float(width) * scale if width else None,
            "is_emergency_exit": emergency_exit(door.Name, psets),
            "position": get_position(door, scale)
        })
    return results

def _element_bbox(element, settings):
    try:
        shape = geom_util.create_shape(settings, element)
        verts = shape.geometry.verts
    except Exception:
        return None
    if not verts:
        return None
    xs, ys, zs = verts[0::3], verts[1::3], verts[2::3]
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))

stair_min_rise_m = 1.0
stair_min_width_m = 0.6

def stairs(model, scale, settings):
    results = []
    for stair in model.by_type("IfcStair"):
        psets = util.get_psets(stair)
        flights = [o for rel in (stair.IsDecomposedBy or []) for o in rel.RelatedObjects
                   if o.is_a("IfcStairFlight")]

        width = find_property(psets, ["Width", "FlightWidth", "StairWidth", "ClearWidth"])
        if width is not None:
            width_m, width_source = float(width) * scale, "ifc_property"
        else:
            flight_widths = [min(d[0], d[1]) for fl in flights
                             if (d := _element_bbox(fl, settings)) is not None]
            if flight_widths:
                width_m, width_source = min(flight_widths), "flight_geometry"
            else:
                width_m, width_source = None, None

        own = _element_bbox(stair, settings)
        if width_m is None and own is not None:
            width_m, width_source = min(own[0], own[1]), "stair_geometry"

        is_egress_stair = bool(flights) or (
            own is not None and own[2] > stair_min_rise_m
            and width_m is not None and width_m >= stair_min_width_m
        )
        if not is_egress_stair:
            continue

        results.append({
            "id": stair.GlobalId,
            "name": stair.Name or "Normal Staircase",
            "width": width_m,
            "width_source": width_source,
            "flight_ids": [f.GlobalId for f in flights],
            "position": get_position(stair, scale)
        })
    return results

def stair_flights(model, scale, settings=None):
    stairs = []
    for flight in model.by_type("IfcStairFlight"):
        psets = util.get_psets(flight)
        width = find_property(psets, ["Width", "ClearWidth", "NominalWidth", "FlightWidth", "StairWidth"])
        going = rise = slope = None
        d = _element_bbox(flight, settings) if settings is not None else None
        if d:
            going, rise = max(d[0], d[1]), d[2]      
            slope = (going ** 2 + rise ** 2) ** 0.5
        stairs.append({
            "id": flight.GlobalId,
            "name": flight.Name or "Stair Flight",
            "width": float(width) * scale if width is not None else None,
            "going_m": going,
            "rise_m": rise,
            "slope_m": slope,
        })
    return stairs

def windows(model, scale):
    results = []
    for window in model.by_type("IfcWindow"):
        psets = util.get_psets(window)
        width = float(window.OverallWidth) * scale if window.OverallWidth else None
        height = float(window.OverallHeight) * scale if window.OverallHeight else None
        area = width * height if width and height else None
        sill_height = find_property(psets, ["SillHeight", "Sill Height", "CillHeight", "Cill Height"])

        results.append({
            "id": window.GlobalId,
            "name": window.Name or "Window",
            "width": width,
            "height": height,
            "area": area,
            "sill_height": float(sill_height) * scale if sill_height is not None else None
        })
    return results

def walls(model, scale):
    results = []
    for wall in model.by_type("IfcWall"):
        psets = util.get_psets(wall)
        is_external = find_property(psets, ["IsExternal", "External", "isExternal"])
        fire_rating = find_property(psets, ["FireRating", "Fire Rating", "FireResistance", "EI", "REI"])
        results.append({
            "id": wall.GlobalId,
            "name": wall.Name or "No wall mentioned",
            "is_external": bool(is_external) if is_external is not None else None,
            "fire_rating": str(fire_rating) if fire_rating is not None else None,
            "position": get_position(wall, scale)
        })
    return results

def slabs(model):
    results = []
    for slab in model.by_type("IfcSlab"):
        psets = util.get_psets(slab)
        slab_type = getattr(slab, "PredefinedType", None)
        fire_rating = find_property(psets, ["FireRating", "Fire Rating", "FireResistance", "REI"])
        results.append({
            "id": slab.GlobalId,
            "name": slab.Name or "No name present",
            "slab_type": str(slab_type).upper() if slab_type else "NOTDEFINED",
            "fire_rating": str(fire_rating) if fire_rating is not None else None
        })
    return results

def space_boundary(model):
    space = []
    for boundary in model.by_type("IfcRelSpaceBoundary"):
        related_space = boundary.RelatingSpace
        related_element = boundary.RelatedBuildingElement
        if related_space is None or related_element is None:
            continue
        space.append({
            "space_id": related_space.GlobalId,
            "space_name": related_space.Name or "Unnamed Space",
            "element_id": related_element.GlobalId,
            "element_type": related_element.is_a(),
            "element_name": related_element.Name or "Unnamed Element"
        })
    return space

def door_space_links(model):
    wall_spaces = defaultdict(set)
    door_spaces = defaultdict(set)
    for boundary in model.by_type("IfcRelSpaceBoundary"):
        element = boundary.RelatedBuildingElement
        space = boundary.RelatingSpace
        if element is None or space is None:
            continue
        if element.is_a("IfcWall"):
            wall_spaces[element.GlobalId].add(space.GlobalId)
        elif element.is_a("IfcDoor"):
            door_spaces[element.GlobalId].add(space.GlobalId)

    for door in model.by_type("IfcDoor"):
        spaces = set(door_spaces.get(door.GlobalId, set()))
        for fills in (door.FillsVoids or []):
            opening = fills.RelatingOpeningElement
            for voids in (opening.VoidsElements or []):
                host = voids.RelatingBuildingElement
                if host is not None and host.is_a("IfcWall"):
                    spaces |= wall_spaces.get(host.GlobalId, set())
        if spaces:
            door_spaces[door.GlobalId] = spaces

    return {door_id: sorted(spaces) for door_id, spaces in door_spaces.items() if spaces}

def connected_elements(model):
    elements = []
    for connection in model.by_type("IfcRelConnectsElements"):
        element_a = connection.RelatingElement
        element_b = connection.RelatedElement
        if element_a is None or element_b is None:
            continue

        elements.append({
            "element_a_id": element_a.GlobalId,
            "element_a_type": element_a.is_a(),
            "element_a_name": element_a.Name or "Unnamed",
            "element_b_id": element_b.GlobalId,
            "element_b_type": element_b.is_a(),
            "element_b_name": element_b.Name or "Unnamed"
        })
    return elements

def transport_elements(model, scale):
    results = []
    for element in model.by_type("IfcTransportElement"):
        psets = util.get_psets(element)
        predefined_type = getattr(element, "PredefinedType", None)
        type_str = str(predefined_type).upper() if predefined_type else "NOTDEFINED"
        name_lower = (element.Name or "").lower()

        if "elevator" in type_str or "lift" in type_str or "elevator" in name_lower or "lift" in name_lower:
            category = "elevator"
        elif "moving" in type_str or "travelator" in name_lower or "walkway" in name_lower:
            category = "moving_walkway"
        else:
            category = "other"

        results.append({
            "id": element.GlobalId,
            "name": element.Name or "No Transport Element",
            "category": category,
            "predefined_type": type_str,
            "is_evac_lift": possible_evacuation_lifts(element.Name, psets),
            "position": get_position(element, scale)
        })
    return results


def storeys(model, scale):
    storeys_list = model.by_type("IfcBuildingStorey")
    if not storeys_list:
        return []

    entrance_elevation = None
    for storey in storeys_list:
        psets = util.get_psets(storey)
        for pset in psets.values():
            if pset.get("EntranceLevel") is True:
                entrance_elevation = storey.Elevation if storey.Elevation is not None else 0.0

    ground_reference_found = entrance_elevation is not None
    if entrance_elevation is None:
        elevations = [s.Elevation for s in storeys_list if s.Elevation is not None]
        entrance_elevation = min(elevations) if elevations else 0.0

    results = []
    for storey in storeys_list:
        raw_elevation = storey.Elevation if storey.Elevation is not None else entrance_elevation
        results.append({
            "id": storey.GlobalId,
            "name": storey.Name or "Unnamed Storey",
            "elevation_m": raw_elevation * scale,                         
            "height_above_ground_m": (raw_elevation - entrance_elevation) * scale,
            "ground_reference_found": ground_reference_found
        })
    return results


def smoke_alarms(model, scale):
    alarm = []
    for element in model.by_type("IfcAlarm"):
        alarm.append({
            "id": element.GlobalId,
            "name": element.Name or "Smoke Alarm",
            "position": get_position(element, scale)
        })
    return alarm


#def fire_terminals(model):
    return [
        {"id": t.GlobalId, "name": t.Name or "Fire Suppression Terminal"}
        for t in model.by_type("IfcFireSuppressionTerminal")
    ]


def storey_levels(model, scale):
    return [
        {"id": s.GlobalId, "name": s.Name or "Unnamed Storey",
         "elevation_m": (s.Elevation or 0.0) * scale}
        for s in model.by_type("IfcBuildingStorey")
    ]


# --- space-frame reconciliation --------------------------------------------------------------------
# Some exporters lose the placement transform on a subset of IfcSpaces: the space geometry comes back
# in the raw authoring frame while doors, walls and stairs keep correct world placements. The two then
# disagree by a rigid transform, no door touches any room, and every egress path disappears.
#
# ``door_space_links`` states which doors bound which rooms from topology alone (IfcRelSpaceBoundary +
# hosting wall), independent of any coordinate. That gives correspondences to *solve* the transform
# from rather than assuming an angle: a global sweep of the single rotational degree of freedom, then
# trimmed ICP against the room outlines to converge it. No angle or offset is hardcoded, so a model
# tilted by any amount is handled. It is done per storey (the loss is per-storey in practice) and
# adopted only when it measurably improves door-to-room agreement, so a well-formed model is left
# untouched -- which is what makes it safe to run on every file.

FRAME_OK_M = 0.5            
FRAME_ADOPT_M = 1.5         
FRAME_GAIN = 10.0           
FRAME_MIN_PAIRS = 5         
FRAME_INLIER_M = 0.5        
FRAME_SWEEP_DEG = 1.0       
FRAME_TRIM_M = 2.0          
FRAME_ICP_MAX_STEPS = 60    
FRAME_ICP_SHIFT_M = 1e-4    
FRAME_ICP_ANGLE_RAD = 1e-5


def _rigid_2d(pairs):
    if not pairs:
        return None
    n = len(pairs)
    sx = sum(p[0][0] for p in pairs) / n
    sy = sum(p[0][1] for p in pairs) / n
    tx = sum(p[1][0] for p in pairs) / n
    ty = sum(p[1][1] for p in pairs) / n
    num = den = 0.0
    for (a, b) in pairs:
        px, py = a[0] - sx, a[1] - sy
        qx, qy = b[0] - tx, b[1] - ty
        num += px * qy - py * qx
        den += px * qx + py * qy
    if abs(num) < 1e-12 and abs(den) < 1e-12:
        return None
    theta = math.atan2(num, den)
    c, s = math.cos(theta), math.sin(theta)
    return (c, s, tx - (c * sx - s * sy), ty - (s * sx + c * sy))


def _apply_xy(transform, x, y):
    c, s, dx, dy = transform
    return (c * x - s * y + dx, s * x + c * y + dy)


def _invert_2d(transform):
    c, s, dx, dy = transform
    return (c, -s, -(c * dx + s * dy), -(-s * dx + c * dy))


def _affine_matrix(transform):
    c, s, dx, dy = transform
    return [c, -s, s, c, dx, dy]


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _frame_gaps(entries, transform=None):
    from shapely.geometry import Point

    inverse = _invert_2d(transform) if transform is not None else None
    gaps = []
    for polygon, (dx, dy) in entries:
        x, y = (dx, dy) if inverse is None else _apply_xy(inverse, dx, dy)
        gaps.append(polygon.distance(Point(x, y)))
    return gaps


def _frame_agreement(entries, transform=None):
    gaps = _frame_gaps(entries, transform)
    return sum(1 for g in gaps if g <= FRAME_INLIER_M), _median(gaps)


def _sweep_rotation(entries):
    centroids = [(p.centroid.x, p.centroid.y) for p, _ in entries]
    targets = [d for _, d in entries]
    best = None
    steps = int(round(360.0 / FRAME_SWEEP_DEG))
    for step in range(steps):
        theta = math.radians(step * FRAME_SWEEP_DEG)
        c, s = math.cos(theta), math.sin(theta)
        dx = _median([t[0] - (c * cx - s * cy) for (cx, cy), t in zip(centroids, targets)])
        dy = _median([t[1] - (s * cx + c * cy) for (cx, cy), t in zip(centroids, targets)])
        transform = (c, s, dx, dy)
        score = _frame_agreement(entries, transform)
        if best is None or (score[0], -score[1]) > (best[1][0], -best[1][1]):
            best = (transform, score)
    return best[0] if best else None


def _trimmed_icp(entries, transform):
    from shapely.geometry import Point
    from shapely.ops import nearest_points

    steps = 0
    for steps in range(1, FRAME_ICP_MAX_STEPS + 1):
        gaps = _frame_gaps(entries, transform)
        keep = [i for i, g in enumerate(gaps) if g <= FRAME_TRIM_M]
        if len(keep) < FRAME_MIN_PAIRS:
            keep = sorted(range(len(gaps)), key=lambda i: gaps[i])[:FRAME_MIN_PAIRS]
        inverse = _invert_2d(transform)
        refined = []
        for i in keep:
            polygon, door_xy = entries[i]
            bx, by = _apply_xy(inverse, door_xy[0], door_xy[1])
            on_outline = nearest_points(polygon.boundary, Point(bx, by))[0]
            refined.append(((on_outline.x, on_outline.y), door_xy))
        stepped = _rigid_2d(refined)
        if stepped is None:
            break
        shift = math.hypot(stepped[2] - transform[2], stepped[3] - transform[3])
        turn = abs(math.atan2(stepped[1], stepped[0]) - math.atan2(transform[1], transform[0]))
        transform = stepped
        if shift < FRAME_ICP_SHIFT_M and turn < FRAME_ICP_ANGLE_RAD:
            break
    return transform, steps


def reconcile_space_frame(spaces, doors, links, storeys):
    try:
        from shapely.affinity import affine_transform
    except Exception:
        return []

    door_position = {d["id"]: d["position"] for d in doors if d.get("position") is not None}
    space_by_id = {s["id"]: s for s in spaces}
    storey_name = {st["id"]: st.get("name") for st in storeys}

    by_storey = defaultdict(list)
    for space in spaces:
        storey_id = (space.get("storey") or {}).get("id")
        if storey_id is not None:
            by_storey[storey_id].append(space)

    entries_by_storey = {}
    for storey_id in by_storey:
        entries = []
        for door_id, bounded in links.items():
            position = door_position.get(door_id)
            if position is None:
                continue
            for gid in bounded:
                space = space_by_id.get(gid)
                if space is None or space.get("footprint") is None:
                    continue
                if (space.get("storey") or {}).get("id") != storey_id:
                    continue
                entries.append((space["footprint"], (position[0], position[1])))
        entries_by_storey[storey_id] = entries

    rows = {}
    needs = []
    for storey_id, entries in entries_by_storey.items():
        row = {"storey": storey_id, "storey_name": storey_name.get(storey_id),
               "pairs": len(entries), "corrected": False, "rotation_deg": None,
               "translation_m": None, "median_gap_before_m": None, "median_gap_after_m": None,
               "doors_on_their_room_before": None, "doors_on_their_room_after": None, "note": None}
        rows[storey_id] = row
        if len(entries) < FRAME_MIN_PAIRS:
            row["note"] = "too few door-to-room correspondences to fit a frame"
            continue
        inliers, median_before = _frame_agreement(entries)
        row["median_gap_before_m"] = round(median_before, 3)
        row["doors_on_their_room_before"] = inliers
        if median_before <= FRAME_OK_M:
            row["note"] = "space and door frames already agree"
            continue
        needs.append(storey_id)

    candidates = []
    for storey_id in needs:
        seed = _sweep_rotation(entries_by_storey[storey_id])
        if seed is not None:
            candidates.append(_trimmed_icp(entries_by_storey[storey_id], seed)[0])
    if len(needs) > 1:
        pooled = [e for storey_id in needs for e in entries_by_storey[storey_id]]
        seed = _sweep_rotation(pooled)
        if seed is not None:
            candidates.append(_trimmed_icp(pooled, seed)[0])

    for storey_id in needs:
        entries, row = entries_by_storey[storey_id], rows[storey_id]
        median_before = row["median_gap_before_m"]
        if not candidates:
            row["note"] = "correspondences are degenerate; no transform fitted"
            continue
        transform = max(candidates, key=lambda t: (lambda a: (a[0], -a[1]))(
            _frame_agreement(entries, t)))
        inliers_after, median_after = _frame_agreement(entries, transform)
        row["median_gap_after_m"] = round(median_after, 3)
        row["doors_on_their_room_after"] = inliers_after

        if not (median_after <= FRAME_ADOPT_M
                and median_after * FRAME_GAIN <= median_before
                and inliers_after > (row["doors_on_their_room_before"] or 0)):
            row["note"] = (f"no fitted frame explained this storey (median {median_before:.2f} m -> "
                           f"{median_after:.2f} m, doors on their room "
                           f"{row['doors_on_their_room_before']} -> {inliers_after} of "
                           f"{len(entries)}); storey left as parsed")
            continue

        matrix = _affine_matrix(transform)
        for space in by_storey[storey_id]:
            if space.get("footprint") is not None:
                space["footprint"] = affine_transform(space["footprint"], matrix)
            centroid = space.get("centroid")
            if centroid is not None:
                x, y = _apply_xy(transform, centroid[0], centroid[1])
                space["centroid"] = (x, y, centroid[2])

        c, s, dx, dy = transform
        row.update({
            "corrected": True,
            "rotation_deg": round(math.degrees(math.atan2(s, c)), 3),
            "translation_m": [round(dx, 3), round(dy, 3)],
            "note": (f"space geometry re-seated onto the door frame; median door-to-room gap "
                     f"{median_before:.2f} m -> {median_after:.2f} m, doors landing on the room "
                     f"they bound {row['doors_on_their_room_before']} -> {inliers_after} of "
                     f"{len(entries)}"),
        })

    return list(rows.values())


OVERLAY_COVERED_SHARE = 0.9   


def _is_overlay_of_a_connected_room(space, connected):
    area = space["footprint"].area
    if area <= 0:
        return True
    storey_id = (space.get("storey") or {}).get("id")
    for other in connected:
        if (other.get("storey") or {}).get("id") != storey_id:
            continue
        if other["footprint"].intersection(space["footprint"]).area >= OVERLAY_COVERED_SHARE * area:
            return True
    return False


def augment_door_space_links(links, spaces, doors, levels, tolerance_m=0.3):
    try:
        from shapely.geometry import Point
    except Exception:
        return links, 0

    linked = {gid for bounded in links.values() for gid in bounded}
    orphans = [s for s in spaces
               if s["id"] not in linked and s.get("footprint") is not None and s.get("storey")]
    connected = [s for s in spaces
                 if s["id"] in linked and s.get("footprint") is not None and s.get("storey")]
    orphans = [s for s in orphans if not _is_overlay_of_a_connected_room(s, connected)]
    if not orphans:
        return links, 0

    reach = {s["id"]: s["footprint"].buffer(tolerance_m) for s in orphans}
    augmented = {door_id: list(bounded) for door_id, bounded in links.items()}
    added = 0
    for door in doors:
        position = door.get("position")
        if position is None:
            continue
        near = _nearest_storey(position[2], levels)
        if near is None:
            continue
        point = Point(position[0], position[1])
        for space in orphans:
            if (space.get("storey") or {}).get("id") != near["id"]:
                continue
            if reach[space["id"]].contains(point):
                bounded = augmented.setdefault(door["id"], [])
                if space["id"] not in bounded:
                    bounded.append(space["id"])
                    added += 1
    return {door_id: sorted(bounded) for door_id, bounded in augmented.items() if bounded}, added


def parser_summary(ifc_path):
    model = ifcopenshell.open(ifc_path)
    scale = unit_util.calculate_unit_scale(model)  
    geom_settings = geom_util.settings()           
    world_settings = geom_util.settings()
    world_settings.set("use-world-coords", True)
    levels = storey_levels(model, scale)

    all_doors = doors(model, scale)
    all_transport = transport_elements(model, scale)

    all_spaces = space_extract(model, scale, world_settings, levels)
    all_storeys = storeys(model, scale)
    links = door_space_links(model)
    space_frame = reconcile_space_frame(all_spaces, all_doors, links, all_storeys)
    links, links_added = augment_door_space_links(links, all_spaces, all_doors, levels)

    return {
        "project": load_project_name(model),
        "spaces": all_spaces,
        "corridors": each_corridor(model, scale),
        "doors": all_doors,
        "stairs": stairs(model, scale, geom_settings),
        "stair_flights": stair_flights(model, scale, geom_settings),
        "windows": windows(model, scale),
        "walls": walls(model, scale),
        "slabs": slabs(model),
        "storeys": all_storeys,
        "smoke_alarms": smoke_alarms(model, scale),
        #"fire_suppression_terminals": fire_terminals(model),
        "space_boundaries": space_boundary(model),
        "door_space_links": links,
        "space_frame": space_frame,
        "door_links_added_geometrically": links_added,
        "connected_elements": connected_elements(model),
        "elevators": [t for t in all_transport if t["category"] == "elevator"],
        "emergency_exits": [d for d in all_doors if d["is_emergency_exit"]]
    }
if __name__ == "__main__":

    ifc_path = resolve_ifc(sys.argv)
    report = parser_summary(ifc_path)

    print("Project Name:", report["project"])
    print("Total Spaces:", len(report["spaces"]))
    print("Total Corridors:", len(report["corridors"]))
    print("Total Doors:", len(report["doors"]))
    print("Total Exits:", len(report["emergency_exits"]))
    print("Total Stairs:", len(report["stairs"]))
    print("Total Stair Flights:", len(report["stair_flights"]))
    print("Total Windows:", len(report["windows"]))
    print("Total Walls:", len(report["walls"]))
    print("Total Slabs:", len(report["slabs"]))
    print("Total Storeys:", len(report["storeys"]))
    print("Total Smoke Alarms:", len(report["smoke_alarms"]))
    #print("Total Fire Suppression Terminals:", len(report["fire_suppression_terminals"]))
    print("Total Elevators:", len(report["elevators"]))

    spaces = report["spaces"]
    with_long = sum(1 for s in spaces if s["long_name"])
    with_centroid = sum(1 for s in spaces if s["centroid"])
    with_storey = sum(1 for s in spaces if s["storey"])
    with_area = sum(1 for s in spaces if s["area"] is not None)
    print(f"\nSpaces with LongName: {with_long}/{len(spaces)}")
    print(f"Spaces with centroid: {with_centroid}/{len(spaces)}")
    print(f"Spaces with storey: {with_storey}/{len(spaces)}")
    print(f"Spaces with area: {with_area}/{len(spaces)}")
    print("\nSample spaces:")
    for s in spaces[:8]:
        c = s["centroid"]
        c_str = f"({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f})" if c else "None"
        st = s["storey"]["name"] if s["storey"] else "None"
        area = f"{s['area']:.1f}" if s["area"] is not None else "None"
        print(f"  long_name={s['long_name']!r:16} area={area:>6} ({s['area_source']}) "
              f"storey={st!r:12} centroid={c_str}")