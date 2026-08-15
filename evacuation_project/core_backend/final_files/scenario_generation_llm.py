#This is the core part of the project where it creates the whole building evacuation scenarios having the
#routes, bottlenecks, risks, assumptions and a narrative. Occupancy, travel distance AND the scenario set
#(what conditions make sense) are all produced by the AI in a SINGLE structured API call, grounded in the
#extracted building geometry. Regulation pass/fail is a separate blocking gate (see uk_regulation_checking).

import os
import sys
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from core_backend.llm import select_llm
from core_backend.egress import build_graph
from core_backend.ifc_parser import parser_summary
from core_backend.space_classifier import classify_spaces
from core_backend.uk_regulation_checking import regulation_gate, load_regs
from core_backend.sample_paths import resolve_ifc

# Jurisdiction -> the document whose occupancy load factors guide the AI's occupancy estimates.
jurisdiction_source = {
    "england":          "Approved Document B, Table C1 (floor space factors)",
    "wales":            "Approved Document B, Table C1 (floor space factors)",
    "scotland":         "Building Standards Technical Handbook (Non-domestic), Table 2.10 (occupancy load factors)",
    "northern_ireland": "Technical Booklet E (occupancy load factors)",
}

DISTANCE_METHOD = ("LLM-estimated approximate travel distance from each space's centroid to its nearest "
                   "final exit (straight-line with a circulation allowance, plus stair descent for upper "
                   "storeys) — reasoned by the model over the supplied geometry, approximate and non-verdict")


def round_number(value, ndigits):
    return round(value, ndigits) if isinstance(value, (int, float)) else value


# ---------------------------------------------------------------------------------------------------
# What the single API call returns: per-space occupancy + distance AND the AI-chosen scenario set.
# ---------------------------------------------------------------------------------------------------
class Route(BaseModel):
    from_area: str = Field(description="where occupants start (a storey or space name)")
    via: str = Field(default="", description="circulation/stair route taken")
    to_exit: str = Field(description="the exit id or name they leave by")
    note: str = Field(default="", description="short note, e.g. approx distance or a caveat")


class ScenarioConditions(BaseModel):
    exits_available: List[str] = Field(description="exit ids that stay open in this scenario")
    exits_discounted: List[str] = Field(default_factory=list,
                                        description="exit ids assumed blocked/unavailable")
    occupancy_state: str = Field(description="e.g. 'night', 'day', 'peak occupancy'")
    occupants_total: int = Field(description="occupants to evacuate under this scenario's state")


class ScenarioContent(BaseModel):
    id: str = Field(description="short id you assign, e.g. 'SCN-BASE', 'SCN-EXIT-BLOCKED'")
    type: str = Field(description="e.g. 'base_case', 'one_exit_discounted'")
    title: str
    conditions: ScenarioConditions
    assumptions: List[str]
    occupant_distribution: List[str] = Field(description="occupants per storey/area, e.g. 'Floor_02: 8'")
    routes: List[Route]
    bottlenecks: List[str]
    risks: List[str]
    narrative: str
    regulatory_justification: str = Field(
        description="the regulation clause(s) this scenario tests, cited ONLY from the given references")
    ai_explanation: str = Field(
        description="short reasoning: why you chose this scenario and what it shows")


class SpaceAnalysis(BaseModel):
    guid: str = Field(description="the space guid, exactly as given")
    occupant_load: Optional[int] = Field(description="estimated occupants (0 for non-occupiable spaces)")
    occupant_basis: str = Field(default="", description="how it was estimated, <=12 words")
    nearest_exit: str = Field(default="", description="the nearest exit id from the given list")
    travel_distance_m: Optional[float] = Field(description="estimated distance to that exit, metres")
    travel_distance_basis: str = Field(default="", description="short note on the estimate")
    reachable: bool = Field(description="false only if the space plainly has no way out")


class BuildingAnalysis(BaseModel):
    spaces: List[SpaceAnalysis]
    scenarios: List[ScenarioContent] = Field(description="at least two: a base case + one or more degraded")


