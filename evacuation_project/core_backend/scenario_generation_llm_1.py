#This is the main part of the project which focuses on suggesting evacuation scenarios using LLM and all the computed values of occupancy, 
# travel distance and egress are given to the LLM such that it will decide adn give the necessary scenarios

import os
import sys
from collections import Counter
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, ValidationError

from core_backend.llm import select_llm
from core_backend.egress import build_graph, ground_spaces, discount_exit, stair_adjacency
from core_backend.exit_names import exit_names, name_exit_ids, named, resolve_exit_ids
from core_backend.occupancy import jurisdiction_source
from core_backend.ifc_parser import parser_summary
from core_backend.occupant_placement import attach_occupancy
from core_backend.space_classifier import classify_spaces
from core_backend.uk_regulation_checking import regulation_gate, load_regs
from core_backend.sample_paths import resolve_ifc

DISTANCE_METHOD = ("geodesic shortest path over a per-storey walkable raster (0.1 m cells) from each "
                   "room's most remote point, plus stair-going descent for upper storeys — a "
                   "geometry-based measurement (approx to cell size), non-verdict")

DISCOUNT_VARIANTS = int(os.getenv("EVAC_DISCOUNT_VARIANTS", "2"))

def _round(value, ndigits):
    return round(value, ndigits) if isinstance(value, (int, float)) else value

class Route(BaseModel):
    from_area: str = Field(description="where occupants start (a storey or space name)")
    via: str = Field(default="", description="circulation/stair route taken")
    to_exit: str = Field(description="the exit name they leave by, e.g. 'Exit 2'")
    note: str = Field(default="", description="short note, e.g. approx distance or a caveat")

class ScenarioConditions(BaseModel):
    exits_available: List[str] = Field(description="exit names that stay open in this scenario, "
                                                   "e.g. ['Exit 1', 'Exit 2']")
    exits_discounted: List[str] = Field(default_factory=list,
                                        description="exit names assumed blocked/unavailable, "
                                                    "e.g. ['Exit 3']")
    occupancy_state: str = Field(description="e.g. 'night', 'day', 'peak occupancy'")
    occupants_total: int = Field(description="occupants to evacuate under this scenario's state; must "
                                             "not exceed the computed total occupant load")

DISTRIBUTIONS = "constant, uniform, normal, lognormal"
class PreMovement(BaseModel):
    distribution: str = Field(description=f"one of: {DISTRIBUTIONS}")
    mean_s: float = Field(description="mean pre-movement time in seconds")
    sd_s: float = Field(default=0.0, description="standard deviation in seconds (0 for constant)")
    basis: str = Field(description="why these values for THIS building and THIS scenario — the "
                                   "occupancy type, whether occupants may be asleep, and the alarm "
                                   "arrangement you are assuming")

class OccupantProfile(BaseModel):
    name: str = Field(description="e.g. 'adult', 'child', 'reduced mobility'")
    fraction: float = Field(description="share of the occupants in this group, 0..1; the fractions "
                                        "across all profiles must sum to 1.0", ge=0, le=1)
    speed_distribution: str = Field(description=f"one of: {DISTRIBUTIONS}")
    speed_ms_mean: float = Field(description="mean unimpeded walking speed, m/s")
    speed_ms_sd: float = Field(default=0.0, description="standard deviation, m/s (0 for constant)")
    shoulder_width_m: float = Field(description="shoulder width / body diameter in metres")
    basis: str = Field(description="why this group and these values suit this building")


class OccupancyMultiplier(BaseModel):
    use_type: str = Field(description="a use_type appearing in the space list, e.g. 'dwelling'")
    multiplier: float = Field(
        description="0.0..1.0 INCLUSIVE — a hard limit, never above 1.0. It can only empty or thin a "
                    "use-type, never overfill it (e.g. at night, commercial 0.0 and dwelling 1.0)",
        ge=0, le=1)
    reason: str

