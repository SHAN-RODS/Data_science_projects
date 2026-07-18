"""Space-use classification.

Maps each IfcSpace onto a controlled vocabulary of use-types. This is the AI-1 stage of the
pipeline, but AI is used only where a formula cannot decide: the deterministic keyword dictionary
below resolves the clean, common labels (which in most exports live in ``IfcSpace.LongName``), and
only the spaces it cannot confidently resolve are sent to the LLM (see ``classify_llm``, added in a
later phase). Every result carries the source space GUID so downstream stages stay traceable.

The dictionary handles multilingual tokens (English + observed Nordic/Finnish/German) and the
Finnish apartment notation (e.g. ``3H+K+WC+KPH`` = 3 rooms + kitchen + WC + bathroom) via a
dwelling-unit regex that runs before the room-level keywords.
"""

import re
import unicodedata
from typing import List

from pydantic import BaseModel, Field

# Controlled vocabulary. "unknown" is the residue the LLM pass resolves; occupancy treats it and
# the non-occupiable types (circulation/stair/plant/parking/storage) conservatively.
USE_TYPES = [
    "bedroom", "living", "kitchen", "kitchen_living", "dining", "dwelling",
    "circulation", "stair", "sanitary", "sauna", "storage", "plant",
    "parking", "communal_amenity", "commercial", "measurement_zone", "unknown",
]

# Keyword groups evaluated in this priority order (first hit wins). Ordering resolves overlaps:
# measurement zones first (they overlap real rooms and must not count as occupiable); then
# stair/sauna/sanitary before generic rooms; storage ("STORE COMMON") before communal ("COMMUNAL").
KEYWORD_GROUPS = [
    # BIM analysis overlays, not egress spaces — excluded from occupant load, never "not_assessed".
    ("measurement_zone", ["gfa", "gross floor", "netarea", "net area", "heated netarea",
                          "volume", "bruttoareal", "floor area"]),
    ("stair", ["staircase", "stair", "stairwell", "trapperom", "trapp", "porras"]),
    ("sauna", ["sauna", "bastu"]),
    ("sanitary", ["bathroom", "toilet", "shower", "washing", "restroom", "lavatory",
                  "kylpyhuone", "kph", "pesuhuone", "dusj", "wc", "bad"]),
    ("kitchen", ["kitchen", "keittio", "kjokken", "kok"]),
    ("bedroom", ["bedroom", "soverom", "makuuhuone", "schlafzimmer"]),
    ("living", ["living", "lounge", "sitting", "stue", "olohuone"]),
    ("dining", ["dining", "ruokailu", "spisestue"]),
    ("parking", ["parking", "garage", "carport", "pysakointi"]),
    ("storage", ["storage", "store", "closet", "locker", "utility", "varasto", "bod"]),
    ("plant", ["mechanical", "electrical", "boiler", "heating", "ventilation", "vent",
               "shaft", "chase", "riser", "technical", "plant", "lift", "elevator", "hiss"]),
    ("communal_amenity", ["gym", "communal", "community", "laundry", "amenity",
                          "meeting", "common"]),
    ("commercial", ["business", "office", "retail", "commercial", "cafe", "restaurant", "shop"]),
    ("circulation", ["corridor", "hallway", "passage", "lobby", "landing", "vestibule",
                     "foyer", "entrance", "turning free space", "turning", "circulation",
                     "aula", "kaytava", "hall"]),
]

# Confidence assigned to a deterministic match (the LLM pass supplies its own for "unknown").
_KEYWORD_CONFIDENCE = 0.9
_DWELLING_CONFIDENCE = 0.75

# Finnish dwelling-unit notation: a room count followed by 'h' (huonetta), e.g. "3h+k+wc".
_DWELLING_RE = re.compile(r"\b\d+\s*h\b")


def normalize(*parts):
    """Lowercase, strip accents/mojibake, and split separators (``/ + : -``) into spaces."""
    text = " ".join(p for p in parts if p)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify_name(long_name, name):
    """Classify a single (long_name, name) pair with the dictionary.

    Returns (use_type, confidence, source). ``source`` is "dictionary" for a hit, else "none".
    """
    text = normalize(long_name, name)
    if not text:
        return "unknown", 0.0, "none"
    # dwelling-unit notation takes precedence over the room keywords it contains (wc, k, ...)
    if _DWELLING_RE.search(text):
        return "dwelling", _DWELLING_CONFIDENCE, "dictionary"
    for use_type, keywords in KEYWORD_GROUPS:
        for kw in keywords:
            if kw in text:
                return use_type, _KEYWORD_CONFIDENCE, "dictionary"
    return "unknown", 0.0, "none"


def classify_dictionary(space):
    """Dictionary classification for one parser space dict. Carries the source GUID."""
    use_type, confidence, source = classify_name(space.get("long_name"), space.get("name"))
    return {
        "guid": space["id"],
        "name": space.get("name"),
        "long_name": space.get("long_name"),
        "use_type": use_type,
        "use_type_confidence": confidence,
        "use_type_source": source,
    }


# --- AI stage 1: LLM classification of the dictionary's "unknown" residue -----------------------
#
# The LLM is used only where a formula (the dictionary) cannot decide: messy, ambiguous, or
# multilingual names. It is grounded (given only name/long_name/area/storey), constrained to the
# controlled vocabulary, run at temperature 0, and cached by normalised name so repeated names cost
# a single call. The output is validated against USE_TYPES; anything off-list becomes "unknown".

_LLM_USE_TYPES = ", ".join(u for u in USE_TYPES if u != "unknown")

