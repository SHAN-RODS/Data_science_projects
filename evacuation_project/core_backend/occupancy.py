"""Occupant-load estimation (deterministic).

Turns a space's use-type + floor area into an estimated occupant load. This is *computation*, not
AI: the numbers come from formulas, and the AI-chosen ``use_type`` only selects which factor applies.

The factors here are **indicative, not compliance-grade** — travel/occupancy figures in this project
are approximations, and every estimate carries an ``occupant_basis`` string stating exactly how it was
derived. Where a load cannot be estimated (unresolved use-type, missing area), the space is surfaced
as ``not_assessed`` rather than silently treated as empty.

The dwelling-occupancy basis is a known open question for the supervisor (Plan.md §13.3); the tables
below are module-level so they are easy to revise once that is settled.
"""

import math
import re

# Indicative floor-area-per-person factors (m^2/person). NOT compliance-grade.
AREA_FACTORS_M2_PER_PERSON = {
    "bedroom": 8.0,
    "living": 5.0,
    "kitchen": 8.0,
    "kitchen_living": 5.0,
    "dining": 4.0,
    "sauna": 4.0,
    "communal_amenity": 2.0,
    "commercial": 6.0,
}

# Spaces that do not carry a design occupant load of their own (people are counted in the rooms
# they occupy, not in the routes/plant they pass through). Occupant load = 0, not "not assessed".
NON_OCCUPIABLE = {"circulation", "stair", "plant", "parking", "storage", "measurement_zone"}

FACTOR_SOURCE = "indicative area factor (approx, not compliance-grade)"

# Finnish dwelling notation: a leading room count, e.g. "3H+K+WC+KPH" -> 3 habitable rooms.
_ROOM_COUNT_RE = re.compile(r"(\d+)\s*h\b")


def _dwelling_occupants(long_name):
    """Estimate occupants for a whole-apartment space from its room-count notation, or (None, None)."""
    match = _ROOM_COUNT_RE.search((long_name or "").lower())
    if match:
        rooms = int(match.group(1))
        occupants = rooms + 1  # habitable rooms + 1 (indicative dwelling design occupancy)
        return occupants, (
            f"{rooms} habitable rooms (from '{long_name}') -> {occupants} persons "
            f"(rooms + 1, indicative dwelling occupancy)"
        )
    return None, None


def occupant_load(space, use_type):
    """Estimate the occupant load for one space.

    Returns a dict: ``{occupant_load, occupant_basis, factor_source, not_assessed}``.
    ``occupant_load`` is None when it cannot be estimated; ``not_assessed`` then states why.
    """
    area = space.get("area")

    if use_type in NON_OCCUPIABLE:
        return {"occupant_load": 0, "occupant_basis": f"non-occupiable ({use_type})",
                "factor_source": None, "not_assessed": None}

    if use_type == "unknown":
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": "use_type unresolved; occupant load not estimated"}

    if use_type == "dwelling":
        occupants, basis = _dwelling_occupants(space.get("long_name"))
        if occupants is not None:
            return {"occupant_load": occupants, "occupant_basis": basis,
                    "factor_source": "dwelling room-count (indicative)", "not_assessed": None}
        # no parseable room count -> fall through to an area factor if one exists

    factor = AREA_FACTORS_M2_PER_PERSON.get(use_type)
    if factor is None:
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": f"no occupancy factor defined for use_type '{use_type}'"}
    if area is None:
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": f"{use_type} space has no area; occupant load not estimated"}

    occupants = max(1, math.ceil(area / factor))
    return {
        "occupant_load": occupants,
        "occupant_basis": f"area {area:.1f} m2 / {factor:.0f} m2/person ({use_type}, indicative)",
        "factor_source": FACTOR_SOURCE,
        "not_assessed": None,
    }