SYSTEM_PROMPT = (
    "You are a fire-safety engineer preparing whole-building evacuation SCENARIOS (the input description "
    "for egress analysis) — not a simulation and not a compliance verdict. You are given the building's "
    "extracted geometry (spaces with area/centroid/storey, final exits with positions, stairs, storeys). "
    "Reason ONLY over these facts; never invent rooms, exits, areas or coordinates. Refer to exits only by "
    "the ids given.\n\n"
    "Do THREE things:\n"
    "1) OCCUPANCY — estimate an occupant_load for every space from its area and use_type, using typical "
    "code floor-space factors as a guide: office/commercial ~6 m2/person; assembly/dining/lounge/common "
    "room ~1 m2/person; bedroom ~8 m2/person; storage and parking ~30 m2/person; a whole 'dwelling' ~ "
    "(habitable rooms + 1) persons; circulation, stair, sanitary and plant spaces carry 0. Give a short "
    "occupant_basis (<=12 words). On a storey modelled as whole apartments, count occupants at the "
    "dwelling level and set constituent rooms to 0 so residents are not double-counted.\n"
    "2) DISTANCE — estimate travel_distance_m from each space's centroid to its nearest listed exit "
    "(straight-line plus a realistic circulation allowance; add stair descent for upper storeys). Give the "
    "nearest_exit id and set reachable=false only if a space plainly has no way out.\n"
    "3) SCENARIOS — YOU decide the scenario set from the geometry: at least two, always including a base "
    "case with all exits available, plus one or more degraded cases (e.g. the busiest or a specific exit "
    "discounted, and/or a night vs day occupancy state). For each, set the conditions yourself — "
    "occupancy_state, occupants_total, which exit ids stay available and which are discounted — then give "
    "occupant distribution, routes (from_area -> via -> to_exit), bottlenecks, risks, assumptions and a "
    "short plain-English narrative. Every number you write in prose must be one you produced in the "
    "structured fields.\n"
    "For each scenario also give: regulatory_justification — the regulation clause(s) the scenario tests, "
    "cited ONLY from the REGULATION REFERENCES provided (use their ids and doc references; do not invent "
    "clause numbers); and ai_explanation — one or two sentences on why you chose this scenario and what it "
    "demonstrates for egress."
)

TASK = "Produce the BuildingAnalysis: per-space occupancy and distance, plus your chosen scenarios."


# ---------------------------------------------------------------------------------------------------
# Grounding: turn the parsed geometry into the facts the model reasons over.
# ---------------------------------------------------------------------------------------------------
def spaces_for_llm(summary, classified):
    """Real (non-overlay) spaces with the geometry the model needs, one dict each."""
    use_type = {c["guid"]: c["use_type"] for c in classified}
    out = []
    for sp in summary["spaces"]:
        ut = use_type.get(sp["id"], "unknown")
        if ut == "measurement_zone":          # BIM area/volume overlay, not a real room
            continue
        out.append({
            "guid": sp["id"],
            "name": sp.get("name"),
            "long_name": sp.get("long_name"),
            "use_type": ut,
            "area_m2": sp.get("area"),
            "storey": (sp.get("storey") or {}).get("name"),
            "centroid": sp.get("centroid"),
        })
    return out


def resolve_exits(summary, classified):
    """Ground-level final exits (geometry only). Falls back to all emergency exits if none sit at grade."""
    adjacency, positions, final_exits = build_graph(summary, classified)
    exits = list(final_exits.values())
    if exits:
        return exits
    return [{"id": d["id"], "name": d.get("name"), "width_m": d.get("width_m"),
             "position": d.get("position")} for d in summary.get("emergency_exits", [])]


def reg_refs(jurisdiction):
    """Compact list of the jurisdiction's rules, to ground each scenario's regulatory_justification."""
    try:
        regs = load_regs(jurisdiction)
    except Exception:
        return []
    return [{"id": r.get("unique_id"), "name": r.get("regulation_name"),
             "reference": r.get("doc_reference")} for r in regs.values()]


