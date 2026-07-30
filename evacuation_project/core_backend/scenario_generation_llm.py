#This is the core part of the project where it creates the whole building evacuation scenarios having the
#routes, bottlenecks, risks, assumptions and a narrative. Occupancy and travel distance are COMPUTED
#deterministically (occupancy.py + travel_distance.py, joined by egress.ground_spaces) and handed to the
#model as facts; the AI only decides WHICH scenarios are worth generating and writes them up, in a SINGLE
#structured API call. Regulation pass/fail is a separate blocking gate (see uk_regulation_checking).

import os
import sys
from collections import Counter
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field

from core_backend.llm import select_llm
from core_backend.egress import build_graph, ground_spaces, discount_exit
from core_backend.occupancy import JURISDICTION_SOURCE
from core_backend.ifc_parser import parser_summary
from core_backend.space_classifier import classify_spaces
from core_backend.uk_regulation_checking import regulation_gate, load_regs
from core_backend.sample_paths import resolve_ifc

DISTANCE_METHOD = ("geodesic shortest path over a per-storey walkable raster (0.1 m cells) from each "
                   "room's most remote point, plus stair-going descent for upper storeys — a "
                   "geometry-based measurement (approx to cell size), non-verdict")

# How many "busiest exit unavailable" variants to precompute so the AI's degraded scenarios are backed
# by real recomputed distances. Each one is a full raster+Dijkstra pass per storey, hence the bound.
DISCOUNT_VARIANTS = int(os.getenv("EVAC_DISCOUNT_VARIANTS", "2"))


def _round(value, ndigits):
    return round(value, ndigits) if isinstance(value, (int, float)) else value


# ---------------------------------------------------------------------------------------------------
# What the single API call returns: the AI-chosen scenario set. The numbers are NOT the model's job.
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
    occupants_total: int = Field(description="occupants to evacuate under this scenario's state; must "
                                             "not exceed the computed total occupant load")


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


class BuildingAnalysis(BaseModel):
    scenarios: List[ScenarioContent] = Field(
        description="At least FOUR distinct scenarios. The first must always be the base case with all "
                    "exits available and normal occupancy. The rest must be selected autonomously from "
                    "this building's geometry, exit arrangement, storeys, occupant distribution and "
                    "computed travel distances — not chosen at random.")


_SYSTEM = (
    "You are a fire-safety engineer preparing whole-building evacuation SCENARIOS (the input description "
    "for egress analysis) — not a simulation and not a compliance verdict.\n\n"
    "IMPORTANT — the numbers are already done. Occupant loads and travel distances below were COMPUTED "
    "from the building geometry: occupant loads from the published code floor-space factors, travel "
    "distances by measuring the walked path over the real floor plan. Use them exactly as supplied. "
    "Never recompute, re-estimate, scale or round them differently, and never invent an occupant count, "
    "a distance, a room or an exit. Every number in your prose must appear verbatim in the facts below. "
    "Refer to exits only by the ids given.\n\n"
    "YOUR TASK IS THE SCENARIO SET.\n"
    "Create AT LEAST FOUR distinct evacuation scenarios, generated autonomously from the building "
    "geometry, storey layout, space types, occupant distribution, exit locations, computed travel "
    "distances and the circulation network. Do NOT choose scenarios randomly — analyse the building and "
    "create scenarios that are meaningful for this specific IFC model.\n\n"
    "The first scenario MUST always be the Base Case: all final exits available, normal occupancy.\n\n"
    "Choose the remaining scenarios by identifying the most realistic or most challenging evacuation "
    "conditions for THIS building. Examples (not mandatory, not exhaustive): loss of the busiest exit; "
    "loss of the exit serving the largest population; loss of an upper-floor escape route; night "
    "occupancy; daytime peak occupancy; maintenance closure of one exit; reduced exit capacity; high "
    "occupancy concentrated on one storey; congestion at a stair; or any geometry-specific evacuation "
    "challenge you can see in the facts. Pick whichever best stress the evacuation routes, and make each "
    "scenario substantially different from the others.\n\n"
    "When a scenario discounts an exit, prefer one of the exits whose DEGRADED-CASE FACTS are supplied "
    "below, and use those recomputed numbers rather than the base-case ones.\n\n"
    "For every scenario determine: occupancy_state, occupants_total, exits_available, exits_discounted, "
    "occupant_distribution, routes (from_area -> via -> to_exit), bottlenecks, risks, assumptions and a "
    "short plain-English narrative. occupants_total must be consistent with your occupant_distribution "
    "and must not exceed the computed total occupant load; if you reduce it (e.g. a night state), say so "
    "in that scenario's assumptions.\n\n"
    "Also give, per scenario: regulatory_justification — the regulation clause(s) the scenario tests, "
    "cited ONLY from the REGULATION REFERENCES provided (use their ids and doc references; do not invent "
    "clause numbers); and ai_explanation — one or two sentences on why you chose this scenario and what "
    "it demonstrates for egress.\n\n"
    "If some spaces could not be assessed, treat them as an open risk, never as safe."
)

