# IFC parsing — extracts evacuation-relevant elements from a residential BIM model
# for use in UK fire safety regulation checking (England, Wales, Northern Ireland,
# Scotland).
#
# IMPORTANT: IFC files store every length as a raw number in whatever unit the
# project declares (often millimetres for Revit/ArchiCAD exports, sometimes
# metres). ifcopenshell does NOT convert this automatically — every raw length
# read from the file must be multiplied by the project's unit scale factor to
# get real metres, or regulation thresholds (all expressed in metres/mm) will
# be compared against the wrong magnitude and every check becomes meaningless.

import ifcopenshell
import ifcopenshell.util.element as util
import ifcopenshell.util.placement as placement_util
import ifcopenshell.util.unit as unit_util


def _pset_value(psets, keys):
    for pset in psets.values():
        for key in keys:
            if key in pset and pset[key] is not None:
                return pset[key]
    return None


def _is_emergency_exit(name, psets):
    clean_name = (name or "").lower()
    if any(word in clean_name for word in ["exit", "escape", "emergency"]):
        return True

    for pset in psets.values():
        for key, value in pset.items():
            if any(word in str(key).lower() or word in str(value).lower()
                   for word in ["exit", "escape", "emergency"]):
                return True
    return False


def _is_evacuation_lift(name, psets):
    evac_words = ["evacuation", "firefighter", "fire lift", "fire-rated lift", "evac lift"]
    name_lower = (name or "").lower()

    if any(word in name_lower for word in evac_words):
        return True

    for pset in psets.values():
        for key, value in pset.items():
            if any(word in str(key).lower() or word in str(value).lower() for word in evac_words):
                return True
    return False


# Returns the element's global XYZ position in metres, or None if it has no
# placement. Used only for rough proximity checks (e.g. "is a smoke alarm
# within 7.5m of this door") — this is a straight-line distance, not a real
# walking/travel distance, and is documented as an approximation.
def _position_m(element, scale):
    if getattr(element, "ObjectPlacement", None) is None:
        return None
    try:
        matrix = placement_util.get_local_placement(element.ObjectPlacement)
        return (matrix[0][3] * scale, matrix[1][3] * scale, matrix[2][3] * scale)
    except Exception:
        return None


def _project_name(model):
    project = model.by_type("IfcProject")
    if project:
        return project[0].Name or "Unnamed Project"
    return "No Project Name Found in the IFC file"


def _spaces(model, scale):
    result = []
    for space in model.by_type("IfcSpace"):
        psets = util.get_psets(space)
        area = _pset_value(psets, ["GrossFloorArea", "NetFloorArea", "Area", "FloorArea"])
        result.append({
            "id": space.GlobalId,
            "name": space.Name or "No Room",
            "area": float(area) * (scale ** 2) if area is not None else None
        })
    return result


def _corridors(model, scale):
    result = []
    for space in model.by_type("IfcSpace"):
        name = space.Name or ""
        clean_name = name.lower()
        if any(word in clean_name for word in ["corridor", "hallway", "passage", "lobby", "landing"]):
            psets = util.get_psets(space)
            width = _pset_value(psets, ["Width", "ClearWidth"])
            result.append({
                "id": space.GlobalId,
                "name": name or "No Corridor",
                "width": float(width) * scale if width is not None else None
            })
    return result


def _doors(model, scale):
    result = []
    for door in model.by_type("IfcDoor"):
        psets = util.get_psets(door)
        width = door.OverallWidth or _pset_value(psets, ["Width", "ClearWidth", "OverallWidth"])
        result.append({
            "id": door.GlobalId,
            "name": door.Name or "Standard Door",
            "width_m": float(width) * scale if width else None,
            "is_emergency_exit": _is_emergency_exit(door.Name, psets),
            "position": _position_m(door, scale)
        })
    return result


def _stairs(model, scale):
    result = []
    for stair in model.by_type("IfcStair"):
        psets = util.get_psets(stair)
        width = _pset_value(psets, ["Width", "FlightWidth", "StairWidth", "ClearWidth"])
        result.append({
            "id": stair.GlobalId,
            "name": stair.Name or "Normal Staircase",
            "width": float(width) * scale if width is not None else None,
            "position": _position_m(stair, scale)
        })
    return result


# IfcStairFlight often carries width where IfcStair itself doesn't — some
# jurisdiction rules (e.g. Wales escape stair width) reference it directly.
def _stair_flights(model, scale):
    result = []
    for flight in model.by_type("IfcStairFlight"):
        psets = util.get_psets(flight)
        width = _pset_value(psets, ["Width", "ClearWidth", "NominalWidth", "FlightWidth", "StairWidth"])
        result.append({
            "id": flight.GlobalId,
            "name": flight.Name or "Stair Flight",
            "width": float(width) * scale if width is not None else None
        })
    return result


