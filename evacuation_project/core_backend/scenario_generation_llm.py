#This is the core part of the project where it creates the whole building evacuation scenarios having the routes, bottlenecks, risks, assumptions and a narrative.

import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field

from core_backend.llm import select_llm
from core_backend.egress import ground_spaces, discount_exit
from core_backend.occupancy import JURISDICTION_SOURCE
from core_backend.ifc_parser import parser_summary
from core_backend.space_classifier import classify_spaces
from core_backend.uk_regulation_checking import annotate_and_summarize
import sys
import json
from core_backend.sample_paths import resolve_ifc

DISTANCE_METHOD = ("geodesic shortest path over a per-storey walkable raster (0.1 m cells) from each "
                   "room's most remote point, plus stair-going descent for upper storeys — a "
                   "geometry-based measurement (approx to cell size), non-verdict")


def _round(value, ndigits):
    return round(value, ndigits) if isinstance(value, (int, float)) else value

# What the LLM produces per variant 
class Route(BaseModel):
    from_area: str = Field(description="where occupants start (e.g. a storey or space name)")
    via: str = Field(default="", description="circulation/stair route taken")
    to_exit: str = Field(description="the exit id or name they leave by")
    note: str = Field(default="", description="short note, e.g. approx distance or a caveat")

class ScenarioContent(BaseModel):
    title: str
    assumptions: List[str]
    occupant_distribution: List[str] = Field(description="occupants per storey/area, e.g. 'Floor_02: 8'")
    routes: List[Route]
    bottlenecks: List[str]
    risks: List[str]
    narrative: str


# deterministic facts fed to the model 
def _storey_rollup(grounded):
    rollup = defaultdict(lambda: {"occupants": 0, "spaces": 0, "max_dist": 0.0, "unreachable": 0})
    for s in grounded["spaces"]:
        name = (s["storey"] or {}).get("name", "Unknown")
        row = rollup[name]
        row["spaces"] += 1
        if s["occupant_load"]:
            row["occupants"] += s["occupant_load"]
        if s["reachable"] and s["travel_distance_m"]:
            row["max_dist"] = max(row["max_dist"], s["travel_distance_m"])
        if not s["reachable"]:
            row["unreachable"] += 1
    return rollup


def _facts_block(building, grounded, conditions, reg_summary):
    lines = [
        f"Building: {building['project']} | storeys: {building['storeys']} | "
        f"total occupant load: {building['total_occupant_load']} | "
        f"total floor area: {building['total_floor_area_m2']} m2",
        f"Occupancy state: {conditions['occupancy_state']} | occupants to evacuate: {conditions['occupants_total']}",
        "",
        "Final (ground-level) exits available:",
    ]
    for e in conditions["exits_available"]:
        lines.append(f"  - {e['id']} (name={e['name']}, width_m={e['width_m']})")
    if conditions["exits_discounted"]:
        lines.append(f"Exit DISCOUNTED for this scenario: {', '.join(conditions['exits_discounted'])}")
    lines.append("")
    lines.append("Per-storey rollup (occupants / spaces / max approx travel distance m / unreachable):")
    for storey, row in _storey_rollup(grounded).items():
        lines.append(f"  - {storey}: occupants={row['occupants']} spaces={row['spaces']} "
                     f"max_travel_m={round(row['max_dist'], 1)} unreachable={row['unreachable']}")
    reachable = [s for s in grounded["spaces"] if s["reachable"] and s["travel_distance_m"]]
    longest = sorted(reachable, key=lambda s: -s["travel_distance_m"])[:6]
    lines.append("")
    lines.append("Longest approx travel distances (space -> nearest exit):")
    for s in longest:
        storey = (s["storey"] or {}).get("name")
        lines.append(f"  - {s['use_type']} on {storey}: {s['travel_distance_m']} m "
                     f"to exit {s['nearest_exit']}")
    if grounded["not_assessed"]:
        lines.append("")
        lines.append(f"{len(grounded['not_assessed'])} space(s) could not be fully assessed "
                     f"(missing data / no path) — do not assume they are safe.")
    lines.append("")
    lines.append(f"Regulation reference (measured vs limit, non-verdict): {reg_summary}")
    return "\n".join(lines)

