#This is the core part of the project where it creates the whole building evacuation scenarios having the
#relevant IFC elements, regulatory justification, an AI explanation and structured scenario inputs
#(fire scenario, occupancy, escape strategy, occupant behaviour, engineering assumptions). Occupancy and
#travel distance are COMPUTED deterministically (occupancy.py + travel_distance.py, joined by
#egress.ground_spaces) and handed to the model as facts; the AI only decides WHICH scenarios are worth
#generating and writes them up, in a SINGLE structured API call. Regulation pass/fail is a separate
#blocking gate (see uk_regulation_checking).

import os
import sys
from collections import Counter
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

from core_backend.llm import select_llm
from core_backend.egress import build_graph, ground_spaces, discount_exit, stair_adjacency
from core_backend.occupancy import JURISDICTION_SOURCE
from core_backend.ifc_parser import parser_summary
from core_backend.occupant_placement import attach_occupancy
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
# What the single API call returns: the AI-chosen scenario set, in the SCN-001 shape.
# The numbers (occupant loads, travel distances) are NOT the model's job — see _SYSTEM below.
# ---------------------------------------------------------------------------------------------------
class FireScenario(BaseModel):
    fire_origin: Optional[str] = Field(default=None, description="space/area where the fire starts, "
                                       "if this scenario involves one; leave null otherwise")
    blocked_elements: List[str] = Field(default_factory=list,
                                        description="ids of doors/stairs/exits blocked by fire or smoke")
    smoke_condition: str = Field(default="none", description="'none', 'light', or 'heavy'")


class OccupantProfile(BaseModel):
    """One population group: how big its people are and how fast they walk."""
    name: str = Field(description="e.g. 'adult', 'child', 'reduced mobility'")
    fraction: float = Field(description="share of the occupants in this group, 0..1; the fractions "
                                        "across all profiles must sum to 1.0", ge=0, le=1)
    speed_ms_mean: float = Field(description="mean unimpeded walking speed, m/s (able adults ~1.2 m/s, "
                                             "stay within 0.5-2.0 m/s)")
    shoulder_width_m: float = Field(description="shoulder width / body diameter in metres (~0.45-0.5)")
    basis: str = Field(description="why this group and these values suit this building")


class OccupancyInputs(BaseModel):
    occupancy_state: str = Field(description="e.g. 'night', 'day', 'peak occupancy'")
    occupants_total: int = Field(description="occupants to evacuate under this scenario's state; must "
                                             "not exceed the computed total occupant load")
    profiles: List[OccupantProfile] = Field(description="population mix; fractions must sum to 1.0")


class EscapeStrategy(BaseModel):
    available_exits: List[str] = Field(description="exit ids that stay open in this scenario")
    blocked_exits: List[str] = Field(default_factory=list, description="exit ids assumed unavailable")
    routing_strategy: str = Field(default="", description="short note on how occupants are routed to "
                                                           "the available exits")


class OccupantBehaviour(BaseModel):
    movement_model: str = Field(description="'steering' (agent-based, shows queueing/route choice) or "
                                             "'sfpe' (hydraulic flow)")
    pre_movement_time: str = Field(description="pre-travel activity time as a short string, including "
                                               "the basis — e.g. 'lognormal, mean 120s, alert daytime "
                                               "occupancy' or 'normal, mean 480s, asleep at night'")


class ScenarioInputs(BaseModel):
    fire_scenario: FireScenario
    occupancy: OccupancyInputs
    escape_strategy: EscapeStrategy
    occupant_behaviour: OccupantBehaviour
    engineering_assumptions: List[str] = Field(default_factory=list)


class RelevantIfcElements(BaseModel):
    spaces: List[str] = Field(default_factory=list, description="space guids this scenario concerns")
    doors: List[str] = Field(default_factory=list, description="door ids this scenario concerns")
    stairs: List[str] = Field(default_factory=list, description="stair ids this scenario concerns")
    exits: List[str] = Field(default_factory=list, description="exit ids this scenario concerns")


