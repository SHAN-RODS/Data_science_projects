# Checks IFC Parsed Elements against Fire Safety Rules and Raises a Flag If there is Violation
# If we are unsure regarding a violation we raise a manual review flag else a priority based Flag

import json
import os
import sys
from core_backend.ifc_parser import parser_summary

rules_file = {
    "england":"eng_reg.json",
    "wales":"wales_reg.json",
    "northern_ireland":"ireland_reg.json",
    "scotland":"scotland_reg.json",
}

def load_regs(jurisdiction="england"):
    jurisdiction = jurisdiction.lower()
    filename = rules_file.get(jurisdiction)
    # if filename is None:
    #     raise ValueError(f"Unknown jurisdiction: {jurisdiction}")
    directory = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(directory, filename)
    with open(json_path, "r", encoding="utf-8") as f:
        building_data = json.load(f)
    regulations_by_id = {
    rule["unique_id"]: rule
    for rule in building_data.get("regulations", [])
}
    return regulations_by_id


def flag(rule, element, element_type, attribute, issue, requires_manual_review=False):
    return {
        "element_id": element.get("id", "BUILDING"),
        "element_name": element.get("name", "Building"),
        "element_type":element_type,
        "attribute": attribute,
        "rule": rule,
        "issue": issue,
        "requires_manual_review": requires_manual_review
    }

def manual_review_flag(rule, element, element_type, reason):
    return flag(
        rule, element, element_type, "Cannot Check from the IFC data alone",
        f"{reason} Manual review will be required against {rule['doc_reference']}.",
        requires_manual_review=True
    )

def to_metres(value, unit):
    if unit == "mm":
        return value / 1000
    return value  

def violates(value, rule):
    threshold = to_metres(rule["threshold_mark"], rule.get("unit"))
    comparison = rule.get("comparison")
    if comparison == "gte":
        return value < threshold
    if comparison == "lte":
        return value > threshold
    return False


def distance_m(pos_a, pos_b):
    if pos_a is None or pos_b is None:
        return None
    return sum((a - b) ** 2 for a, b in zip(pos_a, pos_b)) ** 0.5


# England — Approved Document B Volume 1 

def door_width(doors, regs):
    flags = []
    rule = regs.get("ENG-R1")
    limit_m = to_metres(rule["threshold_mark"], rule.get("unit"))
    for door in doors:
        width = door.get("width_m")
        if width is None:
            continue
        if width < limit_m:
            flags.append(flag(
                rule, door, "door",
                f"OverallWidth = {width:.3f}m ({int(width * 1000)}mm)",
                f"Door '{door['name']}' is {width:.3f}m ({int(width * 1000)}mm) wide — "
                f"below the {limit_m}m ({int(limit_m * 1000)}mm) minimum. "
                f"Reference: {rule['doc_reference']}."
            ))
    return flags

def exits(emergency_exits, regs):
    flags = []
    rule = regs.get("ENG-R11")
    if rule is None:
        return flags
    if len(emergency_exits) == 0:
        flags.append(flag(
            rule, {"id": "BUILDING", "name": "Building"}, "exits",
            "Emergency exit count = 0",
            "No emergency exits detected in the model. Final exits must give "
            "direct access to a place of safety outside"
            f". Reference: {rule['doc_reference']}."
        ))
    return flags


def possible_escape_route(emergency_exits, regs):
    flags = []
    rule = regs.get("ENG-R12")
    if rule is None:
        return flags

    count = len(emergency_exits)
    limit = int(rule["threshold_mark"])

    if count < limit:
        flags.append(flag(
            rule, {"id": "BUILDING", "name": "Building"}, "exits",
            f"Escape route count = {count}",
            f"Only {count} escape routes found, below the required minimum "
            f"of {limit}. Occupants need an alternative way out if one route "
            f"is blocked by fire, smoke or heat. Ref: {rule['doc_reference']}."
        ))
    return flags


def stair_width(stairs, regs):
    flags = []
    rule = regs.get("ENG-R4")
    limit_m = to_metres(rule["threshold_mark"], rule.get("unit"))

    for stair in stairs:
        width = stair.get("width")
        if width is None:
            continue
        if width < limit_m:
            flags.append(flag(
                rule, stair, "stair",
                f"Width = {width:.3f}m ({int(width * 1000)}mm)",
                f"Stair '{stair['name']}' is {width:.3f}m wide — below the "
                f"{limit_m}m ({int(limit_m * 1000)}mm) minimum for firefighting "
                f"stairs and common stairs. Ref: {rule['doc_reference']}."
            ))
    return flags