def facts_block(building, spaces, exits, stairs, storeys, reg_refs):
    lines = [
        f"Building: {building['project']} | storeys: {building['storeys']} | "
        f"total floor area: {building['total_floor_area_m2']} m2 | real spaces: {len(spaces)}",
        "",
        "Storeys (name -> elevation / height above ground, metres):",
    ]
    for s in storeys:
        lines.append(f"  - {s.get('name')}: elevation={round_number(s.get('elevation_m'), 2)}, "
                     f"height_above_ground={round_number(s.get('height_above_ground_m'), 2)}")
    lines.append("")
    lines.append("Final (ground-level) exits — occupants leave by these ids:")
    for e in exits:
        p = e.get("position")
        pxyz = f"{p[0]:.1f},{p[1]:.1f},{p[2]:.1f}" if p else "n/a"
        lines.append(f"  - {e['id']} (name={e.get('name')}, width_m={round_number(e.get('width_m'), 2)}, at {pxyz})")
    if stairs:
        lines.append("")
        lines.append("Internal stairs (connect storeys):")
        for st in stairs:
            lines.append(f"  - {st['id']} (name={st.get('name')}, width_m={round_number(st.get('width'), 2)})")
    lines.append("")
    lines.append("Spaces (guid | use_type | area m2 | storey | name | centroid x,y,z):")
    for sp in spaces:
        c = sp["centroid"]
        cxyz = f"{c[0]:.1f},{c[1]:.1f},{c[2]:.1f}" if c else "n/a"
        lines.append(f"  - {sp['guid']} | {sp['use_type']} | area={round_number(sp['area_m2'], 1)} | "
                     f"storey={sp['storey']} | name={sp['name']!r} long_name={sp['long_name']!r} | "
                     f"centroid={cxyz}")
    if reg_refs:
        lines.append("")
        lines.append("Regulation references (cite each scenario's regulatory_justification ONLY from these):")
        for r in reg_refs:
            lines.append(f"  - {r['id']}: {r['name']} (ref: {r['reference']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# Deterministic assembly of the whole-building object around the single LLM analysis.
# ---------------------------------------------------------------------------------------------------
def building_block(summary, spaces_for_llm, jurisdiction):
    total_area = round(sum(sp["area_m2"] for sp in spaces_for_llm if sp["area_m2"]), 1)
    return {
        "project": summary["project"],
        "source_ifc": summary.get("source_ifc"),
        "jurisdiction": jurisdiction,
        "occupancy_type": "residential (dwellings)",
        "storeys": len(summary.get("storeys", [])),
        "total_floor_area_m2": total_area,
        "total_occupant_load": 0,          # filled from the model's per-space loads
    }


def spaces_block(spaces_for_llm, classified, analysis_by_guid):
    conf = {c["guid"]: c for c in classified}
    out = []
    for sp in spaces_for_llm:
        a = analysis_by_guid.get(sp["guid"])
        c = conf.get(sp["guid"], {})
        out.append({
            "guid": sp["guid"],
            "name": sp["name"],
            "use_type": sp["use_type"],
            "use_type_confidence": c.get("use_type_confidence"),
            "use_type_source": c.get("use_type_source"),
            "storey": sp["storey"],
            "area_m2": round_number(sp["area_m2"], 2),
            "occupant_load": a.occupant_load if a else None,
            "occupant_basis": a.occupant_basis if a else None,
            "nearest_exit": (a.nearest_exit or None) if a else None,
            "travel_distance_m": round_number(a.travel_distance_m, 1) if (a and a.travel_distance_m is not None) else None,
            "travel_distance_basis": a.travel_distance_basis if a else None,
            "reachable": a.reachable if a else False,
        })
    return out


def exits_block(exits):
    return [{"id": e["id"], "name": e.get("name"), "type": "final_exit", "width_m": round_number(e.get("width_m"), 2)}
            for e in exits]


def circulation_block(stairs):
    return [{"id": st["id"], "name": st.get("name"), "type": "internal_stair",
             "width_m": round_number(st.get("width"), 2)} for st in stairs]


def assemble_scenario(sc):
    return {
        "id": sc.id,
        "type": sc.type,
        "title": sc.title,
        "conditions": {
            "exits_available": sc.conditions.exits_available,
            "exits_discounted": sc.conditions.exits_discounted,
            "occupancy_state": sc.conditions.occupancy_state,
            "occupants_total": sc.conditions.occupants_total,
        },
        "assumptions": sc.assumptions,
        "occupant_distribution": sc.occupant_distribution,
        "routes": [r.model_dump() for r in sc.routes],
        "bottlenecks": sc.bottlenecks,
        "risks": sc.risks,
        "narrative": sc.narrative,
        "regulatory_justification": sc.regulatory_justification,
        "ai_explanation": sc.ai_explanation,
    }


def not_assessed_rows(spaces_block):
    """Surface (never silently pass) spaces the model could not load or could not route out."""
    out = []
    for s in spaces_block:
        if s["occupant_load"] is None:
            out.append({"element": s["guid"], "name": s["name"],
                        "missing": "occupant load not estimated by the model",
                        "action": "flagged, not silently passed"})
        occupiable = s["occupant_load"] is None or (s["occupant_load"] or 0) > 0
        if occupiable and not s["reachable"]:
            out.append({"element": s["guid"], "name": s["name"],
                        "missing": "no egress path to a final exit was identified",
                        "action": "flagged, not silently passed"})
    return out


def generate_scenario_object(summary, classified, jurisdiction, gate, llm=None, model_label=None):
    """Assemble the whole-building scenario object from ONE grounded LLM call + the regulation gate."""
    if llm is None:
        # the generation call is the big one — give it a long read timeout (override EVAC_GEN_TIMEOUT)
        llm, model_label = select_llm(max_tokens=16384,
                                      timeout=float(os.getenv("EVAC_GEN_TIMEOUT", "600")))

    spaces_for_llm = spaces_for_llm(summary, classified)
    exits = resolve_exits(summary, classified)
    stairs = summary.get("stairs", [])
    storeys = summary.get("storeys", [])

    building = building_block(summary, spaces_for_llm, jurisdiction)

    reg_refs = reg_refs(jurisdiction)
    facts = facts_block(building, spaces_for_llm, exits, stairs, storeys, reg_refs)
    prompt = f"{SYSTEM_PROMPT}\n\n=== BUILDING FACTS (reason only over these) ===\n{facts}\n\n=== TASK ===\n{TASK}"

    # THE single API call: occupancy + distance + AI-chosen scenarios, all at once.
    analysis = llm.with_structured_output(BuildingAnalysis).invoke(prompt)

    analysis_by_guid = {a.guid: a for a in analysis.spaces}
    spaces_block = spaces_block(spaces_for_llm, classified, analysis_by_guid)
    building["total_occupant_load"] = sum(s["occupant_load"] for s in spaces_block if s["occupant_load"])

    scenarios = [assemble_scenario(sc) for sc in analysis.scenarios]

    return {
        "schema_version": "1.0",
        "provenance": {
            "generated_by_model": model_label,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "occupancy_factor_source": (f"LLM-estimated from geometry, guided by code occupancy load "
                                        f"factors — {jurisdiction_source.get(jurisdiction, jurisdiction_source['england'])}"),
            "distance_method": DISTANCE_METHOD,
            "llm_grounded": True,
            "llm_temperature": os.getenv("ANTHROPIC_TEMPERATURE", "0"),
        },
        "building": building,
        "exits": exits_block(exits),
        "circulation": circulation_block(stairs),
        "spaces": spaces_block,
        "scenarios": scenarios,
        "regulation_check": gate,
        "validation": {},
        "not_assessed": not_assessed_rows(spaces_block),
    }


def build_full_scenario(ifc_path, jurisdiction="england", use_llm=True, gate=None):
    """Parse, gate, classify, then produce the whole-building scenario object in one generation call.

    ``gate`` may be a precomputed regulation_gate() result (the frontend runs the gate first to decide
    whether to generate at all); if None it is computed here and embedded in the object.
    """
    summary = parser_summary(ifc_path)
    summary["source_ifc"] = os.path.basename(ifc_path)
    if gate is None:
        gate = regulation_gate(summary, jurisdiction=jurisdiction)
    classified = classify_spaces(summary["spaces"], use_llm=use_llm)
    return generate_scenario_object(summary, classified, jurisdiction, gate)


if __name__ == "__main__":

    args = [a for a in sys.argv if not a.startswith("--")]
    obj = build_full_scenario(resolve_ifc(args), jurisdiction="england")

    gate = obj["regulation_check"]
    print(f"Regulation gate: {'PASS' if gate['passed'] else 'FAIL'} "
          f"({len(gate['violations'])} violation(s), {len(gate['manual_review'])} manual-review)")
    print(f"Generated scenario object with {len(obj['scenarios'])} scenarios, "
          f"{len(obj['spaces'])} spaces, {len(obj['exits'])} exits, "
          f"{len(obj['not_assessed'])} not_assessed.")
    for scn in obj["scenarios"]:
        print(f"\n{scn['id']} ({scn['type']}) — {scn['title']}")
        print(f"conditions: {scn['conditions']}")
        print(f"routes: {len(scn['routes'])} | bottlenecks: {len(scn['bottlenecks'])} "
              f"| risks: {len(scn['risks'])}")
        print(f"narrative: {scn['narrative'][:280]}...")