class SimulationSetup(BaseModel):
    movement_model: str = Field(description="'steering' (agent-based) or 'sfpe' (hydraulic)")
    end_time_s: float = Field(description="how long to let the simulation run, seconds")
    pre_movement: PreMovement
    profiles: List[OccupantProfile] = Field(description="population mix; fractions must sum to 1.0")
    occupancy_multipliers: List[OccupancyMultiplier] = Field(
        default_factory=list,
        description="how you scaled the computed occupant load to reach this scenario's "
                    "occupants_total; omit or leave empty for a full-occupancy scenario")

class ScenarioContent(BaseModel):
    type: str = Field(description="e.g. 'base_case', 'one_exit_discounted'")
    title: str
    conditions: ScenarioConditions
    assumptions: List[str]
    occupant_distribution: List[str] = Field(description="occupants per storey/area, e.g. 'Floor_02: 8'")
    routes: List[Route]
    bottlenecks: List[str]
    risks: List[str]
    narrative: str
    simulation: SimulationSetup
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
    "Refer to exits only by the NAMES given below — 'Exit 1', 'Exit 2' and so on — everywhere: in "
    "exits_available, exits_discounted, the routes and the prose. Do not invent an exit name, do not "
    "renumber them, and never quote an IFC GlobalId (the ids are resolved from the names for you).\n\n"
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
    "SIMULATION PARAMETERS — THESE ONES ARE YOURS TO CHOOSE.\n"
    "The occupant loads and travel distances above are computed and off-limits. The `simulation` block "
    "is different: it is the egress-simulation set-up, and you decide it per scenario. Give:\n"
    "  * movement_model — 'steering' (agent-based, shows queueing and route choice) or 'sfpe' "
    "(hydraulic flow); say which suits the scenario.\n"
    "  * end_time_s — long enough that everyone who can escape has escaped.\n"
    "  * pre_movement — the pre-travel activity time as a DISTRIBUTION, not a single number. Choose "
    "values that fit this building's occupancy and whether occupants may be asleep, and state the alarm "
    "arrangement you are assuming in `basis`. Typical engineering practice sits well under 30 minutes; "
    "a sleeping residential occupancy warrants a longer and more spread-out time than an alert one.\n"
    "  * profiles — the population mix (e.g. adults, children, reduced mobility), each with a walking "
    "speed distribution and shoulder width. Unimpeded walking speeds for able adults on the level are "
    "around 1.2 m/s and should stay inside 0.5-2.0 m/s; shoulder widths sit around 0.45-0.5 m. The "
    "`fraction` values MUST sum to exactly 1.0.\n"
    "  * occupancy_multipliers — if this scenario's occupants_total is lower than the computed total "
    "occupant load, give the per-use_type multipliers that produce it (e.g. a night state might set "
    "'commercial' to 0.0 and keep 'dwelling' at 1.0). These are applied per room downstream, so they "
    "are how your reduced total actually gets placed in the building. Leave empty for full occupancy. "
    "Every multiplier MUST lie between 0.0 and 1.0 inclusive — this is a hard limit, not a preference. "
    "The computed occupant loads are already the code-derived capacity of each room, so there is no "
    "such thing as a multiplier above 1.0: it would put more people in a room than the floor-space "
    "factor allows and the result would no longer be code-based. To make a scenario MORE demanding, "
    "discount an exit, lengthen pre-movement, or shift the population mix towards slower profiles — "
    "never inflate occupancy.\n"
    "Every one of these carries a `basis` / `reason` field: state your engineering reasoning there. "
    "These are the only numbers in the whole output you are permitted to originate — everything else "
    "must still come verbatim from the computed facts.\n\n"
    "Also give, per scenario: regulatory_justification — the regulation clause(s) the scenario tests, "
    "cited ONLY from the REGULATION REFERENCES provided (use their ids and doc references; do not invent "
    "clause numbers); and ai_explanation — one or two sentences on why you chose this scenario and what "
    "it demonstrates for egress.\n\n"
    "If some spaces could not be assessed, treat them as an open risk, never as safe."
)

TASK = "Produce the BuildingAnalysis: your chosen scenarios, written from the computed facts."

def resolve_exits(summary, classified, grounded):
    exits = list(grounded["final_exits"])
    if exits:
        return exits
    _, _, final_exits = build_graph(summary, classified)
    if final_exits:
        return list(final_exits.values())
    return [{"id": d["id"], "name": d.get("name"), "width_m": d.get("width_m"),
             "position": d.get("position")} for d in summary.get("emergency_exits", [])]