class RegulatoryJustification(BaseModel):
    codes: List[str] = Field(default_factory=list, description="regulation unique_ids cited, from the "
                                                                "REGULATION REFERENCES given — never "
                                                                "invented")
    objective: str = Field(default="", description="what the cited regulation is trying to achieve")
    reason: str = Field(default="", description="why this scenario tests that regulation")


class ScenarioContent(BaseModel):
    scenario_id: str = Field(description="short id you assign, e.g. 'SCN-001', 'SCN-002'")
    description: str = Field(description="one-line description of the scenario")
    relevant_ifc_elements: RelevantIfcElements
    regulatory_justification: RegulatoryJustification
    ai_explanation: str = Field(description="short reasoning: why you chose this scenario and what it "
                                             "shows")
    scenario_inputs: ScenarioInputs


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
    "Refer to spaces, doors, stairs and exits only by the ids given.\n\n"
    "YOUR TASK IS THE SCENARIO SET.\n"
    "Create AT LEAST FOUR distinct evacuation scenarios, generated autonomously from the building "
    "geometry, storey layout, space types, occupant distribution, exit locations, computed travel "
    "distances and the circulation network. Do NOT choose scenarios randomly — analyse the building and "
    "create scenarios that are meaningful for this specific IFC model.\n\n"
    "The first scenario MUST always be the Base Case: all final exits available, normal occupancy, no "
    "fire/smoke condition (fire_scenario.smoke_condition = 'none', fire_origin = null, "
    "blocked_elements = []).\n\n"
    "Choose the remaining scenarios by identifying the most realistic or most challenging evacuation "
    "conditions for THIS building. Examples (not mandatory, not exhaustive): loss of the busiest exit; "
    "loss of the exit serving the largest population; loss of an upper-floor escape route; night "
    "occupancy; daytime peak occupancy; maintenance closure of one exit; reduced exit capacity; high "
    "occupancy concentrated on one storey; a fire in a specific space blocking nearby elements. Pick "
    "whichever best stress the evacuation routes, and make each scenario substantially different from "
    "the others.\n\n"
    "When a scenario discounts an exit, prefer one of the exits whose DEGRADED-CASE FACTS are supplied "
    "below, and use those recomputed numbers rather than the base-case ones.\n\n"
    "FOR EVERY SCENARIO, fill in:\n"
    "  * scenario_id, description — a short id you assign and a one-line description.\n"
    "  * relevant_ifc_elements — the space guids / door ids / stair ids / exit ids this scenario "
    "actually concerns, all copied verbatim from the facts below. Leave a list empty if none apply.\n"
    "  * fire_scenario — fire_origin (a space name/guid from the facts, or null if this scenario has no "
    "fire), blocked_elements (ids blocked by the fire/smoke, or empty), smoke_condition ('none', "
    "'light' or 'heavy'). Most scenarios (e.g. an exit closed for maintenance) have no fire condition — "
    "leave this at its 'none'/null/empty defaults unless the scenario is specifically fire-driven.\n"
    "  * scenario_inputs.occupancy — occupancy_state, occupants_total (must not exceed the computed "
    "total occupant load; if reduced, say why in engineering_assumptions), and profiles (population "
    "mix; fractions MUST sum to exactly 1.0).\n"
    "  * scenario_inputs.escape_strategy — available_exits, blocked_exits, and a short "
    "routing_strategy note.\n"
    "  * scenario_inputs.occupant_behaviour — movement_model ('steering' or 'sfpe', say which suits "
    "the scenario) and pre_movement_time as a short string stating both the value and its basis "
    "(occupancy type, whether occupants may be asleep, the alarm arrangement assumed). Typical "
    "engineering practice sits well under 30 minutes; a sleeping residential occupancy warrants a "
    "longer and more spread-out time than an alert one.\n"
    "  * scenario_inputs.engineering_assumptions — any other assumptions made for this scenario.\n"
    "  * regulatory_justification — codes (cited ONLY from the REGULATION REFERENCES given below, by "
    "their ids; never invent a clause number), objective, and reason.\n"
    "  * ai_explanation — one or two sentences on why you chose this scenario and what it demonstrates "
    "for egress.\n\n"
    "The occupant profiles, movement model, pre-movement time and routing strategy are the only things "
    "in the whole output you are permitted to originate — everything else (ids, counts, distances) must "
    "still come verbatim from the computed facts.\n\n"
    "If some spaces could not be assessed, treat them as an open risk, never as safe."
)

