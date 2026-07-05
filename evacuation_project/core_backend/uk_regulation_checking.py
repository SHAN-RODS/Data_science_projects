# Checks parsed IFC elements against UK fire safety regulations for a
# chosen jurisdiction (England, Wales, Northern Ireland, Scotland). Each
# function returns a list of "flag" dicts — one flag = one violation, or
# one "manual review required" note where IFC data alone can't settle the
# question. This is the core of the project: it turns raw IFC data into
# the issues that scenario generation then explains in plain English.
#
# Two kinds of flags are produced:
#   - Computed violations: a real numeric/logical comparison against
#     extracted IFC data (e.g. door width < 750mm).
#   - Manual review flags: the regulation depends on information IFC
#     geometry can't reliably provide (e.g. "is this wall near enough to
#     the protected zone at the final exit?"). These are clearly worded
#     as requiring manual verification rather than presented as a
#     computed pass/fail — the same pattern the project already uses for
#     evacuation lift shaft compliance and floor slab fire ratings.

import json
import os

_JURISDICTION_FILES = {
    "england":          "eng_reg.json",
    "wales":            "wales_reg.json",
    "northern_ireland": "ireland_reg.json",
    "scotland":         "scotland_reg.json",
}


def load_regs(jurisdiction="england"):
    """Loads one jurisdiction's regulation file and returns {unique_id: rule}."""
    jurisdiction = jurisdiction.lower()
    filename = _JURISDICTION_FILES.get(jurisdiction)
    if filename is None:
        raise ValueError(f"Unknown jurisdiction: {jurisdiction}")

    directory = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(directory, filename)

    with open(json_path, "r", encoding="utf-8") as f:
        building_data = json.load(f)

    return {rule["unique_id"]: rule for rule in building_data.get("regulations", [])}


# ── Shared helpers ───────────────────────────────────────────────────────

def _flag(rule, element, element_type, attribute, issue, requires_manual_review=False):
    return {
        "element_id":   element.get("id", "BUILDING"),
        "element_name": element.get("name", "Building"),
        "element_type": element_type,
        "attribute":    attribute,
        "rule":         rule,
        "issue":        issue,
        # True means: IFC data alone can't confirm or deny this — a person
        # needs to check it. False means: this is a computed violation
        # directly from extracted IFC data. Kept separate from severity,
        # which describes how serious the regulation is if it does apply,
        # not whether it's been confirmed.
        "requires_manual_review": requires_manual_review
    }


def _manual_review_flag(rule, element, element_type, reason):
    return _flag(
        rule, element, element_type, "Not verifiable from IFC data alone",
        f"{reason} Manual review required against {rule['doc_reference']}.",
        requires_manual_review=True
    )


def _to_metres(value, unit):
    if unit == "mm":
        return value / 1000
    return value  # already metres, sqm, count, mins, presence, degrees


def _violates(value, rule):
    """True if `value` breaks the rule, honouring the rule's comparison direction."""
    threshold = _to_metres(rule["threshold_mark"], rule.get("unit"))
    comparison = rule.get("comparison", "gte")
    if comparison == "gte":
        return value < threshold
    if comparison == "lte":
        return value > threshold
    return False


def _distance_m(pos_a, pos_b):
    if pos_a is None or pos_b is None:
        return None
    return sum((a - b) ** 2 for a, b in zip(pos_a, pos_b)) ** 0.5


# ── England — Approved Document B Volume 1 ───────────────────────────────