_TASK = "Produce the BuildingAnalysis: your chosen scenarios, written from the computed facts."


# ---------------------------------------------------------------------------------------------------
# Grounding: the computed egress results are the facts the model reasons over.
# ---------------------------------------------------------------------------------------------------
def _resolve_exits(summary, classified, grounded):
    """The computed ground-level final exits; falls back to all emergency exits if none sit at grade."""
    exits = list(grounded["final_exits"])
    if exits:
        return exits
    _, _, final_exits = build_graph(summary, classified)
    if final_exits:
        return list(final_exits.values())
    return [{"id": d["id"], "name": d.get("name"), "width_m": d.get("width_m"),
             "position": d.get("position")} for d in summary.get("emergency_exits", [])]


def _reg_refs(jurisdiction):
    """Compact list of the jurisdiction's rules, to ground each scenario's regulatory_justification."""
    try:
        regs = load_regs(jurisdiction)
    except Exception:
        return []
    return [{"id": r.get("unique_id"), "name": r.get("regulation_name"),
             "reference": r.get("doc_reference")} for r in regs.values()]


def _storey_rollup(grounded):
    """Per-storey occupants / space count / longest computed travel distance / unreachable count."""
    rollup = {}
    for s in grounded["spaces"]:
        name = (s["storey"] or {}).get("name", "Unknown")
        row = rollup.setdefault(name, {"occupants": 0, "spaces": 0, "max_dist": 0.0, "unreachable": 0})
        row["spaces"] += 1
        if s["occupant_load"]:
            row["occupants"] += s["occupant_load"]
        if s["reachable"] and s["travel_distance_m"]:
            row["max_dist"] = max(row["max_dist"], s["travel_distance_m"])
        if not s["reachable"]:
            row["unreachable"] += 1
    return rollup


def _degraded_cases(summary, classified, grounded, jurisdiction, limit=DISCOUNT_VARIANTS):
    """Recompute egress with each of the busiest exits unavailable, so the AI's degraded scenarios have
    real numbers to cite.

    The result is carried in the output object as well as fed to the model, so every degraded figure
    the narrative quotes traces back to the record (validation.number_factcheck relies on this).
    """
    if limit <= 0:
        return []
    usage = Counter(s["nearest_exit"] for s in grounded["spaces"] if s["nearest_exit"])
    cases = []
    for exit_id, _count in usage.most_common(limit):
        variant = discount_exit(summary, classified, exit_id, jurisdiction=jurisdiction)
        cases.append({
            "exit_discounted": exit_id,
            "method": "egress re-measured with this exit removed (same computation as the base case)",
            "per_storey": [
                {"storey": storey, "occupants": row["occupants"],
                 "max_travel_distance_m": round(row["max_dist"], 1),
                 "unreachable": row["unreachable"]}
                for storey, row in _storey_rollup(variant).items()
            ],
        })
    return cases