_TASK = "Produce the BuildingAnalysis: your chosen scenarios, written from the computed facts."


# ---------------------------------------------------------------------------------------------------
# Grounding: the computed egress results are the facts the model reasons over.
# UNCHANGED from the original file — none of this needed to move for the schema change.
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
        lines += ["", "Regulation references (cite each scenario's regulatory_justification.codes ONLY "
                      "from these):"]
        for r in reg_refs:
            lines.append(f"  - {r['id']}: {r['name']} (ref: {r['reference']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# Deterministic assembly of the whole-building object around the AI-chosen scenarios.
# UNCHANGED from the original file — these build `building`/`exits`/`doors`/etc, not the scenario shape.
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
            # where a simulator seeds this room's occupants (metres, IFC world coords)
            "centroid": [_round(v, 2) for v in s["centroid"]] if s["centroid"] else None,
            "occupant_load": s["occupant_load"],
            "occupant_basis": s["occupant_basis"],
            "nearest_exit": s["nearest_exit"],
            "travel_distance_m": _round(s["travel_distance_m"], 1),
            "travel_distance_method": s.get("travel_distance_method"),
            "most_remote_point": s.get("most_remote_point"),
            "reachable": s["reachable"],
            "reachability_note": s.get("reachability_note"),
        })
    return out


def _point(p):
    """A position tuple as a plain JSON array of metres, or None."""
    return [_round(v, 2) for v in p] if p else None


def _exits_block(exits):
    return [{"id": e["id"], "name": e.get("name"), "type": "final_exit",
             "width_m": _round(e.get("width_m"), 2), "position": _point(e.get("position"))}
            for e in exits]


# an IfcStair's base and top are matched to storey elevations within this tolerance
_STOREY_MATCH_TOL_M = 1.0


def _circulation_block(summary):
    """The IfcStair elements, enriched with the flight geometry and storey span an egress simulator
    needs to rebuild the vertical connections (all of it already parsed, none of it exported before)."""
    flights = {f["id"]: f for f in summary.get("stair_flights", [])}
    storeys = summary.get("storeys", [])

    def storey_at(z):
        if z is None or not storeys:
            return None
        near = min(storeys, key=lambda s: abs(s["elevation_m"] - z))
        return near["name"] if abs(near["elevation_m"] - z) <= _STOREY_MATCH_TOL_M else None

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


def _stair_links_block(summary, classified):
    """Stair spaces the egress graph joins vertically — the connectivity travel_distance actually
    walked, so a simulator's own vertical links can be checked against it."""
    use_type = {c["guid"]: c["use_type"] for c in classified}
    storey_of = {s["id"]: (s["storey"] or {}).get("name") for s in summary["spaces"]}
    return [{"space_a": a, "space_b": b, "storey_a": storey_of.get(a), "storey_b": storey_of.get(b)}
            for a, b in stair_adjacency(summary["spaces"], use_type)]


def _doors_block(summary, exits):
    """Internal doors — the room-to-room connections. Final exits are excluded (they are in `exits`)."""
    final = {e["id"] for e in exits}
    links = summary.get("door_space_links", {})
    return [{"id": d["id"], "name": d.get("name"), "type": "internal_door",
             "width_m": _round(d.get("width_m"), 2), "position": _point(d.get("position")),
             "connects": links.get(d["id"], [])}
            for d in summary.get("doors", []) if d["id"] not in final]


def _elevators_block(summary):
    return [{"id": t["id"], "name": t.get("name"), "type": "elevator",
             "is_evac_lift": t.get("is_evac_lift"), "position": _point(t.get("position"))}
            for t in summary.get("elevators", [])]