def door_width(doors, regs):
    flags = []
    rule = regs.get("ENG-R1")
    if rule is None:
        return flags

    limit_m = _to_metres(rule["threshold_mark"], rule.get("unit"))

    for door in doors:
        width = door.get("width_m")
        if width is None:
            continue
        if width < limit_m:
            flags.append(_flag(
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
        flags.append(_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "exits",
            "Emergency exit count = 0",
            "No emergency exits detected in the model. Final exits must give "
            "direct access to a place of safety outside and must not be a "
            f"barrier for disabled people. Reference: {rule['doc_reference']}."
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
        flags.append(_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "exits",
            f"Escape route count = {count}",
            f"Only {count} escape route(s) found, below the required minimum "
            f"of {limit}. Occupants need an alternative way out if one route "
            f"is blocked by fire, smoke or heat. Ref: {rule['doc_reference']}."
        ))
    return flags


def stair_width(stairs, regs):
    flags = []
    rule = regs.get("ENG-R4")
    if rule is None:
        return flags

    limit_m = _to_metres(rule["threshold_mark"], rule.get("unit"))

    for stair in stairs:
        width = stair.get("width")
        if width is None:
            continue
        if width < limit_m:
            flags.append(_flag(
                rule, stair, "stair",
                f"Width = {width:.3f}m ({int(width * 1000)}mm)",
                f"Stair '{stair['name']}' is {width:.3f}m wide — below the "
                f"{limit_m}m ({int(limit_m * 1000)}mm) minimum for firefighting "
                f"stairs and common stairs. Ref: {rule['doc_reference']}."
            ))
    return flags


def corridor_width(corridors, regs):
    flags = []
    rule = regs.get("ENG-R5")
    if rule is None:
        return flags

    limit_m = _to_metres(rule["threshold_mark"], rule.get("unit"))

    for corridor in corridors:
        width = corridor.get("width")
        if width is None:
            continue
        if width < limit_m:
            flags.append(_flag(
                rule, corridor, "corridor",
                f"Width = {width:.3f}m ({int(width * 1000)}mm)",
                f"Corridor '{corridor['name']}' is {width:.3f}m wide — below "
                f"the {limit_m}m ({int(limit_m * 1000)}mm) minimum. Common "
                f"corridors must be protected corridors with compartment "
                f"walls. Ref: {rule['doc_reference']}."
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
            flags.append(_flag(
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

    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))

    for window in windows:
        height = window.get("height")
        width = window.get("width")

        if height is not None and height < limit:
            flags.append(_flag(
                rule, window, "window",
                f"OverallHeight = {height:.3f}m ({int(height * 1000)}mm)",
                f"Window '{window['name']}' height is {height:.3f}m — below "
                f"the {int(limit * 1000)}mm minimum for escape windows. "
                f"Ref: {rule['doc_reference']}."
            ))

        if width is not None and width < limit:
            flags.append(_flag(
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
    if rule is None:
        return flags

    for lift in elevators:
        if not lift.get("is_evac_lift", False):
            flags.append(_flag(
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
    if rule is None:
        return flags

    for lift in elevators:
        if lift.get("is_evac_lift", False):
            flags.append(_manual_review_flag(
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

    floor_slabs_only = [
        slab for slab in slabs if "FLOOR" in str(slab.get("slab_type", "")).upper()
    ]

    for slab in floor_slabs_only:
        if slab.get("fire_rating") is None:
            flags.append(_manual_review_flag(
                rule, slab, "slab",
                f"Floor slab '{slab['name']}' has no fire rating recorded in "
                f"the IFC file. Every floor separating flats must achieve "
                f"the required fire resistance."
            ))
    return flags


def escalators(escalator_list, regs, rule_id="ENG-R13"):
    flags = []
    rule = regs.get(rule_id)
    if rule is None:
        return flags

    for escalator in escalator_list:
        flags.append(_flag(
            rule, escalator, "escalator",
            "category = escalator",
            f"Escalator '{escalator['name']}' detected. Escalators must not "
            f"be counted as part of the number or capacity of escape routes. "
            f"Ref: {rule['doc_reference']}."
        ))
    return flags


def _check_england(summary, regs):
    flags = []
    flags += door_width(summary["doors"], regs)
    flags += exits(summary["emergency_exits"], regs)
    flags += possible_escape_route(summary["emergency_exits"], regs)
    flags += stair_width(summary["stairs"], regs)
    flags += corridor_width(summary["corridors"], regs)
    flags += window_area(summary["windows"], regs)
    flags += window_size(summary["windows"], regs)
    flags += lifts(summary.get("elevators", []), regs)
    flags += evacuation_lifts(summary.get("elevators", []), regs)
    flags += floor_slabs(summary.get("slabs", []), regs)
    flags += escalators(summary.get("escalators", []), regs)
    return flags


# ── Shared primitives for Wales / Northern Ireland / Scotland ────────────
# These jurisdictions' regulations check different attributes from England's
# (storey height above ground, smoke alarm coverage, fire suppression
# presence, stair flight width) so they get their own generic building
# blocks rather than reusing England's per-attribute functions.

def _storey_height_provision_check(storeys, rule, provided_count, element_label):
    """
    If any storey exceeds the rule's height threshold, some escape
    provision (a stair, an escape route) must exist somewhere in the
    building. provided_count is how many of that provision were found
    anywhere in the model — this flags the breaching storeys only when
    the count is zero, since IFC doesn't reliably link a stair/exit to a
    specific storey without deeper spatial-containment analysis.
    """
    flags = []
    if provided_count > 0:
        return flags

    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    for storey in storeys:
        height = storey["height_above_ground_m"]
        if height >= limit:
            flags.append(_flag(
                rule, storey, "storey", f"Height above ground = {height:.2f}m",
                f"Storey '{storey['name']}' is {height:.2f}m above ground "
                f"level (>= {limit}m) but no {element_label} was found "
                f"anywhere in the model. Ref: {rule['doc_reference']}."
            ))
    return flags


def _fire_suppression_presence_check(fire_suppression_terminals, rule):
    flags = []
    if len(fire_suppression_terminals) < rule["threshold_mark"]:
        flags.append(_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "fire_suppression",
            f"Fire suppression terminal count = {len(fire_suppression_terminals)}",
            f"No automatic fire suppression system detected in the model. "
            f"{rule['description']} Ref: {rule['doc_reference']}."
        ))
    return flags


def _window_sill_height_check(windows, rule):
    """threshold_mark is {"min": mm, "max": mm} for these rules."""
    flags = []
    limits = rule["threshold_mark"]
    min_m = limits["min"] / 1000
    max_m = limits["max"] / 1000

    for window in windows:
        sill = window.get("sill_height")
        if sill is None:
            flags.append(_manual_review_flag(
                rule, window, "window",
                f"Window '{window['name']}' has no sill height recorded in the IFC file."
            ))
            continue
        if sill < min_m or sill > max_m:
            flags.append(_flag(
                rule, window, "window",
                f"Sill height = {sill:.3f}m ({int(sill * 1000)}mm)",
                f"Window '{window['name']}' sill height is {int(sill * 1000)}mm — "
                f"outside the {limits['min']}-{limits['max']}mm required range. "
                f"Ref: {rule['doc_reference']}."
            ))
    return flags


def _smoke_alarm_check(smoke_alarms, doors, rule):
    """
    Real UK regs distinguish bedroom-door vs living/kitchen-door distances;
    room-type classification from IFC space names is unreliable across
    languages and authoring tools, so this applies a single simplified
    threshold (the stricter of the regulation's stated distances) to
    every door as a straight-line approximation — not the literal
    multi-tier rule, and not a walking-route distance.
    """
    flags = []
    if len(smoke_alarms) == 0:
        flags.append(_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "smoke_alarm",
            "Smoke alarm count = 0",
            f"No smoke alarms detected in the model. {rule['description']} "
            f"Ref: {rule['doc_reference']}."
        ))
        return flags

    threshold = rule["threshold_mark"]
    limit = min(threshold.values()) if isinstance(threshold, dict) else _to_metres(threshold, rule.get("unit"))
    alarm_positions = [a["position"] for a in smoke_alarms if a["position"] is not None]
    if not alarm_positions:
        return flags

    for door in doors:
        door_pos = door.get("position")
        if door_pos is None:
            continue
        nearest = min(_distance_m(door_pos, ap) for ap in alarm_positions)
        if nearest > limit:
            flags.append(_flag(
                rule, door, "door", f"Distance to nearest smoke alarm = {nearest:.1f}m",
                f"Door '{door['name']}' is {nearest:.1f}m (straight-line) from the "
                f"nearest smoke alarm — beyond the {limit}m guidance distance. "
                f"Ref: {rule['doc_reference']}."
            ))
    return flags


def _stair_flight_width_check(stair_flights, rule):
    flags = []
    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    for flight in stair_flights:
        width = flight.get("width")
        if width is None:
            continue
        if width < limit:
            flags.append(_flag(
                rule, flight, "stair_flight",
                f"Width = {width:.3f}m ({int(width * 1000)}mm)",
                f"Stair flight '{flight['name']}' is {width:.3f}m wide — below "
                f"the {int(limit * 1000)}mm minimum. Ref: {rule['doc_reference']}."
            ))
    return flags


def _space_area_limit_check(spaces, rule):
    flags = []
    for space in spaces:
        area = space.get("area")
        if area is None:
            continue
        if _violates(area, rule):
            flags.append(_flag(
                rule, space, "space", f"Area = {area:.1f}sqm",
                f"Space '{space['name']}' is {area:.1f}sqm — exceeds the "
                f"{rule['threshold_mark']}sqm limit. Ref: {rule['doc_reference']}."
            ))
    return flags


def _basement_manual_review(storeys, rule):
    flags = []
    basements = [
        s for s in storeys
        if "basement" in s["name"].lower() or s["height_above_ground_m"] < 0
    ]
    for storey in basements:
        flags.append(_manual_review_flag(
            rule, storey, "storey",
            f"Storey '{storey['name']}' is below ground level and may contain habitable rooms."
        ))
    return flags


def _passenger_lift_manual_review(storeys, elevators, rule):
    flags = []
    if not elevators:
        return flags
    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        for lift in elevators:
            flags.append(_manual_review_flag(
                rule, lift, "elevator",
                f"Passenger lift '{lift['name']}' present in a building with a "
                f"storey >= {limit}m above ground."
            ))
    return flags


def _protected_stairway_manual_review(storeys, stairs, rule):
    flags = []
    if not stairs:
        return flags
    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(_manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "stair",
            f"Building has a storey >= {limit}m above ground — protected "
            f"stairway barrier configuration must be verified."
        ))
    return flags


def _protected_enclosure_manual_review(storeys, walls, rule):
    """
    'Protected enclosure should reach 4.5m above ground' is a structural
    continuity question (does the fire-separating wall run full height)
    that IFC attributes can't settle — flagging every wall individually
    would just be noise, so this raises one aggregate flag when the
    building has a storey tall enough to trigger the requirement.
    """
    flags = []
    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    if not walls:
        return flags
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(_manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "wall",
            f"Building has a storey >= {limit}m above ground — protected "
            f"enclosure walls must be verified to run the full required height."
        ))
    return flags


def _lobby_manual_review(corridors, rule):
    """Lobby-classified spaces can't be checked for true travel distance to
    the nearest stair without full path routing, so they're flagged for
    manual review rather than an approximate straight-line guess."""
    flags = []
    lobbies = [c for c in corridors if "lobby" in c["name"].lower()]
    for lobby in lobbies:
        flags.append(_manual_review_flag(
            rule, lobby, "corridor",
            f"Lobby space '{lobby['name']}' detected — travel distance to the "
            f"nearest protected stair must be verified manually."
        ))
    return flags


def _external_stair_manual_review(storeys, stairs, rule):
    flags = []
    if not stairs:
        return flags
    limit = _to_metres(rule["threshold_mark"], rule.get("unit"))
    if any(s["height_above_ground_m"] >= limit for s in storeys):
        flags.append(_manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "stair",
            f"Building's topmost storey is >= {limit}m above ground — verify "
            f"whether any external escape stair serves it."
        ))
    return flags


def _wall_proximity_manual_review(walls, rule):
    """'Adjoining the protected zone near the final exit' is a relational,
    context-dependent judgement that IFC geometry alone can't resolve
    reliably, so external walls are flagged once for manual review rather
    than computing a potentially misleading distance figure."""
    flags = []
    external_walls = [w for w in walls if w.get("is_external")]
    if external_walls:
        flags.append(_manual_review_flag(
            rule, {"id": "BUILDING", "name": "Building"}, "wall",
            f"{len(external_walls)} external wall(s) detected — proximity to "
            f"the protected final-exit zone must be verified manually."
        ))
    return flags


# ── Wales — Approved Document B Volume 2 ─────────────────────────────────

def _check_wales(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])
    stair_flights = summary.get("stair_flights", [])

    if "WAL-R1" in regs:
        flags += _storey_height_provision_check(storeys, regs["WAL-R1"], len(stairs), "stair")
    if "WAL-R2" in regs:
        flags += _storey_height_provision_check(storeys, regs["WAL-R2"], len(summary["emergency_exits"]), "escape route")
    if "WAL-R3" in regs:
        flags += window_area(summary["windows"], regs, "WAL-R3")
    if "WAL-R4" in regs:
        flags += _fire_suppression_presence_check(summary.get("fire_suppression_terminals", []), regs["WAL-R4"])
    if "WAL-R5" in regs:
        flags += _window_sill_height_check(summary["windows"], regs["WAL-R5"])
    if "WAL-R6" in regs:
        flags += _basement_manual_review(storeys, regs["WAL-R6"])
    if "WAL-R7" in regs:
        flags += _stair_flight_width_check(stair_flights, regs["WAL-R7"])
    if "WAL-R8" in regs:
        flags += _passenger_lift_manual_review(storeys, summary.get("elevators", []), regs["WAL-R8"])
    if "WAL-R9" in regs:
        flags += _smoke_alarm_check(summary.get("smoke_alarms", []), summary["doors"], regs["WAL-R9"])
    if "WAL-R10" in regs:
        flags += _protected_stairway_manual_review(storeys, stairs, regs["WAL-R10"])
    return flags


# ── Northern Ireland — Technical Booklet E (2012) ────────────────────────

def _check_northern_ireland(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])

    if "NI-R1" in regs:
        flags += _storey_height_provision_check(storeys, regs["NI-R1"], len(stairs), "stair")
    if "NI-R2" in regs:
        flags += _storey_height_provision_check(storeys, regs["NI-R2"], len(summary["emergency_exits"]), "protected stairway")
    if "NI-R3" in regs:
        flags += window_area(summary["windows"], regs, "NI-R3")
    if "NI-R4" in regs:
        flags += window_size(summary["windows"], regs, "NI-R4")
    if "NI-R5" in regs:
        flags += _window_sill_height_check(summary["windows"], regs["NI-R5"])
    if "NI-R6" in regs:
        flags += _basement_manual_review(storeys, regs["NI-R6"])
    if "NI-R7" in regs:
        flags += _smoke_alarm_check(summary.get("smoke_alarms", []), summary["doors"], regs["NI-R7"])
    if "NI-R8" in regs:
        flags += _space_area_limit_check(summary["spaces"], regs["NI-R8"])
    if "NI-R9" in regs:
        flags += _passenger_lift_manual_review(storeys, summary.get("elevators", []), regs["NI-R9"])
    if "NI-R10" in regs:
        flags += _external_stair_manual_review(storeys, stairs, regs["NI-R10"])
    return flags


# ── Scotland — Technical Handbooks 2022 ──────────────────────────────────

def _check_scotland(summary, regs):
    flags = []
    storeys = summary.get("storeys", [])
    stairs = summary.get("stairs", [])
    walls = summary.get("walls", [])

    if "SCO-R1" in regs:
        flags += _storey_height_provision_check(storeys, regs["SCO-R1"], len(summary["emergency_exits"]), "escape route")
    if "SCO-R2" in regs:
        flags += _protected_enclosure_manual_review(storeys, walls, regs["SCO-R2"])
    if "SCO-R3" in regs:
        flags += _storey_height_provision_check(storeys, regs["SCO-R3"], len(summary["emergency_exits"]), "alternative exit route")
    if "SCO-R4" in regs:
        flags += window_area(summary["windows"], regs, "SCO-R4")
    if "SCO-R5" in regs:
        flags += _fire_suppression_presence_check(summary.get("fire_suppression_terminals", []), regs["SCO-R5"])
    if "SCO-R6" in regs:
        flags += _lobby_manual_review(summary["corridors"], regs["SCO-R6"])
    if "SCO-R7" in regs:
        flags += _basement_manual_review(storeys, regs["SCO-R7"])
    if "SCO-R8" in regs:
        flags += _space_area_limit_check(summary["spaces"], regs["SCO-R8"])
    if "SCO-R9" in regs:
        flags += _wall_proximity_manual_review(walls, regs["SCO-R9"])
    if "SCO-R10" in regs:
        flags += _external_stair_manual_review(storeys, stairs, regs["SCO-R10"])
    return flags


# ── Public entry point ────────────────────────────────────────────────────

_CHECKERS = {
    "england":          _check_england,
    "wales":            _check_wales,
    "northern_ireland": _check_northern_ireland,
    "scotland":         _check_scotland,
}


def check_all_rules(summary, jurisdiction="england"):
    """
    Runs every implemented regulation check against a parsed IFC summary
    (from ifc_parser.get_summary) for the given jurisdiction.
    Returns a flat list of flag dicts — one per violation or manual
    review note found.
    """
    jurisdiction = jurisdiction.lower()
    regs = load_regs(jurisdiction)
    checker = _CHECKERS.get(jurisdiction)
    if checker is None:
        raise ValueError(f"Unknown jurisdiction: {jurisdiction}")
    return checker(summary, regs)


if __name__ == "__main__":
    import sys
    from ifc_parser import get_summary

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )

    print("Loading IFC file...")
    summary = get_summary(ifc_path)

    for jurisdiction in ["england", "wales", "northern_ireland", "scotland"]:
        violations = check_all_rules(summary, jurisdiction=jurisdiction)
        print(f"\n=== {jurisdiction.upper()} — {len(violations)} flag(s) ===")
        for flag in violations:
            rule = flag["rule"]
            print(f"  [{rule['severity_level'].upper()}] {rule['unique_id']} — "
                  f"{flag['element_type']} — {flag['element_name']}")
            print(f"  {flag['issue']}\n")

