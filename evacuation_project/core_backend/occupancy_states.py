#The occupancy states a scenario can be run in, and what each one does to the computed room loads.
#
# These multipliers used to be the model's to invent, one per use type per scenario, and it was the
# wrong job to hand it. The multipliers decide the headcount, but the headcount is computed
# downstream and the model is told not to state one - so it was choosing numbers whose effect it
# could not see, against an inequality (night must hold at least as many residents as day) it had no
# aggregate to check. It got the direction wrong on most runs, and each wrong reply cost a full
# regeneration of every scenario.
#
# Which state a building is worth testing in is judgement, and the model still makes that call. What
# a named state DOES to a room type is not judgement - it is a standing engineering assumption, the
# same one every time, and it belongs in a table that can be read, cited and argued with. Putting it
# here makes the inversion structurally impossible instead of checked: see check_states().

# Use types move together, so the multiplier is set per family and a state overrides only where it
# has something specific to say. Non-occupiable types (circulation, stair, sanitary, plant,
# measurement_zone) never carry a computed load, so they need no entry.
use_type_families = {
    "dwelling": "residential",
    "bedroom": "residential",
    "living": "residential",
    "kitchen": "residential",
    "kitchen_living": "residential",
    "dining": "residential",
    "communal_amenity": "amenity",
    "gym": "amenity",
    "laundry": "amenity",
    "sauna": "amenity",
    "commercial": "commercial",
    "storage": "ancillary",
    "parking": "ancillary",
}

families = ("residential", "amenity", "commercial", "ancillary")

# The residential family is what the night/day direction is judged on. Amenity is deliberately out
# of it: emptying a residents' lounge overnight is correct, and counting it made a correct night
# state read as an inversion on any building with a large amenity.
residential_use_types = {ut for ut, family in use_type_families.items()
                         if family == "residential"}

occupancy_states = {
    "night_sleeping": {
        "label": "Night - residents at home and asleep",
        "period": "night",
        "summary": "About 01:00. Everyone who lives here is in, and asleep. Shared facilities and "
                   "any workplace are shut.",
        "families": {"residential": 1.0, "amenity": 0.0, "commercial": 0.0, "ancillary": 0.0},
        "overrides": {},
        "reasons": {
            "residential": "residents are at home and asleep - the sleeping risk profile, and the "
                           "design case for a residential block: the fullest the dwellings ever "
                           "are, with the longest pre-movement time",
            "amenity": "shared facilities are closed overnight",
            "commercial": "any workplace in the building is closed overnight",
            "ancillary": "stores and parking are not occupied at this hour",
        },
    },
    "early_morning": {
        "label": "Early morning - residents waking and leaving",
        "period": "transitional",
        "summary": "About 07:30. Most residents are still in but awake and moving; the earliest "
                   "leavers have gone and any workplace is opening up.",
        "families": {"residential": 0.85, "amenity": 0.15, "commercial": 0.4, "ancillary": 0.25},
        "overrides": {"gym": 0.4, "sauna": 0.0},
        "reasons": {
            "residential": "most residents are still at home but awake - a shorter pre-movement "
                           "time than the night state over a nearly comparable population",
            "amenity": "shared facilities see light individual use before work; the gym takes its "
                       "morning peak and the sauna is not in use",
            "commercial": "any workplace is opening and part-staffed",
            "ancillary": "residents collecting bicycles and cars on their way out",
        },
    },
    "working_day": {
        "label": "Working day - residents out, workplace full",
        "period": "day",
        "summary": "About 14:00 on a weekday. Most residents are out at work, school or elsewhere; "
                   "any commercial space is at its design occupancy.",
        "families": {"residential": 0.3, "amenity": 0.5, "commercial": 1.0, "ancillary": 0.2},
        "overrides": {"sauna": 0.1},
        "reasons": {
            "residential": "most residents are out; those left are shift workers, carers, retired "
                           "or unwell residents and pre-school children",
            "amenity": "shared facilities in mid-level daytime use",
            "commercial": "any workplace in the building is at its full code-derived occupancy - "
                          "the state that puts the most people into non-residential space",
            "ancillary": "occasional daytime access to stores and parking",
        },
    },
    "evening_communal": {
        "label": "Evening - residents in, shared facilities busiest",
        "period": "transitional",
        "summary": "About 20:00. Residents are back and awake, using living and dining space and "
                   "the shared facilities, which are at their busiest.",
        "families": {"residential": 0.9, "amenity": 1.0, "commercial": 0.3, "ancillary": 0.2},
        "overrides": {"bedroom": 0.6, "living": 1.0, "dining": 1.0},
        "reasons": {
            "residential": "residents are home and awake, in living and dining space rather than "
                           "bedrooms - the same people standing in different rooms from the night "
                           "state, which is what makes this a distinct evacuation to simulate",
            "amenity": "the peak for a residents' lounge, gym or sauna, and the state that puts "
                       "the most people into the shared parts of the building at once",
            "commercial": "any workplace is winding down",
            "ancillary": "light evening use of stores and parking",
        },
    },
    "weekend_daytime": {
        "label": "Weekend daytime - residents in and mobile",
        "period": "day",
        "summary": "About 11:00 on a Saturday. Most residents are in and awake, moving between "
                   "their flats and the shared facilities; any workplace is quiet.",
        "families": {"residential": 0.75, "amenity": 0.8, "commercial": 0.3, "ancillary": 0.35},
        "overrides": {"bedroom": 0.55, "gym": 0.9},
        "reasons": {
            "residential": "most residents are in but awake and dispersed, and unlike a weekday "
                           "they are moving around the building rather than out of it",
            "amenity": "the weekend is the busy time for a gym and a residents' lounge",
            "commercial": "any workplace is quiet or closed at the weekend",
            "ancillary": "the busiest state for stores and parking - residents loading cars, "
                         "reaching bike and bin stores",
        },
    },
}


