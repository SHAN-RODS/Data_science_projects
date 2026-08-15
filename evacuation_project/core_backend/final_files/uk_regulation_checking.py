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


# --- Annotation layer (reframed) ---------------------------------------------------------------
#
# The legacy checkers below emit a flag only when an element *violates* a threshold, and silently
# skip elements whose attribute is missing. The pivot needs the opposite: a non-verdict reference
# layer that reports the *measured value + applicable limit + a flag* for every measurable element
# (whether it meets the limit or not), and surfaces missing data explicitly. The scenario generator
# may cite these notes; it never treats them as a compliance verdict. A below-limit measurement is
# flagged "requires_manual_review", never "non-compliant" -- this tool does not issue verdicts.

AREA_UNITS = ("sqm", "m2", "m²")


def rule_kind(rule):
    """Classify a jurisdiction rule into a measurable annotation kind, or None to skip.

    Dispatch on ``ifc_element`` + ``ifc_attribute_involved`` (reliable) rather than ``applies_to``,
    which is mislabelled in several of the jurisdiction JSONs (e.g. storey-height rules tagged
    ``applies_to: "doors"``). ``ifc_element`` may be a string or a list.
    """
    element = rule.get("ifc_element") or ""
    if isinstance(element, list):
        element = " ".join(element)
    element = element.lower()
    attr = (rule.get("ifc_attribute_involved") or "").lower()
    unit = (rule.get("unit") or "").lower()

    if "ifcdoor" in element and "width" in attr:
        return "door_width"
    if "ifcstair" in element and "flight" not in element and "width" in attr:
        return "stair_width"
    if "ifcwindow" in element and "area" in attr and unit in AREA_UNITS:
        return "window_area"
    if "ifcdoor" in element and ("emergency" in attr or "exit" in attr) and unit == "count":
        return "exits_count"
    return None


def regulation_note(rule, element, element_type, attribute, measured, limit, meets):
    return {
        "regulation_id": rule["unique_id"],
        "regulation_name": rule.get("regulation_name"),
        "reference": rule.get("doc_reference"),
        "element_id": element.get("id", "BUILDING"),
        "element_name": element.get("name", "Building"),
        "element_type": element_type,
        "attribute": attribute,
        "measured": measured,
        "limit": limit,
        # non-verdict: meets the reference limit, or a reviewer should check it -- never "violation"
        "flag": "within_limit" if meets else "requires_manual_review",
    }


def missing(rule, element, element_type, attribute):
    return {
        "regulation_id": rule["unique_id"],
        "reference": rule.get("doc_reference"),
        "element_id": element.get("id"),
        "element_name": element.get("name"),
        "element_type": element_type,
        "missing": attribute,
        "action": "flagged, not silently passed",
    }


def annotate(summary, jurisdiction="england"):
    """Produce non-verdict reference notes + a not_assessed list for the measurable egress elements.

    Returns ``{"regulation_notes": [...], "not_assessed": [...]}``. Works across the jurisdiction
    JSONs by dispatching on each rule's ``applies_to`` + ``unit`` rather than hardcoded IDs.
    """
    regs = load_regs(jurisdiction)
    notes, not_assessed = [], []

    for rule in regs.values():
        kind = rule_kind(rule)
        if kind is None:
            continue

        if kind == "door_width":
            limit = to_metres(rule["threshold_mark"], rule.get("unit"))
            for door in summary.get("doors", []):
                width = door.get("width_m")
                if width is None:
                    not_assessed.append(missing(rule, door, "door", "width"))
                else:
                    notes.append(regulation_note(rule, door, "door", "width_m",
                                       round(width, 3), limit, width >= limit))

        elif kind == "stair_width":
            limit = to_metres(rule["threshold_mark"], rule.get("unit"))
            for stair in summary.get("stairs", []):
                width = stair.get("width")
                if width is None:
                    not_assessed.append(missing(rule, stair, "stair", "width"))
                else:
                    notes.append(regulation_note(rule, stair, "stair", "width",
                                       round(width, 3), limit, width >= limit))

        elif kind == "window_area":
            limit = rule["threshold_mark"]
            for window in summary.get("windows", []):
                area = window.get("area")
                if area is None:
                    not_assessed.append(missing(rule, window, "window", "opening area"))
                else:
                    notes.append(regulation_note(rule, window, "window", "area_m2",
                                       round(area, 3), limit, area >= limit))

        elif kind == "exits_count":
            limit = int(rule["threshold_mark"])
            count = len(summary.get("emergency_exits", []))
            building = {"id": "BUILDING", "name": "Building"}
            notes.append(regulation_note(rule, building, "exits", "emergency_exit_count",
                               count, limit, count >= limit))

    return {"regulation_notes": notes, "not_assessed": not_assessed}