def reg_refs(jurisdiction):
    try:
        regs = load_regs(jurisdiction)
    except Exception:
        return []
    return [{"id": r.get("unique_id"), "name": r.get("regulation_name"),
             "reference": r.get("doc_reference")} for r in regs.values()]

def storey_rollup(grounded):
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


def degraded_cases(summary, classified, grounded, jurisdiction, names, limit=DISCOUNT_VARIANTS):
    if limit <= 0:
        return []
    usage = Counter(s["nearest_exit"] for s in grounded["spaces"] if s["nearest_exit"])
    cases = []
    for exit_id, _count in usage.most_common(limit):
        variant = discount_exit(summary, classified, exit_id, jurisdiction=jurisdiction)
        cases.append({
            "exit_discounted": exit_id,
            "exit_discounted_name": named(exit_id, names),
            "method": "egress re-measured with this exit removed (same computation as the base case)",
            "per_storey": [
                {"storey": storey, "occupants": row["occupants"],
                 "max_travel_distance_m": round(row["max_dist"], 1),
                 "unreachable": row["unreachable"]}
                for storey, row in storey_rollup(variant).items()
            ],
        })
    return cases


def facts_block(building, grounded, exits, stairs, storeys, reg_refs, degraded, names):
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

    lines += ["", "Final (ground-level) exits — occupants leave by these, refer to them by NAME:"]
    width_of = {e["id"]: e.get("width_m") for e in exits}
    for exit_id, name in names.items():          # names are already in plan order
        lines.append(f"  - {name} (width_m={_round(width_of.get(exit_id), 2)})")

    if stairs:
        lines += ["", "Internal stairs (connect storeys):"]
        for st in stairs:
            lines.append(f"  - {st['id']} (name={st.get('name')}, width_m={_round(st.get('width'), 2)})")

    lines += ["", "BASE-CASE per-storey rollup (computed — occupants / spaces / longest travel "
                  "distance m / unreachable):"]
    for storey, row in storey_rollup(grounded).items():
        lines.append(f"  - {storey}: occupants={row['occupants']} spaces={row['spaces']} "
                     f"max_travel_m={round(row['max_dist'], 1)} unreachable={row['unreachable']}")

    reachable = [s for s in spaces if s["reachable"] and s["travel_distance_m"]]
    longest = sorted(reachable, key=lambda s: -s["travel_distance_m"])[:8]
    if longest:
        lines += ["", "Longest computed travel distances (space -> nearest exit):"]
        for s in longest:
            storey = (s["storey"] or {}).get("name")
            lines.append(f"  - {s['use_type']} on {storey}: {s['travel_distance_m']} m "
                         f"to {named(s['nearest_exit'], names)}")

    lines += ["", "Spaces (guid | use_type | storey | area m2 | OCCUPANTS | travel distance m | "
                  "nearest exit | name):"]
    for s in spaces:
        storey = (s["storey"] or {}).get("name")
        lines.append(f"  - {s['guid']} | {s['use_type']} | storey={storey} | "
                     f"area={_round(s['area_m2'], 1)} | occupants={s['occupant_load']} | "
                     f"travel_m={s['travel_distance_m']} | "
                     f"exit={named(s['nearest_exit'], names)} | name={s['name']!r}")

    if degraded:
        lines += ["", "DEGRADED-CASE FACTS (computed — egress re-measured with one exit unavailable):"]
        for case in degraded:
            lines.append(f"  If {named(case['exit_discounted'], names)} is UNAVAILABLE:")
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


def building_block(summary, grounded, jurisdiction):
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


def spaces_block(grounded, classified, names):
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
            "centroid": [_round(v, 2) for v in s["centroid"]] if s["centroid"] else None,
            "occupant_load": s["occupant_load"],
            "occupant_basis": s["occupant_basis"],
            "nearest_exit": s["nearest_exit"],
            "nearest_exit_name": named(s["nearest_exit"], names),
            "travel_distance_m": _round(s["travel_distance_m"], 1),
            "travel_distance_method": s.get("travel_distance_method"),
            "most_remote_point": s.get("most_remote_point"),
            "reachable": s["reachable"],
            "reachability_note": s.get("reachability_note"),
        })
    return out


