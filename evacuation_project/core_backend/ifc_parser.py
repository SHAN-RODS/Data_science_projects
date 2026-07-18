import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.unit as unit_util
import ifcopenshell.util.placement as placement_util
import ifcopenshell.geom as geom_util
import ifcopenshell.util.shape as shape_util
from collections import defaultdict
import sys

def find_property(psets, keys):
    # different authoring tools (Revit/ArchiCAD/Tekla) name the same property differently
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
        # placement matrix is 4x4, last column is the XYZ translation
        matrix = placement_util.get_local_placement(element_name.ObjectPlacement)
        return (matrix[0][3] * scale, matrix[1][3] * scale, matrix[2][3] * scale)
    except Exception:
        return None

def load_project_name(model):
    project = model.by_type("IfcProject")
    if project:
        return project[0].Name or "No Name"
    return "No IFC project name found."

def space_geometry(space, settings):
    # Derive (centroid, footprint_area) from the space solid in one shape build.
    #   centroid: bounding-box centre (x, y, z) in metres, or None
    #   footprint_area: horizontal floor area in m^2, or None
    # IfcOpenShell's mesher returns vertices already in metres, so no unit scaling is applied.
    # The world-Z of aggregated spaces is corrected in space_extract using the storey elevation
    # (some Revit exports author space geometry at local z ~ 0). Keep the shape reference alive
    # while reading .verts/.geometry — the buffers are C++-owned and freed when it is collected.
    try:
        shape = geom_util.create_shape(settings, space)
        g = shape.geometry
        verts = g.verts
    except Exception:
        return None, None
    if not verts:
        return None, None
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
    return centroid, area


def resolve_storey(space):
    # The IfcBuildingStorey a space belongs to. Spaces link to storeys either by spatial
    # containment (IfcRelContainedInSpatialStructure) or aggregation (IfcRelAggregates); this
    # tries both and walks up until it reaches a storey.
    storey = util.get_container(space) or util.get_aggregate(space)
    guard = 0
    while storey is not None and not storey.is_a("IfcBuildingStorey") and guard < 10:
        storey = util.get_aggregate(storey)
        guard += 1
    if storey is not None and storey.is_a("IfcBuildingStorey"):
        return storey
    return None


def _nearest_storey(z, storey_levels):
    # Fallback storey assignment by elevation for spaces with no containment/aggregation link.
    if z is None or not storey_levels:
        return None
    return min(storey_levels, key=lambda s: abs(s["elevation_m"] - z))


def space_extract(model, scale, settings, storey_levels):
    results = []
    for space in model.by_type("IfcSpace"):
        psets = util.get_psets(space)
        area = find_property(psets, ["GrossFloorArea", "NetFloorArea", "Area", "FloorArea"])

        raw, geom_area = space_geometry(space, settings)
        if raw is None:
            # no usable geometry — fall back to the placement origin (world metres)
            raw = get_position(space, scale)

        # Prefer an IFC-declared area; otherwise derive the floor area from geometry. Record which
        # so downstream occupant-load estimates stay traceable to a source or a stated method.
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
                # correct world-Z only when the geometry sits below its floor (authored at ~0)
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
            "name": space.Name or "No Room",             # raw name (kept verbatim for the classifier)
            "long_name": space.LongName or None,         # the semantically clean label in most exports
            "area": area_m2,                             # m^2, or None
            "area_source": area_source,                  # "ifc_property" | "geometry_footprint" | None
            "centroid": centroid,                        # (x, y, z) world metres, or None
            "storey": storey,                            # {"id", "name"} or None
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

def stairs(model, scale):
    results = []
    for stair in model.by_type("IfcStair"):
        psets = util.get_psets(stair)
        width = find_property(psets, ["Width", "FlightWidth", "StairWidth", "ClearWidth"])
        results.append({
            "id": stair.GlobalId,
            "name": stair.Name or "Normal Staircase",
            "width": float(width) * scale if width is not None else None,
            "position": get_position(stair, scale)
        })
    return results