def window_area(windows, regs, rule_id="ENG-R6"):
    flags = []
    rule = regs.get(rule_id)
    if rule is None:
        return flags
    limit = rule["threshold_mark"]
    for window in windows:
        area = window.get("area")
        if area is None:
            continue
        if area < limit:
            flags.append(flag(
                rule, window, "window",
                f"Opening area = {area:.3f}sqm",
                f"Window '{window['name']}' has an opening area of "
                f"{area:.3f}sqm — below the {limit}sqm minimum for escape "
                f"windows. Ref: {rule['doc_reference']}."
            ))
    return flags


def window_size(windows, regs, rule_id="ENG-R7"):
    flags = []
    rule = regs.get(rule_id)
    if rule is None:
        return flags
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    for window in windows:
        height = window.get("height")
        width = window.get("width")

        if height is not None and height < limit:
            flags.append(flag(
                rule, window, "window",
                f"OverallHeight = {height:.3f}m ({int(height * 1000)}mm)",
                f"Window '{window['name']}' height is {height:.3f}m — below "
                f"the {int(limit * 1000)}mm minimum for escape windows. "
                f"Ref: {rule['doc_reference']}."
            ))

        if width is not None and width < limit:
            flags.append(flag(
                rule, window, "window",
                f"OverallWidth = {width:.3f}m ({int(width * 1000)}mm)",
                f"Window '{window['name']}' width is {width:.3f}m — below "
                f"the {int(limit * 1000)}mm minimum for escape windows. "
                f"Ref: {rule['doc_reference']}."
            ))
    return flags


def lifts(elevators, regs):
    flags = []
    rule = regs.get("ENG-R8")
    for lift in elevators:
        if not lift.get("is_evac_lift", False):
            flags.append(flag(
                rule, lift, "elevator",
                f"PredefinedType = {lift.get('predefined_type', 'ELEVATOR')}",
                f"Standard lift '{lift['name']}' detected. Lift wells must be "
                f"enclosed within a protected stairway or have minimum REI 30 "
                f"fire resisting construction, and must not be used as means "
                f"of escape. Ref: {rule['doc_reference']}."
            ))
    return flags

def evacuation_lifts(elevators, regs):
    flags = []
    rule = regs.get("ENG-R9")
    for lift in elevators:
        if lift.get("is_evac_lift", False):
            flags.append(manual_review_flag(
                rule, lift, "elevator",
                f"Evacuation lift '{lift['name']}' detected. Must be within a "
                f"dedicated shaft with protected stairway and evacuation lobby."
            ))
    return flags

def floor_slabs(slabs, regs):
    flags = []
    rule = regs.get("ENG-R10")
    if rule is None:
        return flags
    floor_slabs_only = [ slab for slab in slabs if "FLOOR" in str(slab.get("slab_type", "")).upper() ]
    for slab in floor_slabs_only:
        if slab.get("fire_rating") is None:
            flags.append(manual_review_flag(
                rule, slab, "slab",
                f"Floor slab '{slab['name']}' has no fire rating recorded in "
                f"the IFC file. Every floor separating flats must achieve "
                f"the required fire resistance."
            ))
    return flags


def check_england(summary, regs):
    flags = []
    flags += door_width(summary["doors"], regs)
    flags += exits(summary["emergency_exits"], regs)
    flags += possible_escape_route(summary["emergency_exits"], regs)
    flags += stair_width(summary["stairs"], regs)
    flags += window_sill_height_check(summary["windows"], regs["ENG-R5"])
    flags += window_area(summary["windows"], regs)
    flags += window_size(summary["windows"], regs)
    flags += lifts(summary.get("elevators", []), regs)
    flags += evacuation_lifts(summary.get("elevators", []), regs)
    flags += floor_slabs(summary.get("slabs", []), regs)
    return flags

def storey_height_provision_check(storeys, rule, provided_count, element_label):
    flags = []
    if provided_count > 0:
        return flags
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    for storey in storeys:
        height = storey["height_above_ground_m"]
        if height >= limit:
            flags.append(flag(
                rule, storey, "storey", f"Height above ground = {height:.2f}m",
                f"Storey '{storey['name']}' is {height:.2f}m above ground "
                f"level (>= {limit}m) but no {element_label} was found "
                f"anywhere in the model. Ref: {rule['doc_reference']}."
            ))
    return flags

def fire_suppression_presence_check(fire_suppression_terminals, rule):
    flags = []
    if len(fire_suppression_terminals) < rule["threshold_mark"]:
        flags.append(flag(
            rule, {"id": "BUILDING", "name": "Building"}, "fire_suppression",
            f"Fire suppression terminal count = {len(fire_suppression_terminals)}",
            f"No automatic fire suppression system detected in the model. "
            f"{rule['description']} Ref: {rule['doc_reference']}."
        ))
    return flags