def _point(p):
    return [_round(v, 2) for v in p] if p else None


def exits_block(exits, names):
    order = {exit_id: n for n, exit_id in enumerate(names)}      # names are in plan order
    return [{"id": e["id"], "exit_name": named(e["id"], names), "name": e.get("name"),
             "type": "final_exit", "width_m": _round(e.get("width_m"), 2),
             "position": _point(e.get("position"))}
            for e in sorted(exits, key=lambda e: order.get(e["id"], len(order)))]

storey_match_tol_m = 1.0


def circulation_block(summary):
    flights = {f["id"]: f for f in summary.get("stair_flights", [])}
    storeys = summary.get("storeys", [])

    def storey_at(z):
        if z is None or not storeys:
            return None
        near = min(storeys, key=lambda s: abs(s["elevation_m"] - z))
        return near["name"] if abs(near["elevation_m"] - z) <= storey_match_tol_m else None

    out = []
    for st in summary.get("stairs", []):
        mine = [flights[fid] for fid in st.get("flight_ids", []) if fid in flights]
        rise = sum(f["rise_m"] for f in mine if f.get("rise_m")) or None
        going = sum(f["going_m"] for f in mine if f.get("going_m")) or None
        slope = sum(f["slope_m"] for f in mine if f.get("slope_m")) or None
        position = st.get("position")
        base_z = position[2] if position else None
        spans = [storey_at(base_z), storey_at(base_z + rise) if (base_z is not None and rise) else None]
        spans = list(dict.fromkeys(s for s in spans if s))      # de-dup, keep base-then-top order
        out.append({
            "id": st["id"], "name": st.get("name"), "type": "internal_stair",
            "width_m": _round(st.get("width"), 2), "width_source": st.get("width_source"),
            "position": _point(position),
            "rise_m": _round(rise, 2), "going_m": _round(going, 2), "slope_m": _round(slope, 2),
            "connects_storeys": spans,
        })
    return out


def stair_links_block(summary, classified):
    use_type = {c["guid"]: c["use_type"] for c in classified}
    storey_of = {s["id"]: (s["storey"] or {}).get("name") for s in summary["spaces"]}
    return [{"space_a": a, "space_b": b, "storey_a": storey_of.get(a), "storey_b": storey_of.get(b)}
            for a, b in stair_adjacency(summary["spaces"], use_type)]


def doors_block(summary, exits):
    final = {e["id"] for e in exits}
    links = summary.get("door_space_links", {})
    return [{"id": d["id"], "name": d.get("name"), "type": "internal_door",
             "width_m": _round(d.get("width_m"), 2), "position": _point(d.get("position")),
             "connects": links.get(d["id"], [])}
            for d in summary.get("doors", []) if d["id"] not in final]


def elevators_block(summary):
    return [{"id": t["id"], "name": t.get("name"), "type": "elevator",
             "is_evac_lift": t.get("is_evac_lift"), "position": _point(t.get("position"))}
            for t in summary.get("elevators", [])]


def model_block(summary):
    return {
        "source_ifc": summary.get("source_ifc"),
        "units": "m",
        "coordinate_system": "ifc_world_coordinates",
        "geometry_note": ("geometry is NOT carried in this object — import the same IFC into the "
                          "simulator and key on the IFC GlobalIds used throughout"),
    }


def assemble_scenario(sc, number, names):
    return {
        "id": f"SCN-{number:03d}",
        "type": sc.type,
        "title": name_exit_ids(sc.title, names),
        "conditions": {
            "exits_available": name_exit_ids(sc.conditions.exits_available, names),
            "exits_discounted": name_exit_ids(sc.conditions.exits_discounted, names),
            "exits_available_ifc_ids": resolve_exit_ids(sc.conditions.exits_available, names),
            "exits_discounted_ifc_ids": resolve_exit_ids(sc.conditions.exits_discounted, names),
            "occupancy_state": sc.conditions.occupancy_state,
            "occupants_total": sc.conditions.occupants_total,
        },
        "assumptions": name_exit_ids(sc.assumptions, names),
        "occupant_distribution": name_exit_ids(sc.occupant_distribution, names),
        "routes": [name_exit_ids(r.model_dump(), names) for r in sc.routes],
        "bottlenecks": name_exit_ids(sc.bottlenecks, names),
        "risks": name_exit_ids(sc.risks, names),
        "narrative": name_exit_ids(sc.narrative, names),
        "simulation": sc.simulation.model_dump(),
        "regulatory_justification": name_exit_ids(sc.regulatory_justification, names),
        "ai_explanation": name_exit_ids(sc.ai_explanation, names),
    }