def state_keys():
    return list(occupancy_states)


def label_of(key):
    entry = occupancy_states.get(key)
    return entry["label"] if entry else key


def period_of(key):
    """'night', 'day' or 'transitional'. Only night and day are compared for direction - the two
    transitional states sit between them by construction and comparing them says nothing."""
    entry = occupancy_states.get(key)
    return entry["period"] if entry else None


def multiplier_map(key):
    """Every use type this state touches, as {use_type: multiplier}."""
    entry = occupancy_states[key]
    values, overrides = entry["families"], entry["overrides"]
    return {use_type: overrides.get(use_type, values[family])
            for use_type, family in use_type_families.items()}


def multipliers_for(key, use_types=None):
    """The state's multipliers in the shape the scenario object carries them: one record per use
    type, each with the reason it moves that way. ``use_types`` narrows the list to the types the
    building actually has, so a scenario is not padded with rows for rooms nobody has."""
    entry = occupancy_states[key]
    values = multiplier_map(key)
    wanted = set(values) if use_types is None else set(use_types)
    return [{"use_type": use_type,
             "multiplier": values[use_type],
             "reason": entry["reasons"][use_type_families[use_type]]}
            for use_type in use_type_families
            if use_type in wanted]


def occupied_use_types(spaces):
    """The use types in this building that actually carry a computed occupant load. A type with no
    load has nothing for a multiplier to do."""
    return {s.get("use_type") for s in spaces if (s.get("occupant_load") or 0) > 0}


def occupants_under(spaces, key):
    """How many occupants this state leaves in the building. The same arithmetic
    occupant_placement.scenario_weights uses to seat them, so the two cannot disagree."""
    values = multiplier_map(key)
    return int(sum((s.get("occupant_load") or 0) * values.get(s.get("use_type"), 1.0)
                   for s in spaces if (s.get("occupant_load") or 0) > 0))


def residential_occupants(spaces, key):
    """How many occupants a state leaves in residential space - what the night/day direction is
    judged on."""
    values = multiplier_map(key)
    return int(sum((s.get("occupant_load") or 0) * values.get(s.get("use_type"), 1.0)
                   for s in spaces
                   if s.get("use_type") in residential_use_types
                   and (s.get("occupant_load") or 0) > 0))


def state_menu(spaces=None):
    """The states written out for the generation prompt. Where the building's spaces are known each
    state carries the headcount its multipliers actually produce - the aggregate the model used to
    be asked to reason about without ever being shown it."""
    lines = []
    for key, entry in occupancy_states.items():
        line = f"  - {key} - {entry['label']}. {entry['summary']}"
        if spaces is not None:
            line += f" Occupants under this state: {occupants_under(spaces, key)}."
        lines.append(line)
    return "\n".join(lines)


def check_states():
    """The table's own invariants, asserted once at import rather than checked on every run.

    The last one is what replaces the retry loop: because occupant loads are never negative, a night
    state holding every residential use type at the schema maximum of 1.0 cannot seat fewer
    residents than any daytime state on ANY building. The inversion stops being something a reply
    can get wrong and becomes something the table makes unreachable."""
    faults = []
    nights = [k for k, v in occupancy_states.items() if v["period"] == "night"]
    if len(nights) != 1:
        faults.append(f"expected exactly one night state, found {nights}")

    seen = {}
    for key, entry in occupancy_states.items():
        for family in families:
            if family not in entry["families"]:
                faults.append(f"{key}: no multiplier for the {family} family")
            if family not in entry["reasons"]:
                faults.append(f"{key}: no reason given for the {family} family")
        for use_type in entry["overrides"]:
            if use_type not in use_type_families:
                faults.append(f"{key}: overrides an unknown use type {use_type!r}")
        if faults:
            continue
        for use_type, value in multiplier_map(key).items():
            if not 0.0 <= value <= 1.0:
                faults.append(f"{key}: {use_type} multiplier {value} is outside 0.0-1.0")
        signature = tuple(sorted(multiplier_map(key).items()))
        if signature in seen:
            faults.append(f"{key} and {seen[signature]} have identical multipliers - one of them "
                          f"is not a distinct scenario")
        seen[signature] = key

    if faults:
        return faults

    for night in nights:
        night_values = multiplier_map(night)
        for key, entry in occupancy_states.items():
            if entry["period"] != "day":
                continue
            day_values = multiplier_map(key)
            for use_type in sorted(residential_use_types):
                if day_values[use_type] > night_values[use_type]:
                    faults.append(
                        f"{key} puts {use_type} at {day_values[use_type]} but the night state "
                        f"{night} only reaches {night_values[use_type]} - a daytime state cannot "
                        f"hold more residents than the night one")
    return faults


startup_faults = check_states()
if startup_faults:
    raise ValueError("occupancy_states is inconsistent:\n  " + "\n  ".join(startup_faults))


if __name__ == "__main__":
    for key, entry in occupancy_states.items():
        print(f"\n{key} - {entry['label']}  [{entry['period']}]")
        print(f"  {entry['summary']}")
        for record in multipliers_for(key):
            print(f"    {record['use_type']:18} {record['multiplier']}")