def window_sill_height_check(windows, rule):
    flags = []
    limits = rule["threshold_mark"]
    min_m = limits["min"] / 1000
    max_m = limits["max"] / 1000
    for window in windows:
        sill = window.get("sill_height")
        if sill is None:
            flags.append(manual_review_flag(
                rule, window, "window",
                f"Window '{window['name']}' has no sill height recorded in the IFC file."
            ))
            continue
        if sill < min_m or sill > max_m:
            flags.append(flag(
                rule, window, "window",
                f"Sill height = {sill:.3f}m ({int(sill * 1000)}mm)",
                f"Window '{window['name']}' sill height is {int(sill * 1000)}mm — "
                f"outside the {limits['min']}-{limits['max']}mm required range. "
                f"Ref: {rule['doc_reference']}."
            ))
    return flags

def smoke_alarm_check(smoke_alarms, doors, rule):
    flags = []
    if len(smoke_alarms) == 0:
        flags.append(flag(
            rule, {"id": "BUILDING", "name": "Building"}, "smoke_alarm",
            "Smoke alarm count = 0",
            f"No smoke alarms detected in the model. {rule['description']} "
            f"Ref: {rule['doc_reference']}."
        ))
        return flags
    threshold = rule["threshold_mark"]
    limit = min(threshold.values()) if isinstance(threshold, dict) else to_metres(threshold, rule.get("unit"))
    alarm_positions = [a["position"] for a in smoke_alarms if a["position"] is not None]
    if not alarm_positions:
        return flags
    for door in doors:
        door_pos = door.get("position")
        if door_pos is None:
            continue
        nearest = min(distance_m(door_pos, ap) for ap in alarm_positions)
        if nearest > limit:
            flags.append(flag(
                rule, door, "door", f"Distance to nearest smoke alarm = {nearest:.1f}m",
                f"Door '{door['name']}' is {nearest:.1f}m (straight-line) from the "
                f"nearest smoke alarm — beyond the {limit}m guidance distance. "
                f"Ref: {rule['doc_reference']}."
            ))
    return flags

def stair_flight_width_check(stair_flights, rule):
    flags = []
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    for flight in stair_flights:
        width = flight.get("width")
        if width is None:
            continue
        if width < limit:
            flags.append(flag(
                rule, flight, "stair_flight",
                f"Width = {width:.3f}m ({int(width * 1000)}mm)",
                f"Stair flight '{flight['name']}' is {width:.3f}m wide — below "
                f"the {int(limit * 1000)}mm minimum. Ref: {rule['doc_reference']}."
            ))
    return flags

def space_area_limit_check(spaces, rule):
    flags = []
    for space in spaces:
        area = space.get("area")
        if area is None:
            continue
        if violates(area, rule):
            flags.append(flag(
                rule, space, "space", f"Area = {area:.1f}sqm",
                f"Space '{space['name']}' is {area:.1f}sqm — exceeds the "
                f"{rule['threshold_mark']}sqm limit. Ref: {rule['doc_reference']}."
            ))
    return flags


def basement_manual_review(storeys, rule):
    flags = []
    basements = [
        s for s in storeys
        if "basement" in s["name"].lower() or s["height_above_ground_m"] < 0
    ]
    for storey in basements:
        flags.append(manual_review_flag(
            rule, storey, "storey",
            f"Storey '{storey['name']}' is below ground level and may contain habitable rooms."
        ))
    return flags


def passenger_lift_manual_review(storeys, elevators, rule):
    flags = []
    if not elevators:
        return flags
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        for lift in elevators:
            flags.append(manual_review_flag(
                rule, lift, "elevator",
                f"Passenger lift '{lift['name']}' present in a building with a "
                f"storey >= {limit}m above ground."
            ))
    return flags


def protected_stairway_manual_review(storeys, stairs, rule):
    flags = []
    if not stairs:
        return flags
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "stair",
            f"Building has a storey >= {limit}m above ground — protected "
            f"stairway barrier configuration must be verified."
        ))
    return flags


def protected_stairway_manual_review(storeys, walls, rule):
    flags = []
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    if not walls:
        return flags
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "wall",
            f"Building has a storey >= {limit}m above ground — protected "
            f"enclosure walls must be verified to run the full required height."
        ))
    return flags

def lobby_manual_review(corridors, rule):
    flags = []
    lobbies = [c for c in corridors if "lobby" in c["name"].lower()]
    for lobby in lobbies:
        flags.append(manual_review_flag(
            rule, lobby, "corridor",
            f"Lobby space '{lobby['name']}' detected — travel distance to the "
            f"nearest protected stair must be verified manually."
        ))
    return flags