GENERATION_ATTEMPTS = int(os.getenv("EVAC_GEN_ATTEMPTS", "3"))


def invoke_structured(llm, prompt, attempts=None):
    structured = llm.with_structured_output(BuildingAnalysis)
    attempts = max(1, attempts if attempts is not None else GENERATION_ATTEMPTS)
    last_error = None
    for attempt in range(attempts):
        if last_error is None:
            ask = prompt
        else:
            ask = (f"{prompt}\n\n=== YOUR PREVIOUS REPLY WAS REJECTED ===\n"
                   f"It failed schema validation:\n{last_error}\n\n"
                   f"Return the whole BuildingAnalysis again with those fields corrected. Every bound "
                   f"in the schema is a hard limit, not a preference — clamp to the nearest allowed "
                   f"value and adjust your reasoning to match, rather than restating the value you "
                   f"first chose. Leave everything else as it was.")
        try:
            return structured.invoke(ask)
        except ValidationError as exc:
            last_error = exc
            print(f"[scenario generation] attempt {attempt + 1}/{attempts} rejected by the schema; "
                  f"{'retrying with the error fed back' if attempt + 1 < attempts else 'giving up'}",
                  file=sys.stderr)
    raise last_error


def generate_scenario_object(summary, classified, jurisdiction, gate, llm=None, model_label=None):
    if llm is None:
        llm, model_label = select_llm(max_tokens=16384,
                                      timeout=float(os.getenv("EVAC_GEN_TIMEOUT", "600")))

    grounded = ground_spaces(summary, classified, jurisdiction=jurisdiction)
    exits = resolve_exits(summary, classified, grounded)
    stairs = summary.get("stairs", [])
    storeys = summary.get("storeys", [])

    names = exit_names(exits)

    building = building_block(summary, grounded, jurisdiction)
    degraded = degraded_cases(summary, classified, grounded, jurisdiction, names)

    regulation_refs = reg_refs(jurisdiction)
    facts = facts_block(building, grounded, exits, stairs, storeys, regulation_refs, degraded, names)
    prompt = f"{_SYSTEM}\n\n=== COMPUTED BUILDING FACTS (reason only over these) ===\n{facts}\n\n=== TASK ===\n{TASK}"

    analysis = invoke_structured(llm, prompt)

    obj = {
        "schema_version": "1.0",
        "provenance": {
            "generated_by_model": model_label,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "occupancy_factor_source": (f"code occupancy load factors — "
                                        f"{jurisdiction_source.get(jurisdiction, jurisdiction_source['england'])}"
                                        f"; dwellings: NDSS bedspaces (habitable rooms + 1)"),
            "distance_method": DISTANCE_METHOD,
            "llm_grounded": True,
            "llm_temperature": os.getenv("ANTHROPIC_TEMPERATURE", "0"),
        },
        "model": model_block(summary),
        "building": building,
        "exits": exits_block(exits, names),
        "doors": doors_block(summary, exits),
        "circulation": circulation_block(summary),
        "stair_links": stair_links_block(summary, classified),
        "elevators": elevators_block(summary),
        "spaces": spaces_block(grounded, classified, names),
        "degraded_cases": degraded,
        "scenarios": [assemble_scenario(sc, n, names)
                      for n, sc in enumerate(analysis.scenarios, start=1)],
        "regulation_check": gate,
        "validation": {},
        "not_assessed": grounded["not_assessed"],
    }

    return attach_occupancy(obj)


def build_full_scenario(ifc_path, jurisdiction="england", use_llm=True, gate=None):
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