def _windows(model, scale):
    result = []
    for window in model.by_type("IfcWindow"):
        psets = util.get_psets(window)
        width = float(window.OverallWidth) * scale if window.OverallWidth else None
        height = float(window.OverallHeight) * scale if window.OverallHeight else None
        area = width * height if width and height else None

        # Sill height (floor to bottom of opening) — needed for Wales/NI
        # window-height regulations. Rarely a native attribute; only
        # available if the authoring tool stored it as a property.
        sill_height = _pset_value(psets, ["SillHeight", "Sill Height", "CillHeight", "Cill Height"])

        result.append({
            "id": window.GlobalId,
            "name": window.Name or "Window",
            "width": width,
            "height": height,
            "area": area,
            "sill_height": float(sill_height) * scale if sill_height is not None else None
        })
    return result


def _walls(model, scale):
    result = []
    for wall in model.by_type("IfcWall"):
        psets = util.get_psets(wall)
        is_external = _pset_value(psets, ["IsExternal", "External", "isExternal"])
        fire_rating = _pset_value(psets, ["FireRating", "Fire Rating", "FireResistance", "EI", "REI"])
        result.append({
            "id": wall.GlobalId,
            "name": wall.Name or "No wall mentioned",
            "is_external": bool(is_external) if is_external is not None else None,
            "fire_rating": str(fire_rating) if fire_rating is not None else None,
            "position": _position_m(wall, scale)
        })
    return result


def _slabs(model):
    result = []
    for slab in model.by_type("IfcSlab"):
        psets = util.get_psets(slab)
        slab_type = getattr(slab, "PredefinedType", None)
        fire_rating = _pset_value(psets, ["FireRating", "Fire Rating", "FireResistance", "REI"])
        result.append({
            "id": slab.GlobalId,
            "name": slab.Name or "No name present",
            "slab_type": str(slab_type).upper() if slab_type else "NOTDEFINED",
            "fire_rating": str(fire_rating) if fire_rating is not None else None
        })
    return result


def _space_boundaries(model):
    result = []
    for boundary in model.by_type("IfcRelSpaceBoundary"):
        related_space = boundary.RelatingSpace
        related_element = boundary.RelatedBuildingElement

        if related_space is None or related_element is None:
            continue

        result.append({
            "space_id": related_space.GlobalId,
            "space_name": related_space.Name or "Unnamed Space",
            "element_id": related_element.GlobalId,
            "element_type": related_element.is_a(),
            "element_name": related_element.Name or "Unnamed Element"
        })
    return result


def _connected_elements(model):
    result = []
    for connection in model.by_type("IfcRelConnectsElements"):
        element_a = connection.RelatingElement
        element_b = connection.RelatedElement

        if element_a is None or element_b is None:
            continue

        result.append({
            "element_a_id": element_a.GlobalId,
            "element_a_type": element_a.is_a(),
            "element_a_name": element_a.Name or "Unnamed",
            "element_b_id": element_b.GlobalId,
            "element_b_type": element_b.is_a(),
            "element_b_name": element_b.Name or "Unnamed"
        })
    return result


def _transport_elements(model, scale):
    result = []
    for element in model.by_type("IfcTransportElement"):
        psets = util.get_psets(element)
        predefined_type = getattr(element, "PredefinedType", None)
        type_str = str(predefined_type).upper() if predefined_type else "NOTDEFINED"
        name_lower = (element.Name or "").lower()

        if "elevator" in type_str or "lift" in type_str or \
           "elevator" in name_lower or "lift" in name_lower:
            category = "elevator"
        elif "escalator" in type_str or "escalator" in name_lower:
            category = "escalator"
        elif "moving" in type_str or "travelator" in name_lower or "walkway" in name_lower:
            category = "moving_walkway"
        else:
            category = "other"

        result.append({
            "id": element.GlobalId,
            "name": element.Name or "Unnamed Transport Element",
            "category": category,
            "predefined_type": type_str,
            "is_evac_lift": _is_evacuation_lift(element.Name, psets),
            "position": _position_m(element, scale)
        })
    return result