def summarize_annotations(annotation_result):
    """Collapse the per-element notes into a per-regulation summary + the flagged exceptions.

    The full within-limit list is high volume and low signal (one row per compliant door/window);
    this keeps the traceable counts + measured range and lists only the requires_manual_review items
    in full. Non-verdict throughout.
    """
    notes = annotation_result.get("regulation_notes", [])
    groups = {}
    for note in notes:
        groups.setdefault(note["regulation_id"], []).append(note)

    by_regulation = []
    for reg_id, group in groups.items():
        measured = [g["measured"] for g in group if isinstance(g["measured"], (int, float))]
        first = group[0]
        by_regulation.append({
            "regulation_id": reg_id,
            "regulation_name": first.get("regulation_name"),
            "reference": first.get("reference"),
            "element_type": first.get("element_type"),
            "checked": len(group),
            "within_limit": sum(1 for g in group if g["flag"] == "within_limit"),
            "requires_manual_review": sum(1 for g in group if g["flag"] == "requires_manual_review"),
            "measured_min": round(min(measured), 3) if measured else None,
            "measured_max": round(max(measured), 3) if measured else None,
            "limit": first.get("limit"),
        })

    exceptions = [n for n in notes if n["flag"] == "requires_manual_review"]
    return {"by_regulation": by_regulation, "requires_manual_review": exceptions}


def annotate_and_summarize(summary, jurisdiction="england"):
    """Annotate the model then return the compact building-level regulation block."""
    result = annotate(summary, jurisdiction=jurisdiction)
    summarised = summarize_annotations(result)
    return {
        "jurisdiction": jurisdiction,
        "basis": "measured value vs applicable limit; non-verdict reference only",
        "by_regulation": summarised["by_regulation"],
        "requires_manual_review": summarised["requires_manual_review"],
        "not_assessed": result["not_assessed"],
    }


# England — Approved Document B Volume 1 

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


# --- Pass/Fail gate ----------------------------------------------------------------------------
#
# A visible, blocking validation step (supervisor requirement): the building is checked against the
# selected regulation BEFORE any scenario is generated. Any hard threshold violation (a measurable
# attribute that breaches its limit) FAILS the building and blocks generation. "Manual review" flags
# ("cannot check from the IFC alone") are surfaced but do NOT block — they are uncertainty, not
# violations, and would otherwise fail almost every model on missing fire-ratings etc.

def slim_flag(f):
    """Flatten a raw checker flag into a compact, JSON/UI-friendly row."""
    rule = f.get("rule", {}) or {}
    return {
        "regulation_id": rule.get("unique_id"),
        "regulation_name": rule.get("regulation_name"),
        "severity": rule.get("severity_level"),
        "reference": rule.get("doc_reference"),
        "element_id": f.get("element_id"),
        "element_name": f.get("element_name"),
        "element_type": f.get("element_type"),
        "attribute": f.get("attribute"),
        "issue": f.get("issue"),
    }


def regulation_gate(summary, jurisdiction="england"):
    """Deterministic PASS/FAIL of the building against the selected regulation.

    Returns ``{jurisdiction, passed, violations, manual_review, basis}``. ``passed`` is True only when
    there is no hard violation. ``violations`` are threshold breaches (blocking); ``manual_review`` are
    items the IFC cannot decide (non-blocking, shown for transparency). This is the gate the frontend
    uses to allow or block scenario generation.
    """
    flags = check_all_rules(summary, jurisdiction)
    violations = [slim_flag(f) for f in flags if not f.get("requires_manual_review")]
    manual_review = [slim_flag(f) for f in flags if f.get("requires_manual_review")]
    return {
        "jurisdiction": jurisdiction,
        "passed": len(violations) == 0,
        "violations": violations,
        "manual_review": manual_review,
        "basis": ("hard threshold checks against the selected regulation; any violation blocks "
                  "generation; manual-review items are surfaced but non-blocking"),
    }


if __name__ == "__main__":
    from core_backend.sample_paths import resolve_ifc

    summary = parser_summary(resolve_ifc(sys.argv))

    # Pass/Fail gate (the blocking validation step) — measured against each jurisdiction.
    for jurisdiction in ["england", "wales", "northern_ireland", "scotland"]:
        gate = regulation_gate(summary, jurisdiction=jurisdiction)
        verdict = "PASS" if gate["passed"] else "FAIL"
        print(f"\n{jurisdiction.upper()} — {verdict} "
              f"({len(gate['violations'])} violation(s), {len(gate['manual_review'])} manual-review)")
        for v in gate["violations"][:6]:
            print(f"  [{v['severity']}] {v['regulation_id']} {v['element_type']}/{v['element_name']}: "
                  f"{v['issue']}")