def _model_block(summary):
    """How to line this object up with the geometry an egress simulator imports from the same IFC."""
    return {
        "source_ifc": summary.get("source_ifc"),
        "units": "m",
        # parser_summary builds spaces with use-world-coords=True, so every position/centroid here is
        # already in the IFC's world frame — no re-projection needed on import.
        "coordinate_system": "ifc_world_coordinates",
        "geometry_note": ("geometry is NOT carried in this object — import the same IFC into the "
                          "simulator and key on the IFC GlobalIds used throughout"),
    }


def _assemble_scenario(sc):
    """CHANGED: now returns the SCN-001 shape (scenario_id / description / relevant_ifc_elements /
    regulatory_justification / ai_explanation / scenario_inputs) instead of the old
    id/type/title/conditions/routes/bottlenecks/risks/narrative/simulation shape."""
    return {
        "scenario_id": sc.scenario_id,
        "description": sc.description,
        "relevant_ifc_elements": sc.relevant_ifc_elements.model_dump(),
        "regulatory_justification": sc.regulatory_justification.model_dump(),
        "ai_explanation": sc.ai_explanation,
        "scenario_inputs": sc.scenario_inputs.model_dump(),
    }


GENERATION_ATTEMPTS = int(os.getenv("EVAC_GEN_ATTEMPTS", "3"))


def _invoke_structured(llm, prompt, attempts=None):
    """The generation call, retried with the schema's own rejection handed back to the model.

    The scenario call is the expensive one — 16k tokens and up to a 600 s read — and it is a single
    shot. One field the model puts out of range otherwise throws the entire run away: a profile
    `fraction` set that doesn't sum to 1.0, or an occupants_total above the computed load, is the kind
    of thing seen in practice.

    Showing the model the exact validation error lets it correct that one field rather than the user
    losing the whole generation. The constraints themselves are never relaxed — a reply that keeps
    breaking them still raises, so this cannot become a way to smuggle an invalid value through.
    """
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
    """Assemble the whole-building scenario object: computed egress + ONE grounded LLM call for the
    scenario set + the regulation gate. UNCHANGED except that `analysis.scenarios` now come back in the
    new SCN-001 shape via the updated `_assemble_scenario`."""
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

    analysis = _invoke_structured(llm, prompt)

    obj = {
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
        "model": _model_block(summary),
        "building": building,
        "exits": _exits_block(exits),
        "doors": _doors_block(summary, exits),
        "circulation": _circulation_block(summary),
        "stair_links": _stair_links_block(summary, classified),
        "elevators": _elevators_block(summary),
        "spaces": _spaces_block(grounded, classified),
        "degraded_cases": degraded,
        "scenarios": [_assemble_scenario(sc) for sc in analysis.scenarios],
        "regulation_check": gate,
        "validation": {},
        "not_assessed": grounded["not_assessed"],
    }

    # Deterministic post-pass: spread each scenario's occupants over the rooms and give them a goal.
    # NOTE: attach_occupancy() previously read scn["conditions"]/scn["simulation"] on each scenario —
    # those keys no longer exist under the new shape. If it still expects them it will break here;
    # check occupant_placement.py and update it to read scn["scenario_inputs"]["occupancy"] and
    # scn["scenario_inputs"]["escape_strategy"] instead before relying on this in production.
    return attach_occupancy(obj)


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
        # CHANGED: old prints used scn['id']/scn['type']/scn['title']/scn['conditions']/scn['routes']/
        # scn['bottlenecks']/scn['risks']/scn['narrative'] — none of those keys exist any more.
        occ = scn["scenario_inputs"]["occupancy"]
        strategy = scn["scenario_inputs"]["escape_strategy"]
        print(f"\n{scn['scenario_id']} — {scn['description']}")
        print(f"occupancy_state={occ['occupancy_state']} occupants_total={occ['occupants_total']} "
              f"available_exits={len(strategy['available_exits'])} "
              f"blocked_exits={len(strategy['blocked_exits'])}")
        print(f"ai_explanation: {scn['ai_explanation'][:280]}...")