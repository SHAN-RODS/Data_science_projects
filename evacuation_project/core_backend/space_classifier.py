#It will map the Ifspace onto a controlled vocabulary having use-types. LLM is used only here if the deterministic keyword dictionary cannot 
#resolve the labels. so these are sent to the LLM to give the results

import re
import unicodedata
from typing import List

from pydantic import BaseModel, Field

import sys
from collections import Counter
from core_backend.ifc_parser import parser_summary
from core_backend.sample_paths import resolve_ifc

use_types = [
    "bedroom", "living", "kitchen", "kitchen_living", "dining", "dwelling",
    "circulation", "stair", "sanitary", "sauna", "storage", "plant",
    "parking", "communal_amenity", "gym", "laundry", "commercial",
    "measurement_zone", "unknown",
]

keyword_group = [
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
    ("gym", ["gym", "fitness", "treningsrom", "kuntosali", "weights room"]),
    ("laundry", ["laundry", "vaskeri", "vaskerom", "pesula", "pyykki"]),
    ("communal_amenity", ["communal", "community", "amenity", "meeting", "common"]),
    ("commercial", ["business", "office", "retail", "commercial", "cafe", "restaurant", "shop"]),
    ("circulation", ["corridor", "hallway", "passage", "lobby", "landing", "vestibule",
                     "foyer", "entrance", "turning free space", "turning", "circulation",
                     "aula", "kaytava", "hall"]),
]

keyword_confidence = 0.9
dwelling_confidence = 0.75
code_confidence = 0.85

dwelling_re = re.compile(r"\b\d+\s*h\b")

occupancy_code_prefixes = [
    ("0121-11-00","living"),            
    ("0121-12","sanitary"),          
    ("0121-64","kitchen"),
    ("0121-73","sanitary"),          
    ("0121-74","sauna"),
    ("0121-83","circulation"),       
    ("0121-92", "stair"),             
    ("0121-99", "plant"),             
    ("0121-94", "plant"),             
    ("0121-96", "plant"),             
    ("0121-52", "storage"),
    ("0121-71", "storage"),           
    ("0121-72", "communal_amenity"),  
    ("0121-55", "parking"),
    ("0121-47", "communal_amenity"),  
    ("0121-77", "communal_amenity"),  
    ("0121-22", "commercial"),        
    ("0121-11", "living"),            
]

def classify_code(occupancy_code):
    if not occupancy_code:
        return None, None
    code = str(occupancy_code).strip()
    for prefix, use_type in occupancy_code_prefixes:
        if code.startswith(prefix):
            return use_type,code_confidence
    return None, None


def normalize(*parts):
    text = " ".join(p for p in parts if p)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def classify_name(long_name, name):
    text = normalize(long_name, name)
    if not text:
        return "unknown", 0.0, "none"
    if dwelling_re.search(text):
        return "dwelling", dwelling_confidence, "dictionary"
    for use_type, keywords in keyword_group:
        for kw in keywords:
            if kw in text:
                return use_type, keyword_confidence, "dictionary"
    return "unknown", 0.0, "none"


def classify_dictionary(space):
    use_type, confidence, source = classify_name(space.get("long_name"), space.get("name"))
    if use_type == "unknown":
        code_type, code_conf = classify_code(space.get("occupancy_code"))
        if code_type is not None:
            use_type, confidence, source = code_type, code_conf, "occupancy_code"
    return {
        "guid": space["id"],
        "name": space.get("name"),
        "long_name": space.get("long_name"),
        "use_type": use_type,
        "use_type_confidence": confidence,
        "use_type_source": source,
    }


llm_use_types = ", ".join(u for u in use_types if u != "unknown")

llm_instructions = (
    "You classify spaces from a building's IFC model into a controlled vocabulary of use-types, "
    "for a fire-evacuation analysis. For each space choose the single best use_type from this list:\n"
    f"  {llm_use_types}, unknown\n"
    "Guidance: 'measurement_zone' is a BIM area/volume overlay, not a real room; 'dwelling' is a "
    "whole apartment; 'circulation' covers corridors/lobbies/landings; 'plant' covers shafts, risers "
    "and technical rooms. 'communal_amenity' is a shared lounge, common or meeting room - a gym or a "
    "laundry is NOT one, they have their own types, because they hold far fewer people per square "
    "metre than an assembly room does. Base your choice ONLY on the provided name, long_name, area and storey — do "
    "not invent facts. If a space is genuinely undeterminable, return 'unknown' with low confidence. "
    "Return one classification per space, using the given index."
)

class SpaceClassification(BaseModel):
    index: int = Field(description="index of the space in the provided list")
    use_type: str = Field(description="one use-type from the allowed vocabulary")
    confidence: float = Field(description="confidence between 0 and 1", ge=0, le=1)

class SpaceClassificationList(BaseModel):
    classifications: List[SpaceClassification]


def llm_prompt(items):
    lines = [llm_instructions, "", "Spaces to classify:"]
    for index, (guid, sample) in enumerate(items):
        area = sample.get("area")
        storey = (sample.get("storey") or {}).get("name")
        lines.append(
            f"[{index}] name={sample.get('name')!r} long_name={sample.get('long_name')!r} "
            f"area_m2={round(area, 1) if area else None} storey={storey!r}"
        )
    return "\n".join(lines)


def classify_llm(unresolved, llm=None):
    if not unresolved:
        return {}

    distinct = {}
    for space in unresolved:
        key = normalize(space.get("long_name"), space.get("name"))
        distinct.setdefault(key, space)
    items = list(distinct.items())

    if llm is None:
        from core_backend.llm import select_llm
        llm, model_label = select_llm()

    try:
        structured = llm.with_structured_output(SpaceClassificationList)
        result = structured.invoke(llm_prompt(items))
    except Exception as exc:  
        print(f"LLM classification unavailable ({exc}); leaving spaces as 'unknown'.")
        return {}

    allowed = set(use_types)
    out = {}
    for c in result.classifications:
        if 0 <= c.index < len(items):
            key = items[c.index][0]
            use_type = c.use_type if c.use_type in allowed else "unknown"
            out[key] = {"use_type": use_type, "confidence": float(c.confidence)}
    return out


def classify_spaces(spaces, use_llm=True, llm=None):
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

    use_llm = "llm" in sys.argv
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
              f"{len(distinct)} distinct name(s). Run with llm to classify them.")
        for long_name, name in distinct[:25]:
            print(f"  long_name={long_name!r:20} name~={name!r}")