def _facts_block(building, grounded, exits, stairs, storeys, reg_refs, degraded):
    spaces = grounded["spaces"]
    lines = [
        f"Building: {building['project']} | storeys: {building['storeys']} | "
        f"total floor area: {building['total_floor_area_m2']} m2 | real spaces: {len(spaces)} | "
        f"TOTAL OCCUPANT LOAD (computed): {building['total_occupant_load']}",
        "",
        "Storeys (name -> elevation / height above ground, metres):",
    ]
    for s in storeys:
        lines.append(f"  - {s.get('name')}: elevation={_round(s.get('elevation_m'), 2)}, "
                     f"height_above_ground={_round(s.get('height_above_ground_m'), 2)}")

    lines += ["", "Final (ground-level) exits — occupants leave by these ids:"]
    for e in exits:
        lines.append(f"  - {e['id']} (name={e.get('name')}, width_m={_round(e.get('width_m'), 2)})")

    if stairs:
        lines += ["", "Internal stairs (connect storeys):"]
        for st in stairs:
            lines.append(f"  - {st['id']} (name={st.get('name')}, width_m={_round(st.get('width'), 2)})")

    lines += ["", "BASE-CASE per-storey rollup (computed — occupants / spaces / longest travel "
                  "distance m / unreachable):"]
    for storey, row in _storey_rollup(grounded).items():
        lines.append(f"  - {storey}: occupants={row['occupants']} spaces={row['spaces']} "
                     f"max_travel_m={round(row['max_dist'], 1)} unreachable={row['unreachable']}")

    reachable = [s for s in spaces if s["reachable"] and s["travel_distance_m"]]
    longest = sorted(reachable, key=lambda s: -s["travel_distance_m"])[:8]
    if longest:
        lines += ["", "Longest computed travel distances (space -> nearest exit):"]
        for s in longest:
            storey = (s["storey"] or {}).get("name")
            lines.append(f"  - {s['use_type']} on {storey}: {s['travel_distance_m']} m "
                         f"to exit {s['nearest_exit']}")

    lines += ["", "Spaces (guid | use_type | storey | area m2 | OCCUPANTS | travel distance m | "
                  "nearest exit | name):"]
    for s in spaces:
        storey = (s["storey"] or {}).get("name")
        lines.append(f"  - {s['guid']} | {s['use_type']} | storey={storey} | "
                     f"area={_round(s['area_m2'], 1)} | occupants={s['occupant_load']} | "
                     f"travel_m={s['travel_distance_m']} | exit={s['nearest_exit']} | "
                     f"name={s['name']!r}")

    if degraded:
        lines += ["", "DEGRADED-CASE FACTS (computed — egress re-measured with one exit unavailable):"]
        for case in degraded:
            lines.append(f"  If exit {case['exit_discounted']} is UNAVAILABLE:")
            for row in case["per_storey"]:
                lines.append(f"    - {row['storey']}: occupants={row['occupants']} "
                             f"max_travel_m={row['max_travel_distance_m']} "
                             f"unreachable={row['unreachable']}")

    if grounded["not_assessed"]:
        lines += ["", f"{len(grounded['not_assessed'])} space(s) could not be fully assessed "
                      f"(missing data / no path) — do not assume they are safe."]

    if reg_refs:
        lines += ["", "Regulation references (cite each scenario's regulatory_justification ONLY from "
                      "these):"]
        for r in reg_refs:
            lines.append(f"  - {r['id']}: {r['name']} (ref: {r['reference']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# Deterministic assembly of the whole-building object around the AI-chosen scenarios.
# ---------------------------------------------------------------------------------------------------
def _building_block(summary, grounded, jurisdiction):
    total_area = round(sum(s["area_m2"] for s in grounded["spaces"] if s["area_m2"]), 1)
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


def _exits_block(exits):
    return [{"id": e["id"], "name": e.get("name"), "type": "final_exit", "width_m": _round(e.get("width_m"), 2)}
            for e in exits]


def _circulation_block(stairs):
    return [{"id": st["id"], "name": st.get("name"), "type": "internal_stair",
             "width_m": _round(st.get("width"), 2)} for st in stairs]


def _assemble_scenario(sc):
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


def generate_scenario_object(summary, classified, jurisdiction, gate, llm=None, model_label=None):
    """Assemble the whole-building scenario object: computed egress + ONE grounded LLM call for the
    scenario set + the regulation gate."""
    if llm is None:
        # the generation call is the big one — give it a long read timeout (override EVAC_GEN_TIMEOUT)
        llm, model_label = select_llm(max_tokens=16384,
                                      timeout=float(os.getenv("EVAC_GEN_TIMEOUT", "600")))

    # ---- computed, deterministic: occupancy + travel distance + reachability --------------------
    grounded = ground_spaces(summary, classified, jurisdiction=jurisdiction)
    exits = _resolve_exits(summary, classified, grounded)
    stairs = summary.get("stairs", [])
    storeys = summary.get("storeys", [])

    building = _building_block(summary, grounded, jurisdiction)
    degraded = _degraded_cases(summary, classified, grounded, jurisdiction)

    # ---- the single API call: which scenarios are worth generating, and their write-up ----------
    reg_refs = _reg_refs(jurisdiction)
    facts = _facts_block(building, grounded, exits, stairs, storeys, reg_refs, degraded)
    prompt = f"{_SYSTEM}\n\n=== COMPUTED BUILDING FACTS (reason only over these) ===\n{facts}\n\n=== TASK ===\n{_TASK}"

    analysis = llm.with_structured_output(BuildingAnalysis).invoke(prompt)

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
        "exits": _exits_block(exits),
        "circulation": _circulation_block(stairs),
        "spaces": _spaces_block(grounded, classified),
        "degraded_cases": degraded,
        "scenarios": [_assemble_scenario(sc) for sc in analysis.scenarios],
        "regulation_check": gate,
        "validation": {},
        "not_assessed": grounded["not_assessed"],
    }


def build_full_scenario(ifc_path, jurisdiction="england", use_llm=True, gate=None):
    """Parse, gate, classify, compute egress, then produce the scenario set in one generation call.

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
    print(f"Occupancy/distance: computed — {obj['provenance']['distance_method'][:60]}...")
    print(f"Generated scenario object with {len(obj['scenarios'])} scenarios, "
          f"{len(obj['spaces'])} spaces, {len(obj['exits'])} exits, "
          f"{len(obj['not_assessed'])} not_assessed.")
    print(f"Total occupant load (computed): {obj['building']['total_occupant_load']}")
    for scn in obj["scenarios"]:
        print(f"\n{scn['id']} ({scn['type']}) — {scn['title']}")
        print(f"conditions: {scn['conditions']}")
        print(f"routes: {len(scn['routes'])} | bottlenecks: {len(scn['bottlenecks'])} "
              f"| risks: {len(scn['risks'])}")
        print(f"narrative: {scn['narrative'][:280]}...")