def external_stair_manual_review(storeys, stairs, rule):
    flags = []
    if not stairs:
        return flags
    limit = to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "stair",
            f"Building's topmost storey is >= {limit}m above ground — verify "
            f"whether any external escape stair serves it."
        ))
    return flags

def wall_proximity_manual_review(walls, rule):
    flags = []
    external_walls = [w for w in walls if w.get("is_external")]
    if external_walls:
        flags.append(manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "wall",
            f"{len(external_walls)} external wall(s) detected — proximity to "
            f"the protected final-exit zone must be verified manually."
        ))
    return flags


# Wales — Approved Document B Volume 2

def check_wales(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])
    stair_flights = summary.get("stair_flights", [])

    
    flags += storey_height_provision_check(storeys, regs["WAL-R1"], len(stairs), "stair")
    flags += storey_height_provision_check(storeys, regs["WAL-R2"], len(summary["emergency_exits"]), "escape route")
    flags += window_area(summary["windows"], regs, "WAL-R3")
    flags += fire_suppression_presence_check(summary.get("fire_suppression_terminals", []), regs["WAL-R4"])
    flags += window_sill_height_check(summary["windows"], regs["WAL-R5"])
    flags += basement_manual_review(storeys, regs["WAL-R6"])
    flags += stair_flight_width_check(stair_flights, regs["WAL-R7"])
    flags += passenger_lift_manual_review(storeys, summary.get("elevators", []), regs["WAL-R8"])
    flags += smoke_alarm_check(summary.get("smoke_alarms", []), summary["doors"], regs["WAL-R9"])
    flags += protected_stairway_manual_review(storeys, stairs, regs["WAL-R10"])

    return flags


# Northern Ireland — Technical Booklet E (2012)
def check_northern_ireland(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])

    flags += storey_height_provision_check(storeys, regs["NI-R1"], len(stairs), "stair")
    flags += storey_height_provision_check(storeys, regs["NI-R2"], len(summary["emergency_exits"]), "protected stairway")
    flags += window_area(summary["windows"], regs, "NI-R3")
    flags += window_size(summary["windows"], regs, "NI-R4")
    flags += window_sill_height_check(summary["windows"], regs["NI-R5"])
    flags += basement_manual_review(storeys, regs["NI-R6"])
    flags += smoke_alarm_check(summary.get("smoke_alarms", []), summary["doors"], regs["NI-R7"])
    flags += space_area_limit_check(summary["spaces"], regs["NI-R8"])
    flags += passenger_lift_manual_review(storeys, summary.get("elevators", []), regs["NI-R9"])
    flags += external_stair_manual_review(storeys, stairs, regs["NI-R10"])
    return flags

# Scotland — Technical Handbooks 2022

def check_scotland(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])
    walls = summary.get("walls", [])

    
    flags += storey_height_provision_check(storeys, regs["SCO-R1"], len(summary["emergency_exits"]), "escape route")
    flags += protected_stairway_manual_review(storeys, walls, regs["SCO-R2"])
    flags += storey_height_provision_check(storeys, regs["SCO-R3"], len(summary["emergency_exits"]), "alternative exit route")
    flags += window_area(summary["windows"], regs, "SCO-R4")
    flags += fire_suppression_presence_check(summary.get("fire_suppression_terminals", []), regs["SCO-R5"])
    flags += lobby_manual_review(summary["corridors"], regs["SCO-R6"])
    flags += basement_manual_review(storeys, regs["SCO-R7"])
    flags += space_area_limit_check(summary["spaces"], regs["SCO-R8"])
    flags += wall_proximity_manual_review(walls, regs["SCO-R9"])
    flags += external_stair_manual_review(storeys, stairs, regs["SCO-R10"])
    return flags


CHECKERS = {
    "england":check_england,
    "wales":check_wales,
    "northern_ireland":check_northern_ireland,
    "scotland":check_scotland,
}

def check_all_rules(summary, jurisdiction="england"):
    jurisdiction = jurisdiction.lower()
    regs = load_regs(jurisdiction)
    checker = CHECKERS.get(jurisdiction)
    if checker is None:
        raise ValueError(f"No jurisdiction: {jurisdiction}")
    return checker(summary, regs)


if __name__ == "__main__":

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )
    summary = parser_summary(ifc_path)
    for jurisdiction in ["england", "wales", "northern_ireland", "scotland"]:
        violations = check_all_rules(summary, jurisdiction=jurisdiction)
        print(f"\n {jurisdiction.upper()} — {len(violations)} flags")
        for violation in violations:
            rule = violation["rule"]
            print(f"  [{rule['severity_level'].upper()}] {rule['unique_id']} — "
                  f"{violation['element_type']} — {violation['element_name']}")
            print(f"  {violation['issue']}\n")