def stair_flights(model, scale):
    stairs = []
    for flight in model.by_type("IfcStairFlight"):
        psets = util.get_psets(flight)
        width = find_property(psets, ["Width", "ClearWidth", "NominalWidth", "FlightWidth", "StairWidth"])
        stairs.append({
            "id": flight.GlobalId,
            "name": flight.Name or "Stair Flight",
            "width": float(width) * scale if width is not None else None
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
    # Which spaces each door connects. Two sources, combined:
    #   1. doors that are themselves IfcRelSpaceBoundary elements (some exports model this directly)
    #   2. doors recovered via their hosting wall: a door fills an opening (IfcRelFillsElement) that
    #      voids a wall (IfcRelVoidsElement); that wall's space boundaries are the rooms it connects.
    # Real exports are patchy about (1), so (2) restores most connectivity. A shared wall is only
    # traversable when it hosts a door -- this keeps "wall = adjacency, door = traversable" intact.
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
        # nothing tagged as the entrance level, so just use the lowest storey instead
        elevations = [s.Elevation for s in storeys_list if s.Elevation is not None]
        entrance_elevation = min(elevations) if elevations else 0.0

    results = []
    for storey in storeys_list:
        raw_elevation = storey.Elevation if storey.Elevation is not None else entrance_elevation
        results.append({
            "id": storey.GlobalId,
            "name": storey.Name or "Unnamed Storey",
            "elevation_m": raw_elevation * scale,                         # world Z, for matching centroids/exits
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


def fire_terminals(model):
    return [
        {"id": t.GlobalId, "name": t.Name or "Fire Suppression Terminal"}
        for t in model.by_type("IfcFireSuppressionTerminal")
    ]


def storey_levels(model, scale):
    # lightweight (name, id, elevation) list used to assign a storey to spaces that lack a
    # containment/aggregation link (see _nearest_storey)
    return [
        {"id": s.GlobalId, "name": s.Name or "Unnamed Storey",
         "elevation_m": (s.Elevation or 0.0) * scale}
        for s in model.by_type("IfcBuildingStorey")
    ]


def parser_summary(ifc_path):
    model = ifcopenshell.open(ifc_path)
    scale = unit_util.calculate_unit_scale(model)  # handles mm vs m unit differences between models
    geom_settings = geom_util.settings()           # default settings; verts come back in metres
    levels = storey_levels(model, scale)
    all_doors = doors(model, scale)
    all_transport = transport_elements(model, scale)

    return {
        "project": load_project_name(model),
        "spaces": space_extract(model, scale, geom_settings, levels),
        "corridors": each_corridor(model, scale),
        "doors": all_doors,
        "stairs": stairs(model, scale),
        "stair_flights": stair_flights(model, scale),
        "windows": windows(model, scale),
        "walls": walls(model, scale),
        "slabs": slabs(model),
        "storeys": storeys(model, scale),
        "smoke_alarms": smoke_alarms(model, scale),
        "fire_suppression_terminals": fire_terminals(model),
        "space_boundaries": space_boundary(model),
        "door_space_links": door_space_links(model),
        "connected_elements": connected_elements(model),
        "elevators": [t for t in all_transport if t["category"] == "elevator"],
        "emergency_exits": [d for d in all_doors if d["is_emergency_exit"]]
    }
if __name__ == "__main__":
    from core_backend.sample_paths import resolve_ifc

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
    print("Total Fire Suppression Terminals:", len(report["fire_suppression_terminals"]))
    print("Total Elevators:", len(report["elevators"]))

    # Phase 1 additions: LongName, centroid, storey coverage
    spaces = report["spaces"]
    with_long = sum(1 for s in spaces if s["long_name"])
    with_centroid = sum(1 for s in spaces if s["centroid"])
    with_storey = sum(1 for s in spaces if s["storey"])
    with_area = sum(1 for s in spaces if s["area"] is not None)
    print(f"\nSpaces with LongName: {with_long}/{len(spaces)}")
    print(f"Spaces with centroid: {with_centroid}/{len(spaces)}")
    print(f"Spaces with storey:   {with_storey}/{len(spaces)}")
    print(f"Spaces with area:     {with_area}/{len(spaces)}")
    print("\nSample spaces:")
    for s in spaces[:8]:
        c = s["centroid"]
        c_str = f"({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f})" if c else "None"
        st = s["storey"]["name"] if s["storey"] else "None"
        area = f"{s['area']:.1f}" if s["area"] is not None else "None"
        print(f"  long_name={s['long_name']!r:16} area={area:>6} ({s['area_source']}) "
              f"storey={st!r:12} centroid={c_str}")