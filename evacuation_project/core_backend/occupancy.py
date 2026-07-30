"""Occupant-load estimation (deterministic).

Turns a space's use-type + floor area into an estimated occupant load. This is *computation*, not
AI: the numbers come from the published occupancy load factors, and the AI-chosen ``use_type`` only
selects which factor applies.

The factors are the **code occupancy load factors** — the floor-area-per-person figures given in
Approved Document B Table C1 (England/Wales), the Scottish Technical Handbook (Non-domestic) Table
2.10, and NI Technical Booklet E. These agree across the UK documents, so one table is carried and
only the citation differs by jurisdiction. Occupant load = area / factor (ADB/Scottish TH method).

Two things the guidance is explicit about, reflected here:
  * stairs, corridors, toilets and plant rooms are excluded from occupancy (kept at load 0);
  * storage and car parks DO carry a (sparse) load — 30 m^2/person — so they are counted.

Dwellings are NOT covered by these factors (a home's rooms are not counted like a place of assembly).
Their occupancy is a bedspace-based design figure: persons = habitable rooms + 1, aligned with the
Nationally Described Space Standard bedspaces (e.g. 2b4p, 3b5p).

Every estimate carries an ``occupant_basis`` string citing exactly how it was derived. Where a load
cannot be estimated (unresolved use-type, missing area), the space is surfaced as ``not_assessed``
rather than silently treated as empty. This is a code-based occupant-capacity *estimate*: the
use-type -> category mapping and the dwelling bedspace assumption are stated modelling choices, not a
certified occupancy schedule (non-verdict).
"""

import math
import re

# Code occupancy load factors (m^2 per person), per Approved Document B Table C1 / Scottish Technical
# Handbook (Non-domestic) Table 2.10. Value paired with the code category the use-type is mapped to.
# communal_amenity is a heterogeneous bucket (gym / laundry / common room) -> mapped conservatively to
# the "common room" factor; a gym sub-type would be sparser. sauna has no code category -> an
# explicitly-labelled project default (communal saunas only; in-apartment ones are zeroed below).
OCCUPANCY_LOAD_FACTORS = {
    "commercial":       (6.0,  "office"),
    "communal_amenity": (1.0,  "common/assembly room"),
    "dining":           (1.0,  "dining room / restaurant"),
    "kitchen":          (7.0,  "kitchen"),
    "kitchen_living":   (1.0,  "lounge"),
    "living":           (1.0,  "lounge / common room"),
    "bedroom":          (8.0,  "bedroom"),
    "storage":          (30.0, "storage / warehousing"),
    "parking":          (30.0, "car park"),
    "sauna":            (4.0,  "project default — no named code category"),
}

# Jurisdiction -> the document the factors are cited from (values are common across the UK guidance).
JURISDICTION_SOURCE = {
    "england":          "Approved Document B, Table C1 (floor space factors)",
    "wales":            "Approved Document B, Table C1 (floor space factors)",
    "scotland":         "Building Standards Technical Handbook (Non-domestic), Table 2.10 (occupancy load factors)",
    "northern_ireland": "Technical Booklet E (occupancy load factors)",
}

# Excluded from occupancy per the guidance ("take care not to include stairs, exit routes (corridors),
# toilets, plant rooms"). Occupant load = 0, not "not assessed" -- these have no standalone design
# occupancy. NB storage and car parks are NOT here: the code gives them a (sparse) load factor above.
NON_OCCUPIABLE = {"circulation", "stair", "plant", "sanitary", "measurement_zone"}

# On a storey modelled with whole-apartment (dwelling) spaces, the individual habitable rooms are the
# SAME floor area modelled a second time -- IFC carries no parent link between an apartment and its
# rooms, so they overlap as siblings. Counting both double-counts residents. On such storeys the
# apartment unit is the sole occupancy carrier and these constituent room-types contribute zero
# standalone load (option 1). Genuinely communal spaces on the floor (communal_amenity/commercial)
# are NOT apartment sub-rooms, so they keep their own load.
APARTMENT_ROOM_TYPES = {"bedroom", "living", "kitchen", "kitchen_living", "dining", "sauna"}


def _source(jurisdiction):
    return JURISDICTION_SOURCE.get((jurisdiction or "england").lower(),
                                   JURISDICTION_SOURCE["england"])

# Finnish dwelling notation: a leading room count, e.g. "3H+K+WC+KPH" -> 3 habitable rooms.
_ROOM_COUNT_RE = re.compile(r"(\d+)\s*h\b")


def _dwelling_occupants(long_name):
    """Estimate occupants for a whole-apartment space from its room-count notation, or (None, None).

    persons = habitable rooms + 1 -- a bedspace-based design occupancy aligned with the Nationally
    Described Space Standard (each habitable room a potential bedspace, + 1), e.g. a 3-room flat ~ 4p.
    """
    match = _ROOM_COUNT_RE.search((long_name or "").lower())
    if match:
        rooms = int(match.group(1))
        occupants = rooms + 1
        return occupants, (
            f"{rooms} habitable rooms (from '{long_name}') + 1 -> {occupants} persons "
            f"(design occupancy ~ NDSS bedspaces, e.g. 2b4p/3b5p)"
        )
    return None, None


def occupant_load(space, use_type, on_dwelling_storey=False, jurisdiction="england"):
    """Estimate the occupant load for one space.

    Returns a dict: ``{occupant_load, occupant_basis, factor_source, not_assessed}``.
    ``occupant_load`` is None when it cannot be estimated; ``not_assessed`` then states why.
    ``on_dwelling_storey`` marks a space whose storey is modelled with apartment-unit spaces, so its
    load is carried by the dwelling and this room must not be re-counted (see APARTMENT_ROOM_TYPES).
    ``jurisdiction`` selects which code document the factor citation points at.
    """
    area = space.get("area")
    source = _source(jurisdiction)

    if use_type in NON_OCCUPIABLE:
        return {"occupant_load": 0, "occupant_basis": f"non-occupiable ({use_type}); excluded from "
                f"occupancy per {source}", "factor_source": None, "not_assessed": None}

    if on_dwelling_storey and use_type in APARTMENT_ROOM_TYPES:
        return {"occupant_load": 0,
                "occupant_basis": f"{use_type} within an apartment — occupants counted at the "
                                  f"dwelling-unit level for this storey (not re-counted)",
                "factor_source": None, "not_assessed": None}

    if use_type == "unknown":
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": "use_type unresolved; occupant load not estimated"}

    if use_type == "dwelling":
        occupants, basis = _dwelling_occupants(space.get("long_name"))
        if occupants is not None:
            return {"occupant_load": occupants, "occupant_basis": basis,
                    "factor_source": "dwelling design occupancy (NDSS bedspaces)", "not_assessed": None}
        # no parseable room count -> fall through to a floor-space factor if one exists

    entry = OCCUPANCY_LOAD_FACTORS.get(use_type)
    if entry is None:
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": f"no occupancy load factor defined for use_type '{use_type}'"}
    factor, category = entry
    if area is None:
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": f"{use_type} space has no area; occupant load not estimated"}

    occupants = max(1, math.ceil(area / factor))
    return {
        "occupant_load": occupants,
        "occupant_basis": f"{use_type}: {area:.1f} m2 / {factor:.0f} m2/person "
                          f"({category}) = {occupants} persons",
        "factor_source": source,
        "not_assessed": None,
    }