_SYSTEM = (
    "You are a fire-safety engineer preparing an evacuation SCENARIO (the input description for egress "
    "analysis), not a simulation and not a compliance verdict. You are given computed building facts. "
    "Reason ONLY over those numbers — never invent areas, counts, distances, or exits. Refer to exits by "
    "the ids given. Every number you write MUST appear in the supplied facts. Describe how occupants "
    "leave under the stated conditions: their distribution, the routes (from area -> via -> exit), the "
    "bottlenecks and risks, your assumptions, and a short plain-English narrative. If some spaces were "
    "not assessed, treat them as an open risk, not as safe."
)

def _generate_variant(llm, building, grounded, conditions, reg_summary):
    facts = _facts_block(building, grounded, conditions, reg_summary)
    prompt = f"{_SYSTEM}\n\n=== COMPUTED FACTS ===\n{facts}\n\n=== TASK ===\nProduce the scenario."
    structured = llm.with_structured_output(ScenarioContent)
    content = structured.invoke(prompt)
    return content


# deterministic assembly of the whole-building object 
def _building_block(summary, grounded, jurisdiction):
    occupiable = [s for s in grounded["spaces"] if s["area_m2"]]
    total_area = round(sum(s["area_m2"] for s in occupiable), 1)
    total_occ = sum(s["occupant_load"] for s in grounded["spaces"] if s["occupant_load"])
    return {
        "project": summary["project"],
        "source_ifc": summary.get("source_ifc"),
        "jurisdiction": jurisdiction,
        "occupancy_type": "residential (dwellings)",
        "storeys": len(summary.get("storeys", [])),
        "total_floor_area_m2": total_area,
        "total_occupant_load": total_occ,
    }

def _spaces_block(grounded, classified):
    conf = {c["guid"]: c for c in classified}
    out = []
    for s in grounded["spaces"]:
        c = conf.get(s["guid"], {})
        out.append({
            "guid": s["guid"],
            "name": s["name"],
            "use_type": s["use_type"],
            "use_type_confidence": c.get("use_type_confidence"),
            "use_type_source": c.get("use_type_source"),
            "storey": (s["storey"] or {}).get("name"),
            "area_m2": _round(s["area_m2"], 2),
            "occupant_load": s["occupant_load"],
            "occupant_basis": s["occupant_basis"],
            "nearest_exit": s["nearest_exit"],
            "travel_distance_m": _round(s["travel_distance_m"], 1),
            "travel_distance_method": s.get("travel_distance_method"),
            "most_remote_point": s.get("most_remote_point"),
            "reachable": s["reachable"],
        })
    return out

def _circulation_block(summary, classified):
    stair_ids = {c["guid"] for c in classified if c["use_type"] == "stair"}
    out = []
    for stair in summary.get("stairs", []):
        out.append({"id": stair["id"], "name": stair.get("name"), "type": "internal_stair",
                    "width_m": _round(stair.get("width"), 2)})
    return out

def _exits_block(final_exits):
    return [{"id": e["id"], "name": e["name"], "type": "final_exit", "width_m": _round(e["width_m"], 2)}
            for e in final_exits]

def _conditions(building, final_exits, discounted_ids, occupancy_state):
    available = [e for e in final_exits if e["id"] not in discounted_ids]
    return {
        "exits_available": available,
        "exits_discounted": list(discounted_ids),
        "occupancy_state": occupancy_state,
        "occupants_total": building["total_occupant_load"],
    }

