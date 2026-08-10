#This focuses on how many people are likely to be in a room based on certain rules according to the building data

import math
import re

occupancy_load_factors = {
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

jurisdiction_source = {
    "england":          "Approved Document B, Table C1 (floor space factors)",
    "wales":            "Approved Document B, Table C1 (floor space factors)",
    "scotland":         "Building Standards Technical Handbook (Non-domestic), Table 2.10 (occupancy load factors)",
    "northern_ireland": "Technical Booklet E (occupancy load factors)",
}

non_occupable = {"circulation", "stair", "plant", "sanitary", "measurement_zone"}

apartment_room_types = {"bedroom", "living", "kitchen", "kitchen_living", "dining", "sauna"}

def _source(jurisdiction):
    return jurisdiction_source.get((jurisdiction or "england").lower(),
                                   jurisdiction_source["england"])

room_count = re.compile(r"(\d+)\s*h\b")

def dwelling_occupants(long_name):
    match = room_count.search((long_name or "").lower())
    if match:
        rooms = int(match.group(1))
        occupants = rooms + 1
        return occupants, (
            f"{rooms} habitable rooms (from '{long_name}') + 1 -> {occupants} persons "
            f"(design occupancy ~ NDSS bedspaces, e.g. 2b4p/3b5p)"
        )
    return None, None

def occupant_load(space, use_type, on_dwelling_storey=False, jurisdiction="england"):
    area = space.get("area")
    source = _source(jurisdiction)

    if use_type in non_occupable:
        return {"occupant_load": 0, "occupant_basis": f"non-occupiable ({use_type}); excluded from "
                f"occupancy per {source}", "factor_source": None, "not_assessed": None}

    if on_dwelling_storey and use_type in apartment_room_types:
        return {"occupant_load": 0,
                "occupant_basis": f"{use_type} within an apartment — occupants counted at the "
                                  f"dwelling-unit level for this storey (not re-counted)",
                "factor_source": None, "not_assessed": None}

    if use_type == "unknown":
        return {"occupant_load": None, "occupant_basis": None, "factor_source": None,
                "not_assessed": "use_type unresolved; occupant load not estimated"}

    if use_type == "dwelling":
        occupants, basis = dwelling_occupants(space.get("long_name"))
        if occupants is not None:
            return {"occupant_load": occupants, "occupant_basis": basis,
                    "factor_source": "dwelling design occupancy (NDSS bedspaces)", "not_assessed": None}

    entry = occupancy_load_factors.get(use_type)
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