# Storey height above "ground level" is central to several UK regulations
# (England, Wales, NI and Scotland all gate requirements on height above
# ground). Raw IfcBuildingStorey.Elevation is usually relative to an
# arbitrary project/geodetic datum, not ground level, so the storey marked
# EntranceLevel=True in Pset_BuildingStoreyCommon (a standard IFC property)
# is used as the ground reference where available. If no storey declares an
# entrance level, height_above_ground_m falls back to elevation relative to
# the lowest storey, which is a rougher approximation — flagged accordingly.
def _storeys(model, scale):
    storeys_raw = model.by_type("IfcBuildingStorey")
    if not storeys_raw:
        return []

    entrance_elevation = None
    for storey in storeys_raw:
        psets = util.get_psets(storey)
        for pset in psets.values():
            if pset.get("EntranceLevel") is True:
                entrance_elevation = storey.Elevation if storey.Elevation is not None else 0.0

    ground_reference_found = entrance_elevation is not None
    if entrance_elevation is None:
        elevations = [s.Elevation for s in storeys_raw if s.Elevation is not None]
        entrance_elevation = min(elevations) if elevations else 0.0

    result = []
    for storey in storeys_raw:
        raw_elevation = storey.Elevation if storey.Elevation is not None else entrance_elevation
        result.append({
            "id": storey.GlobalId,
            "name": storey.Name or "Unnamed Storey",
            "height_above_ground_m": (raw_elevation - entrance_elevation) * scale,
            "ground_reference_found": ground_reference_found
        })
    return result


def _smoke_alarms(model, scale):
    result = []
    for alarm in model.by_type("IfcAlarm"):
        result.append({
            "id": alarm.GlobalId,
            "name": alarm.Name or "Smoke Alarm",
            "position": _position_m(alarm, scale)
        })
    return result


def _fire_suppression_terminals(model):
    result = []
    for terminal in model.by_type("IfcFireSuppressionTerminal"):
        result.append({
            "id": terminal.GlobalId,
            "name": terminal.Name or "Fire Suppression Terminal"
        })
    return result


def get_summary(ifc_path):
    """
    Parses an IFC file and returns every evacuation-relevant element as a
    single dict, with all lengths/areas already converted to metres/sqm
    regardless of the file's declared unit. This is the one entry point
    the rest of the pipeline (regulation checking, scenario generation,
    the Streamlit UI) calls.
    """
    model = ifcopenshell.open(ifc_path)
    scale = unit_util.calculate_unit_scale(model)  # raw file units -> metres

    all_doors = _doors(model, scale)
    all_transport = _transport_elements(model, scale)

    return {
        "project": _project_name(model),
        "spaces": _spaces(model, scale),
        "corridors": _corridors(model, scale),
        "doors": all_doors,
        "stairs": _stairs(model, scale),
        "stair_flights": _stair_flights(model, scale),
        "windows": _windows(model, scale),
        "walls": _walls(model, scale),
        "slabs": _slabs(model),
        "storeys": _storeys(model, scale),
        "smoke_alarms": _smoke_alarms(model, scale),
        "fire_suppression_terminals": _fire_suppression_terminals(model),
        "space_boundaries": _space_boundaries(model),
        "connected_elements": _connected_elements(model),
        "elevators": [t for t in all_transport if t["category"] == "elevator"],
        "escalators": [t for t in all_transport if t["category"] == "escalator"],
        "emergency_exits": [d for d in all_doors if d["is_emergency_exit"]]
    }


if __name__ == "__main__":
    import sys

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )

    report = get_summary(ifc_path)

    print("Project File:", report["project"])
    print("Total Spaces:", len(report["spaces"]))
    print("Total Doors:", len(report["doors"]))
    print("Total Exits:", len(report["emergency_exits"]))
    print("Total Stairs:", len(report["stairs"]))
    print("Total Stair Flights:", len(report["stair_flights"]))
    print("Total Windows:", len(report["windows"]))
    print("Total Corridors:", len(report["corridors"]))
    print("Total Walls:", len(report["walls"]))
    print("Total Slabs:", len(report["slabs"]))
    print("Total Storeys:", len(report["storeys"]))
    print("Total Smoke Alarms:", len(report["smoke_alarms"]))
    print("Total Fire Suppression Terminals:", len(report["fire_suppression_terminals"]))
    print("Total Elevators:", len(report["elevators"]))
    print("Total Escalators:", len(report["escalators"]))

    print("\nStorey heights above ground (approximate):")
    for s in report["storeys"]:
        flag = "" if s["ground_reference_found"] else "  [no EntranceLevel found — using lowest storey as 0]"
        print(f"  {s['name']}: {s['height_above_ground_m']:.2f}m{flag}")

    doors_no_width = [d for d in report["doors"] if d["width_m"] is None]
    stairs_no_width = [s for s in report["stairs"] if s["width"] is None]
    print(f"\nDoors with no width data (skipped by regulation checks): {len(doors_no_width)}")
    print(f"Stairs with no width data (skipped by regulation checks): {len(stairs_no_width)}")

    if report["doors"]:
        widths = [d["width_m"] for d in report["doors"] if d["width_m"] is not None]
        if widths:
            print(f"Door width range: {min(widths):.3f}m - {max(widths):.3f}m")