def generate_scenario_object(summary, classified, jurisdiction, reg_block, llm=None, model_label=None):
    if llm is None:
        llm, model_label = select_llm(max_tokens=4096)

    base_grounded = ground_spaces(summary, classified, jurisdiction=jurisdiction)
    building = _building_block(summary, base_grounded, jurisdiction)

    by_reg = reg_block.get("by_regulation", [])
    reg_na = reg_block.get("not_assessed", [])
    total_checked = sum(r["checked"] for r in by_reg)
    total_within = sum(r["within_limit"] for r in by_reg)
    total_review = sum(r["requires_manual_review"] for r in by_reg)
    reg_summary = (f"{total_checked} elements checked across {len(by_reg)} rules; "
                   f"{total_within} within limit, {total_review} require manual review; "
                   f"{len(reg_na)} not_assessed")

    scenarios = []

    #SCN-BASE: all exits available 
    base_conditions = _conditions(building, base_grounded["final_exits"], set(), "night")
    base_content = _generate_variant(llm, building, base_grounded, base_conditions, reg_summary)
    scenarios.append(_assemble_scenario("SCN-BASE", "base_case", base_conditions, base_content))

    # SCN-EXIT-BLOCKED: discount the busiest final exit 
    usage = Counter(s["nearest_exit"] for s in base_grounded["spaces"] if s["nearest_exit"])
    if usage:
        blocked_id = usage.most_common(1)[0][0]
        blocked_grounded = discount_exit(summary, classified, blocked_id, jurisdiction=jurisdiction)
        blocked_conditions = _conditions(building, base_grounded["final_exits"],
                                         {blocked_id}, "night")
        blocked_content = _generate_variant(llm, building, blocked_grounded,
                                            blocked_conditions, reg_summary)
        scenarios.append(_assemble_scenario("SCN-EXIT-BLOCKED", "one_exit_discounted",
                                            blocked_conditions, blocked_content))

    return {
        "schema_version": "1.0",
        "provenance": {
            "generated_by_model": model_label,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "occupancy_factor_source": (f"code occupancy load factors — "
                                        f"{JURISDICTION_SOURCE.get(jurisdiction, JURISDICTION_SOURCE['england'])}"
                                        f"; dwellings: NDSS bedspaces (habitable rooms + 1)"),
            "distance_method": DISTANCE_METHOD,
            "llm_grounded": True,
            "llm_temperature": os.getenv("ANTHROPIC_TEMPERATURE", "0"),
        },
        "building": building,
        "exits": _exits_block(base_grounded["final_exits"]),
        "circulation": _circulation_block(summary, classified),
        "spaces": _spaces_block(base_grounded, classified),
        "scenarios": scenarios,
        "regulation_check": {
            "jurisdiction": reg_block.get("jurisdiction", jurisdiction),
            "basis": reg_block.get("basis", "measured value vs applicable limit; non-verdict reference only"),
            "by_regulation": by_reg,
            "requires_manual_review": reg_block.get("requires_manual_review", []),
        },
        "validation": {},   
        "not_assessed": base_grounded["not_assessed"] + reg_na,
    }

def _assemble_scenario(scenario_id, scenario_type, conditions, content):
    return {
        "id": scenario_id,
        "type": scenario_type,
        "title": content.title,
        "conditions": {
            "exits_available": [e["id"] for e in conditions["exits_available"]],
            "exits_discounted": conditions["exits_discounted"],
            "occupancy_state": conditions["occupancy_state"],
            "occupants_total": conditions["occupants_total"],
        },
        "assumptions": content.assumptions,
        "occupant_distribution": content.occupant_distribution,
        "routes": [r.model_dump() for r in content.routes],
        "bottlenecks": content.bottlenecks,
        "risks": content.risks,
        "narrative": content.narrative,
    }


def build_full_scenario(ifc_path, jurisdiction="england", use_llm=True):
    summary = parser_summary(ifc_path)
    summary["source_ifc"] = os.path.basename(ifc_path)
    classified = classify_spaces(summary["spaces"], use_llm=use_llm)
    reg_block = annotate_and_summarize(summary, jurisdiction=jurisdiction)
    return generate_scenario_object(summary, classified, jurisdiction, reg_block)


if __name__ == "__main__":

    args = [a for a in sys.argv if not a.startswith("--")]
    obj = build_full_scenario(resolve_ifc(args), jurisdiction="england")
    print(f"Generated scenario object with {len(obj['scenarios'])} scenarios, "
          f"{len(obj['spaces'])} spaces, {len(obj['exits'])} exits, "
          f"{len(obj['not_assessed'])} not_assessed.")
    for scn in obj["scenarios"]:
        print(f"\n{scn['id']} ({scn['type']}) — {scn['title']}")
        print(f"conditions: {scn['conditions']}")
        print(f"routes: {len(scn['routes'])} | bottlenecks: {len(scn['bottlenecks'])} "
              f"|risks: {len(scn['risks'])}")
        print(f"narrative: {scn['narrative'][:280]}...")