_LLM_INSTRUCTIONS = (
    "You classify spaces from a building's IFC model into a controlled vocabulary of use-types, "
    "for a fire-evacuation analysis. For each space choose the single best use_type from this list:\n"
    f"  {_LLM_USE_TYPES}, unknown\n"
    "Guidance: 'measurement_zone' is a BIM area/volume overlay, not a real room; 'dwelling' is a "
    "whole apartment; 'circulation' covers corridors/lobbies/landings; 'plant' covers shafts, risers "
    "and technical rooms. Base your choice ONLY on the provided name, long_name, area and storey — do "
    "not invent facts. If a space is genuinely undeterminable, return 'unknown' with low confidence. "
    "Return one classification per space, using the given index."
)


class SpaceClassification(BaseModel):
    index: int = Field(description="index of the space in the provided list")
    use_type: str = Field(description="one use-type from the allowed vocabulary")
    confidence: float = Field(description="confidence between 0 and 1", ge=0, le=1)


class SpaceClassificationList(BaseModel):
    classifications: List[SpaceClassification]


def _llm_prompt(items):
    lines = [_LLM_INSTRUCTIONS, "", "Spaces to classify:"]
    for index, (_, sample) in enumerate(items):
        area = sample.get("area")
        storey = (sample.get("storey") or {}).get("name")
        lines.append(
            f"[{index}] name={sample.get('name')!r} long_name={sample.get('long_name')!r} "
            f"area_m2={round(area, 1) if area else None} storey={storey!r}"
        )
    return "\n".join(lines)


def classify_llm(unresolved, llm=None):
    """Classify the dictionary's unresolved spaces with the LLM.

    ``unresolved`` is a list of parser space dicts. Returns a dict keyed by normalised name
    (``normalize(long_name, name)``) -> ``{use_type, confidence}``. Empty on failure or no input.
    """
    if not unresolved:
        return {}

    # dedup by normalised name so repeated names (e.g. many "ROOM"s) cost one classification
    distinct = {}
    for space in unresolved:
        key = normalize(space.get("long_name"), space.get("name"))
        distinct.setdefault(key, space)
    items = list(distinct.items())

    if llm is None:
        from core_backend.llm import select_llm
        llm, _ = select_llm()

    try:
        structured = llm.with_structured_output(SpaceClassificationList)
        result = structured.invoke(_llm_prompt(items))
    except Exception as exc:  # noqa: BLE001 - leave spaces as "unknown" so they surface downstream
        print(f"LLM classification unavailable ({exc}); leaving spaces as 'unknown'.")
        return {}

    allowed = set(USE_TYPES)
    out = {}
    for c in result.classifications:
        if 0 <= c.index < len(items):
            key = items[c.index][0]
            use_type = c.use_type if c.use_type in allowed else "unknown"
            out[key] = {"use_type": use_type, "confidence": float(c.confidence)}
    return out


def classify_spaces(spaces, use_llm=True, llm=None):
    """Classify all spaces: dictionary first, LLM for the "unknown" residue.

    Set ``use_llm=False`` for a deterministic, API-free pass (used by the offline eval).
    """
    results = [classify_dictionary(s) for s in spaces]
    if not use_llm:
        return results

    unresolved_idx = [i for i, r in enumerate(results) if r["use_type"] == "unknown"]
    if not unresolved_idx:
        return results

    llm_map = classify_llm([spaces[i] for i in unresolved_idx], llm=llm)
    for i in unresolved_idx:
        key = normalize(spaces[i].get("long_name"), spaces[i].get("name"))
        if key in llm_map:
            results[i]["use_type"] = llm_map[key]["use_type"]
            results[i]["use_type_confidence"] = llm_map[key]["confidence"]
            results[i]["use_type_source"] = "llm"
    return results


if __name__ == "__main__":
    import sys
    from collections import Counter
    from core_backend.ifc_parser import parser_summary
    from core_backend.sample_paths import resolve_ifc

    # dictionary-only by default (no API cost); pass --llm to also run the LLM on the residue
    use_llm = "--llm" in sys.argv
    args = [a for a in sys.argv if not a.startswith("--")]
    summary = parser_summary(resolve_ifc(args))

    classified = classify_spaces(summary["spaces"], use_llm=use_llm)
    dist = Counter(c["use_type"] for c in classified)
    label = "dictionary + LLM" if use_llm else "dictionary only"
    print(f"Classified {len(classified)} spaces ({label}):")
    for use_type, count in dist.most_common():
        print(f"  {use_type:18} {count}")

    if use_llm:
        llm_resolved = [c for c in classified if c["use_type_source"] == "llm"]
        print(f"\nLLM resolved {len(llm_resolved)} previously-unknown spaces. Examples:")
        seen = set()
        for c in llm_resolved:
            key = (c["long_name"], c["name"].split(":")[0] if c["name"] else None)
            if key in seen:
                continue
            seen.add(key)
            print(f"  long_name={c['long_name']!r:16} name~={key[1]!r:12} -> "
                  f"{c['use_type']} (conf {c['use_type_confidence']})")
    else:
        unresolved = [c for c in classified if c["use_type"] == "unknown"]
        distinct = sorted(
            {(c["long_name"], c["name"].split(":")[0] if c["name"] else None) for c in unresolved},
            key=lambda pair: (pair[0] or "", pair[1] or ""),
        )
        print(f"\nUnresolved -> LLM pass: {len(unresolved)} spaces, "
              f"{len(distinct)} distinct name(s). Run with --llm to classify them.")
        for long_name, name in distinct[:25]:
            print(f"  long_name={long_name!r:20} name~={name!r}")
