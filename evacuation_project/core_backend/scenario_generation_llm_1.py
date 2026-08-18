#This is the main part of the project which focuses on suggesting evacuation scenarios using LLM and all the computed values of occupancy, 
# travel distance and egress are given to the LLM such that it will decide adn give the necessary scenarios

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import List, Literal

from pydantic import BaseModel, Field, ValidationError

from core_backend.llm import select_llm, structured_output
from core_backend.egress import build_graph, ground_spaces, discount_exit, stair_adjacency
from core_backend.exit_names import exit_names, name_exit_ids, named, resolve_exit_ids
from core_backend.occupancy import jurisdiction_source
from core_backend.occupancy_states import (label_of, multipliers_for, occupants_under,
                                           occupied_use_types, state_keys, state_menu)
from core_backend.ifc_parser import occupiable_storeys, parser_summary
from core_backend.occupant_placement import attach_occupancy
from core_backend.space_classifier import classify_spaces
from core_backend.uk_regulation_checking import regulation_gate, load_regs
from core_backend.sample_paths import resolve_ifc

DISTANCE_METHOD = ("geodesic shortest path over a per-storey walkable raster (0.1 m cells) from each "
                   "room's most remote point, plus stair-going descent for upper storeys — a "
                   "geometry-based measurement (approx to cell size), non-verdict")

DISCOUNT_VARIANTS = int(os.getenv("EVAC_DISCOUNT_VARIANTS", "2"))

def round_number(value, ndigits):
    return round(value, ndigits) if isinstance(value, (int, float)) else value

class Route(BaseModel):
    from_area: str = Field(description="where occupants start (a storey or space name)")
    via: str = Field(default="", description="circulation/stair route taken")
    to_exit: str = Field(description="the exit name they leave by, e.g. 'Exit 2'")
    note: str = Field(default="", description="short note, e.g. approx distance or a caveat")

class RestrictedArea(BaseModel):
    area: str = Field(description="a space, stair or circulation area occupants must NOT use in this "
                                  "scenario, named from the facts below — never invented")
    reason: str = Field(description="why it is out of use in this scenario, e.g. smoke-logged from the "
                                    "fire origin, closed for maintenance, lifts withdrawn on alarm")

# The states are a closed set defined in occupancy_states.py, so the model picks one rather than
# inventing multipliers. The schema turns this into an enum the provider constrains generation
# against, which is why a mistyped or invented state can no longer reach the object at all.
OccupancyState = Literal[tuple(state_keys())]


class ScenarioConditions(BaseModel):
    exits_available: List[str] = Field(description="exit names that stay open in this scenario, "
                                                   "e.g. ['Exit 1', 'Exit 2']")
    exits_discounted: List[str] = Field(default_factory=list,
                                        description="exit names assumed blocked/unavailable, "
                                                    "e.g. ['Exit 3']")
    occupancy_state: OccupancyState = Field(
        description="which defined occupancy state this scenario runs in — give the key exactly as "
                    "listed in the facts, e.g. 'night_sleeping'. The state IS the population: its "
                    "per-use-type multipliers are applied to the computed room loads for you, and "
                    "both the total and the per-room counts follow from that")
    # no occupants_total and no multipliers — the state determines both, and room_capacity() derives
    # the total from the same arithmetic that seats the occupants downstream, so the two can never
    # disagree and neither can cost a repair attempt.

DISTRIBUTIONS = "constant, uniform, normal, lognormal"

class ResponseDelay(BaseModel):
    distribution: str = Field(description=f"one of: {DISTRIBUTIONS}")
    mean_s: float = Field(description="mean pre-movement time in seconds")
    sd_s: float = Field(default=0.0, description="standard deviation in seconds (0 for constant)")
    basis: str = Field(description="why these values for THIS building and THIS scenario — the "
                                   "occupancy type, whether occupants may be asleep, and the alarm "
                                   "arrangement you are assuming")

class PreMovement(BaseModel):
    detection: str = Field(description="how and when the fire is detected in this scenario — the "
                                       "detection arrangement assumed and roughly how long it takes")
    alarm: str = Field(description="how the alarm is raised once detection occurs — the system type "
                                   "and whether it sounds throughout or in phases")
    recognition: str = Field(description="how long occupants take to recognise the alarm as a real "
                                         "evacuation cue, and why — sleeping occupants take longer")
    response_delay: ResponseDelay = Field(description="the pre-travel activity time once occupants "
                                                      "have recognised the alarm, as a distribution")

class OccupantProfile(BaseModel):
    name: str = Field(description="e.g. 'adult', 'child', 'reduced mobility'")
    fraction: float = Field(description="share of the occupants in this group, 0..1; the fractions "
                                        "across all profiles must sum to 1.0", ge=0, le=1)
    speed_distribution: str = Field(description=f"one of: {DISTRIBUTIONS}")
    speed_ms_mean: float = Field(description="mean unimpeded walking speed, m/s")
    speed_ms_sd: float = Field(default=0.0, description="standard deviation, m/s (0 for constant)")
    shoulder_width_m: float = Field(description="shoulder width / body diameter in metres")
    basis: str = Field(description="why this group and these values suit this building")


class FireConditions(BaseModel):
    fire_origin: str = Field(description="the space or area the fire is assumed to start in, named "
                                         "from the space list below; or 'not fire-specific' for an "
                                         "evacuation that is not driven by a fire location")
    fire_origin_storey: str = Field(default="", description="the storey the origin sits on")
    affected_exits: List[str] = Field(
        default_factory=list,
        description="exit NAMES the fire or its smoke makes unusable, e.g. ['Exit 2']. These are the "
                    "reason a scenario discounts an exit, so they must match exits_discounted")
    affected_routes: List[str] = Field(
        default_factory=list,
        description="stairs or circulation areas degraded by smoke, named from the facts")
    detection_and_alarm: str = Field(description="the detection and alarm arrangement assumed, and "
                                                 "when occupants are taken to become aware — this is "
                                                 "what the pre-movement time is measured from")
    smoke_conditions: str = Field(description="where smoke is assumed to spread and how it degrades "
                                              "the routes above (visibility, tenability)")
    basis: str = Field(description="the engineering reasoning for these fire assumptions in THIS "
                                   "building — why this origin, and why it is a meaningful test")


class SimulationDuration(BaseModel):
    seconds: float = Field(description="how long to let the simulation run, in seconds — long enough "
                                       "that everyone who can escape has escaped")
    basis: str = Field(description="why this run length suits THIS scenario")

class SimulationSettings(BaseModel):
    start_conditions: str = Field(description="the state of the building at t=0, when the clock "
                                              "starts: where occupants are, what they are doing, "
                                              "which exits and routes are open, and whether the "
                                              "alarm has yet sounded")
    duration: SimulationDuration

class EvacuationTime(BaseModel):
    estimated_total_s: float = Field(description="your ESTIMATE of the total time to clear the "
                                                 "building in this scenario, in seconds, measured "
                                                 "from ignition. Must not exceed the run duration")
    basis: str = Field(description="the arithmetic behind the estimate, quoting the values you used: "
                                   "the pre-movement time plus travel over the longest route at the "
                                   "walking speed in your profiles, plus any queueing you expect")

class SimulationSetup(BaseModel):
    movement_model: str = Field(description="'steering' (agent-based) or 'sfpe' (hydraulic)")
    simulation_settings: SimulationSettings
    pre_movement: PreMovement
    profiles: List[OccupantProfile] = Field(description="population mix; fractions must sum to 1.0")
    evacuation_time: EvacuationTime

class ScenarioContent(BaseModel):
    type: str = Field(description="e.g. 'night_occupancy', 'one_exit_discounted'")
    title: str
    purpose: str = Field(description="what this scenario is FOR — the evacuation question it puts to "
                                     "this building and what running it would settle, in one or two "
                                     "sentences")
    conditions: ScenarioConditions
    assumptions: List[str]
    occupant_distribution: List[str] = Field(
        description="where THIS scenario's occupancy_multipliers leave the population, one line per "
                    "storey or area, e.g. 'Floor_02: dwellings fully occupied, circulation empty'. "
                    "Describe which areas hold people and which are emptied — never state a headcount: "
                    "the per-room counts are computed from your multipliers and reported separately")
    routes: List[Route]
    restricted_areas: List[RestrictedArea] = Field(
        default_factory=list,
        description="spaces, stairs or circulation areas occupants must not use in this scenario, "
                    "each with the reason it is out; leave empty only if nothing is restricted")
    bottlenecks: List[str]
    risks: List[str]
    narrative: str
    fire_conditions: FireConditions
    simulation: SimulationSetup
    occupancy_rationale: str = Field(
        description="why this occupancy state is the one worth testing in THIS building — what its "
                    "population and its distribution put at risk that the other states do not")
    regulatory_justification: str = Field(
        description="the regulation clause(s) this scenario tests, cited ONLY from the given references")
    ai_explanation: str = Field(
        description="short reasoning: why you chose this scenario and what it shows")

class BuildingAnalysis(BaseModel):
    scenarios: List[ScenarioContent] = Field(
        description="At least FOUR distinct scenarios, selected autonomously from this building's "
                    "geometry, exit arrangement, storeys, occupant distribution and computed travel "
                    "distances — not chosen at random. No two may pair the SAME occupancy state with "
                    "the SAME discounted exits; that combination is one scenario run twice.")

SYSTEM_PROMPT = (
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
    "One scenario should keep all final exits available, so the set has an undegraded case to compare "
    "against — but give it a realistic occupancy state like any other, NOT the full computed load.\n\n"
    "Choose the remaining scenarios by identifying the most realistic or most challenging evacuation "
    "conditions for THIS building. Examples (not mandatory, not exhaustive): loss of the busiest exit; "
    "loss of the exit serving the largest population; loss of an upper-floor escape route; sleeping "
    "night occupancy; reduced daytime occupancy; maintenance closure of one exit; reduced exit "
    "capacity; high "
    "occupancy concentrated on one storey; congestion at a stair; or any geometry-specific evacuation "
    "challenge you can see in the facts. Pick whichever best stress the evacuation routes, and make each "
    "scenario substantially different from the others.\n\n"
    "When a scenario discounts an exit, prefer one of the exits whose DEGRADED-CASE FACTS are supplied "
    "below, and use those recomputed numbers rather than the base-case ones.\n\n"
    "For every scenario determine: purpose, occupancy_state, exits_available, "
    "exits_discounted, occupant_distribution, routes (from_area -> via -> to_exit), "
    "restricted_areas, bottlenecks, risks, assumptions and a short plain-English narrative. Say in "
    "that scenario's assumptions which population the state you chose takes out of the building.\n\n"
    "DO NOT STATE OCCUPANT HEADCOUNTS ANYWHERE IN YOUR PROSE.\n"
    "You choose the occupancy_state; the multipliers it carries are applied to the code-derived room "
    "loads for you, and both the total and the per-room counts are computed from that and reported "
    "alongside your scenario. A number you write yourself can only disagree with the computed one. "
    "Describe occupancy by state -- 'dwellings at full night occupancy, commercial empty' -- never "
    "'47 occupants'.\n\n"
    "  * purpose — what the scenario is FOR: the evacuation question it puts to THIS building and what "
    "running it would settle. Not a restatement of the title, and not the same sentence four times.\n"
    "  * restricted_areas — the spaces, stairs or circulation areas occupants must NOT use in this "
    "scenario, each with the reason it is out (smoke-logged from the fire origin, closed for "
    "maintenance, lifts withdrawn on alarm). Name them from the facts below; never invent an area. "
    "These must agree with the fire you describe: an area your smoke_conditions make untenable belongs "
    "here. Leave the list empty only if genuinely nothing is restricted.\n\n"
    "CHOOSE THE OCCUPANCY STATE -- ONE PER SCENARIO, FROM THE LIST IN THE FACTS.\n"
    "Every scenario names one occupancy_state from the states listed under OCCUPANCY STATES below. "
    "Each state carries a fixed, published set of per-use-type multipliers which are applied for you "
    "to the computed room loads. You do NOT set multipliers and must not try to. The facts show the "
    "headcount each state produces in THIS building, so you can see what you are choosing before you "
    "choose it.\n"
    "  * Pick the state that makes each scenario worth running, and say why in occupancy_rationale: "
    "what that population and its distribution put at risk that the other states do not.\n"
    "  * Vary the state across the set. The states differ in WHERE the people are as well as how "
    "many, so two scenarios in different states seed different rooms even at similar totals.\n"
    "  * No two scenarios may pair the SAME occupancy_state with the SAME exits_discounted -- that "
    "combination is one simulation run twice. The same state with a DIFFERENT exit discounted is a "
    "legitimate and useful pair: it isolates the exit as the only variable, which is exactly the "
    "comparison a discounted-exit study exists to make.\n"
    "  * occupant_distribution must describe where the state you chose actually leaves people, so it "
    "differs across the set too.\n\n"
    "FIRE-RELATED CONDITIONS — one block per scenario, and they must match the scenario.\n"
    "The IFC carries the geometry and the facts below carry the people; nothing carries the fire, so "
    "you supply it. For each scenario give fire_conditions:\n"
    "  * fire_origin (and fire_origin_storey) — the space the fire is assumed to start in, named from "
    "the space list below. Never invent a room. Put the origin somewhere that makes the scenario worth "
    "running: beside the exit you are discounting, on the storey with the longest travel distance, or "
    "in a space whose loss cuts a route. If a scenario is not fire-driven, say 'not fire-specific'.\n"
    "  * affected_exits / affected_routes — what that fire takes out. affected_exits MUST agree with "
    "exits_discounted: an exit is discounted because something makes it unusable, and this is where "
    "you say what. If the scenario discounts nothing, leave these empty.\n"
    "  * detection_and_alarm — the detection and alarm arrangement assumed and when occupants become "
    "aware. Pre-movement time is measured from that moment, so it must agree with the pre_movement "
    "detection, alarm and recognition you give in the simulation block.\n"
    "  * smoke_conditions — where smoke spreads and how it degrades those routes.\n"
    "  * basis — why this origin, in THIS building, is a meaningful test.\n"
    "Vary the fire across the set for the same reason you vary the occupancy: four scenarios with the "
    "same origin and the same affected routes are one scenario.\n\n"
    "SIMULATION PARAMETERS — THESE ONES ARE YOURS TO CHOOSE.\n"
    "The occupant loads and travel distances above are computed and off-limits. The `simulation` block "
    "is different: it is the egress-simulation set-up, and you decide it per scenario. Give:\n"
    "  * movement_model — 'steering' (agent-based, shows queueing and route choice) or 'sfpe' "
    "(hydraulic flow); say which suits the scenario.\n"
    "  * simulation_settings — how the run is configured. `start_conditions` describes the building "
    "at t=0, the moment the clock starts: where occupants are and what they are doing, which exits "
    "and routes are open, and whether the alarm has sounded yet. It must agree with this scenario's "
    "occupancy state, its available exits and its restricted areas. `duration` is how long to let "
    "the run go — long enough that everyone who can escape has escaped — with its `basis`.\n"
    "  * pre_movement — the time from ignition to occupants starting to move, broken into its four "
    "parts. Three are short prose statements of the assumption: `detection` (how and roughly how "
    "quickly the fire is detected), `alarm` (how the alarm is raised once it is — system type, "
    "throughout or phased), and `recognition` (how long occupants take to read the alarm as a real "
    "evacuation cue, and why). The fourth, `response_delay`, is the pre-travel activity time once they "
    "have recognised it, given as a DISTRIBUTION, not a single number: choose values that fit this "
    "building's occupancy and whether occupants may be asleep, and give your reasoning in its `basis`. "
    "Typical engineering practice puts that delay well under 30 minutes; a sleeping residential "
    "occupancy warrants a longer and more spread-out time than an alert one. All four must tell one "
    "consistent story, and it must be the same story as fire_conditions.detection_and_alarm.\n"
    "  * profiles — the population mix (e.g. adults, children, reduced mobility), each with a walking "
    "speed distribution and shoulder width. Unimpeded walking speeds for able adults on the level are "
    "around 1.2 m/s and should stay inside 0.5-2.0 m/s; shoulder widths sit around 0.45-0.5 m. The "
    "`fraction` values MUST sum to exactly 1.0.\n"
    "  * evacuation_time — your ESTIMATE of how long this scenario takes to clear the building, "
    "measured from ignition. Work it out rather than guessing it: the pre-movement time, plus travel "
    "over the LONGEST computed route in this scenario at the walking speed in your own profiles, plus "
    "whatever queueing the exit widths and occupant numbers lead you to expect. Quote that arithmetic "
    "in `basis`, using the computed distances as given. It must not exceed your run duration — a run "
    "that ends before the building is clear has not answered the question. This figure is an "
    "engineering estimate stated up front, NOT a simulation result: the egress simulation is what "
    "actually determines the evacuation time, and it may well disagree with you.\n"
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
    adjacency, positions, final_exits = build_graph(summary, classified)
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
    for exit_id, usage_count in usage.most_common(limit):
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
        lines.append(f"  - {s.get('name')}: elevation={round_number(s.get('elevation_m'), 2)}, "
                     f"height_above_ground={round_number(s.get('height_above_ground_m'), 2)}")

    lines += ["", "Final (ground-level) exits — occupants leave by these, refer to them by NAME:"]
    width_of = {e["id"]: e.get("width_m") for e in exits}
    for exit_id, name in names.items():          
        lines.append(f"  - {name} (width_m={round_number(width_of.get(exit_id), 2)})")

    if stairs:
        lines += ["", "Internal stairs (connect storeys):"]
        for st in stairs:
            lines.append(f"  - {st['id']} (name={st.get('name')}, width_m={round_number(st.get('width'), 2)})")

    lines += ["", "BASE-CASE per-storey rollup (computed — occupants / spaces / longest travel "
                  "distance m / unreachable):"]
    for storey, row in storey_rollup(grounded).items():
        lines.append(f"  - {storey}: occupants={row['occupants']} spaces={row['spaces']} "
                     f"max_travel_m={round(row['max_dist'], 1)} unreachable={row['unreachable']}")

    lines += ["", "OCCUPANT LOAD BY USE TYPE (computed — this is what an occupancy state acts on):"]
    by_use = defaultdict(lambda: [0, 0])
    for s in spaces:
        if (s["occupant_load"] or 0) > 0:
            row = by_use[s["use_type"]]
            row[0] += s["occupant_load"]
            row[1] += 1
    for use_type, (occupants, rooms) in sorted(by_use.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  - {use_type}: {occupants} occupants across {rooms} room(s)")

    # The states and, crucially, the headcount each one produces HERE. Choosing a state without
    # seeing its effect was the model reasoning blind: the multipliers set the population, but the
    # population is computed downstream, so nothing in the reply ever showed what a choice cost.
    lines += ["", "OCCUPANCY STATES (choose one per scenario — occupancy_state takes the key):",
              state_menu(spaces)]

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
                     f"area={round_number(s['area_m2'], 1)} | occupants={s['occupant_load']} | "
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
        "storeys": len(occupiable_storeys(summary.get("storeys", []))),
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
            "area_m2": round_number(s["area_m2"], 2),
            "centroid": [round_number(v, 2) for v in s["centroid"]] if s["centroid"] else None,
            "occupant_load": s["occupant_load"],
            "occupant_basis": s["occupant_basis"],
            "nearest_exit": s["nearest_exit"],
            "nearest_exit_name": named(s["nearest_exit"], names),
            "travel_distance_m": round_number(s["travel_distance_m"], 1),
            "travel_distance_method": s.get("travel_distance_method"),
            "most_remote_point": s.get("most_remote_point"),
            "reachable": s["reachable"],
            "reachability_note": s.get("reachability_note"),
        })
    return out


def point(p):
    return [round_number(v, 2) for v in p] if p else None


def exits_block(exits, names):
    order = {exit_id: n for n, exit_id in enumerate(names)}      # names are in plan order
    return [{"id": e["id"], "exit_name": named(e["id"], names), "name": e.get("name"),
             "type": "final_exit", "width_m": round_number(e.get("width_m"), 2),
             "position": point(e.get("position"))}
            for e in sorted(exits, key=lambda e: order.get(e["id"], len(order)))]

storey_match_tol_m = 1.0

def circulation_block(summary):
    flights = {f["id"]: f for f in summary.get("stair_flights", [])}
    storeys = occupiable_storeys(summary.get("storeys", []))

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
            "width_m": round_number(st.get("width"), 2), "width_source": st.get("width_source"),
            "position": point(position),
            "rise_m": round_number(rise, 2), "going_m": round_number(going, 2), "slope_m": round_number(slope, 2),
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
             "width_m": round_number(d.get("width_m"), 2), "position": point(d.get("position")),
             "connects": links.get(d["id"], [])}
            for d in summary.get("doors", []) if d["id"] not in final]


def elevators_block(summary):
    return [{"id": t["id"], "name": t.get("name"), "type": "elevator",
             "is_evac_lift": t.get("is_evac_lift"), "position": point(t.get("position"))}
            for t in summary.get("elevators", [])]


def model_block(summary):
    return {
        "source_ifc": summary.get("source_ifc"),
        "units": "m",
        "coordinate_system": "ifc_world_coordinates",
        "geometry_note": ("geometry is NOT carried in this object — import the same IFC into the "
                          "simulator and key on the IFC GlobalIds used throughout"),
    }


def assemble_scenario(sc, number, names, spaces):
    # occupancy_state and occupants_total live in the occupancy block alone — attach_occupancy() reads
    # them from here and returns them in the full block, so they are stated once per scenario.
    return {
        "id": f"SCN-{number:03d}",
        "type": sc.type,
        "title": name_exit_ids(sc.title, names),
        "scenario_objective": {
            "purpose": name_exit_ids(sc.purpose, names),
            "conditions": {
                "exits_discounted": name_exit_ids(sc.conditions.exits_discounted, names),
                "exits_discounted_ifc_ids": resolve_exit_ids(sc.conditions.exits_discounted, names),
            },
        },
        "evacuation_routes": {
            "exits_available": name_exit_ids(sc.conditions.exits_available, names),
            "exits_available_ifc_ids": resolve_exit_ids(sc.conditions.exits_available, names),
            "routes": [name_exit_ids(r.model_dump(), names) for r in sc.routes],
            "restricted_areas": [name_exit_ids(a.model_dump(), names) for a in sc.restricted_areas],
        },
        "assumptions": name_exit_ids(sc.assumptions, names),
        "occupant_distribution": name_exit_ids(sc.occupant_distribution, names),
        "bottlenecks": name_exit_ids(sc.bottlenecks, names),
        "risks": name_exit_ids(sc.risks, names),
        "narrative": name_exit_ids(sc.narrative, names),
        "fire_conditions": name_exit_ids(sc.fire_conditions.model_dump(), names),
        # the multipliers are the state's, looked up here rather than returned by the model, so the
        # object still carries them where placement, validation and the export already read them
        "simulation": dict(sc.simulation.model_dump(),
                           occupancy_multipliers=multipliers_for(
                               sc.conditions.occupancy_state, occupied_use_types(spaces))),
        "occupancy": {
            # computed, not chosen: the same sum occupant_placement uses to seat people
            "occupants_total": room_capacity(spaces, sc),
            "occupancy_state": sc.conditions.occupancy_state,
            "occupancy_state_label": label_of(sc.conditions.occupancy_state),
        },
        "occupancy_rationale": name_exit_ids(sc.occupancy_rationale, names),
        "regulatory_justification": name_exit_ids(sc.regulatory_justification, names),
        "ai_explanation": name_exit_ids(sc.ai_explanation, names),
    }


GENERATION_ATTEMPTS = int(os.getenv("EVAC_GEN_ATTEMPTS", "3"))
# four scenarios come back at roughly 12k tokens, and a repair attempt re-emits the whole structure,
# so the old 16384 ceiling left little headroom. Claude Sonnet 5 thinks by default and max_tokens caps
# thinking AND response text together, so the reasoning now eats into the same budget the structure
# needs — 24000 was sized before that was true. Only what the model writes is billed, so the higher
# ceiling costs nothing on a normal run — it just stops a long reply being cut off part-way.
# 64000 is half of Sonnet 5's 128k output ceiling; raise EVAC_GEN_MAX_TOKENS further if needed.
GENERATION_MAX_TOKENS = int(os.getenv("EVAC_GEN_MAX_TOKENS", "64000"))


# Anthropic reports stop_reason, Mistral reports finish_reason, and they spell a cut-off differently
TRUNCATED = {"max_tokens", "length", "model_length"}


class NoStructuredReply(RuntimeError):
    """The model answered without the structured result the schema call requires."""


def reply_diagnosis(raw):
    """What the provider actually said about the reply, rather than a guess about why it failed."""
    metadata = getattr(raw, "response_metadata", None) or {}
    stop = metadata.get("stop_reason") or metadata.get("finish_reason")
    written = (getattr(raw, "usage_metadata", None) or {}).get("output_tokens")

    if stop in TRUNCATED:
        wrote = f"after writing {written} tokens " if written else ""
        return (f"The reply was cut off {wrote}against the {GENERATION_MAX_TOKENS}-token ceiling "
                f"({stop}), so the structure never finished. Raise EVAC_GEN_MAX_TOKENS.")

    text = " ".join(str(getattr(raw, "content", "") or "").split())
    if text:
        return (f"The reply finished normally ({stop}) but carried prose instead of the structure. "
                f"It began: \"{text[:200]}\"")
    return f"The reply carried no structured result and no text ({stop})."


def scenario_signature(sc):
    """What makes two scenarios the same run: the occupancy state they sit in and the exits they
    close. The state fixes the population and the rooms it starts in; the discounted exits fix the
    ways out it has left. Nothing else decides which simulation this is.

    The signature used to be the multiplier set alone, which made the classic pair — a base case,
    and the same occupancy with the main exit lost — read as a duplicate and cost a repair attempt.
    That pair is the comparison a discounted-exit study exists to make, so it has to be allowed."""
    return (sc.conditions.occupancy_state,
            tuple(sorted(sc.conditions.exits_discounted or [])))


def room_capacity(spaces, sc):
    """How many occupants this scenario's occupancy state leaves in the building: every room's
    computed load, thinned by the multiplier its use type takes in that state. This is the sum
    occupant_placement.scenario_weights takes downstream — the arithmetic that actually seats
    people — so the stated total and the seating can never disagree."""
    return occupants_under(spaces, sc.conditions.occupancy_state)


def occupancy_findings(analysis, spaces):
    """What is left to review once the occupancy states are a table rather than the model's to
    invent. Three faults used to live here and all three are now unreachable:

    a total the multipliers could not seat went when the total became the seat count;
    two scenarios cancelling to the same total went with the multipliers themselves;
    and a night state thinner than its day went when the states became a table whose night holds
    every residential use type at 1.0 — the schema maximum, which no daytime state can exceed on any
    building. occupancy_states.check_states() proves that once at import, instead of a repair
    attempt proving it again on every run.

    One fault survives, because it is the only one the model can still commit: pairing the same
    occupancy state with the same discounted exits describes one simulation twice."""
    findings = []
    by_signature = defaultdict(list)
    for number, sc in enumerate(analysis.scenarios, start=1):
        by_signature[scenario_signature(sc)].append(f"scenario {number} ({sc.type})")

    for (state, discounted), labels in by_signature.items():
        if len(labels) < 2:
            continue
        closed = (f"with {', '.join(discounted)} discounted" if discounted
                  else "with every exit available")
        findings.append(
            f"{' and '.join(labels)} are both '{state}' {closed}, so they seed the same occupants "
            f"into the same rooms and leave them the same ways out — one simulation described "
            f"twice. Move one of them to a different occupancy_state, or discount a different exit "
            f"in it.")
    return findings


def repair_prompt(prompt, failure):
    if isinstance(failure, ValidationError):
        return (f"{prompt}\n\n=== YOUR PREVIOUS REPLY WAS REJECTED ===\n"
                f"It failed schema validation:\n{failure}\n\n"
                f"Return the whole BuildingAnalysis again with those fields corrected. Every bound "
                f"in the schema is a hard limit, not a preference — clamp to the nearest allowed "
                f"value and adjust your reasoning to match, rather than restating the value you "
                f"first chose. Leave everything else as it was.")
    if isinstance(failure, list):
        listed = "\n".join(f"  - {finding}" for finding in failure)
        return (f"{prompt}\n\n=== YOUR PREVIOUS REPLY WAS REJECTED ===\n"
                f"Every field was in range, but the scenario set describes the same run twice:"
                f"\n{listed}\n\n"
                f"Return the whole BuildingAnalysis again with those scenarios corrected. A "
                f"scenario is identified by its occupancy_state and the exits it discounts: the "
                f"state decides how many occupants there are and which rooms they start in, and the "
                f"discounted exits decide the ways out they have left. Two scenarios sharing both "
                f"are one simulation. Change the occupancy_state or the discounted exits on the "
                f"scenarios named above, and leave the others exactly as they are.")
    return (f"{prompt}\n\n=== YOUR PREVIOUS REPLY WAS REJECTED ===\n"
            f"It carried no structured result at all — the reply either ran past the "
            f"{GENERATION_MAX_TOKENS}-token ceiling part-way through or answered in prose.\n\n"
            f"Return the whole BuildingAnalysis as a structured result. Keep every required field "
            f"and every scenario, but write the prose fields tersely enough that the reply finishes.")


def invoke_structured(llm, prompt, attempts=None, review=None):
    """``review`` returns a list of findings on an otherwise valid reply. Findings are handed back
    the way a schema error is, but they never sink the run: a flawed scenario set is still a usable
    one, and validation reports every finding downstream, so the closest reply is kept rather than an
    expensive call thrown away."""
    structured = structured_output(llm, BuildingAnalysis)
    attempts = max(1, attempts if attempts is not None else GENERATION_ATTEMPTS)
    last_failure = None
    closest = None
    for attempt in range(attempts):
        ask = prompt if last_failure is None else repair_prompt(prompt, last_failure)
        # include_raw keeps a failed parse as data rather than an exception, so the reason a reply
        # was unusable comes off the provider's own metadata instead of being assumed
        result = structured.invoke(ask)
        analysis = result.get("parsed")
        error = result.get("parsing_error")

        if isinstance(error, ValidationError):
            last_failure = error
            reason = "rejected by the schema"
        elif analysis is None:
            # a reply carrying no structure parses to None rather than raising, so it has to be
            # caught here — left alone it reaches the caller as an attribute error on None
            last_failure = NoStructuredReply(reply_diagnosis(result.get("raw")))
            reason = "no structured result returned"
        else:
            findings = review(analysis) if review is not None else []
            if not findings:
                return analysis
            if closest is None or len(findings) < len(closest[1]):
                closest = (analysis, findings)
            last_failure = findings
            reason = f"{len(findings)} occupancy finding(s)"
        print(f"[scenario generation] attempt {attempt + 1}/{attempts} {reason}; "
              f"{'retrying with the reason fed back' if attempt + 1 < attempts else 'giving up'}",
              file=sys.stderr)
    if closest is not None:
        print(f"[scenario generation] {len(closest[1])} occupancy finding(s) survived "
              f"{attempts} attempt(s); keeping the closest set — validation reports them on the "
              f"generated object", file=sys.stderr)
        return closest[0]
    raise last_failure


def generate_scenario_object(summary, classified, jurisdiction, gate, llm=None, model_label=None):
    if llm is None:
        llm, model_label = select_llm(max_tokens=GENERATION_MAX_TOKENS,
                                      timeout=float(os.getenv("EVAC_GEN_TIMEOUT", "600")))

    grounded = ground_spaces(summary, classified, jurisdiction=jurisdiction)
    exits = resolve_exits(summary, classified, grounded)
    stairs = summary.get("stairs", [])
    storeys = occupiable_storeys(summary.get("storeys", []))

    names = exit_names(exits)

    building = building_block(summary, grounded, jurisdiction)
    degraded = degraded_cases(summary, classified, grounded, jurisdiction, names)

    regulation_refs = reg_refs(jurisdiction)
    facts = facts_block(building, grounded, exits, stairs, storeys, regulation_refs, degraded, names)
    prompt = f"{SYSTEM_PROMPT}\n\n=== COMPUTED BUILDING FACTS (reason only over these) ===\n{facts}\n\n=== TASK ===\n{TASK}"

    analysis = invoke_structured(
        llm, prompt,
        review=lambda reply: occupancy_findings(reply, grounded["spaces"]))

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
        "scenarios": [assemble_scenario(sc, n, names, grounded["spaces"])
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
        print(f"objective: {scn['scenario_objective']}")
        print(f"routes: {len(scn['evacuation_routes']['routes'])} "
              f"| restricted areas: {len(scn['evacuation_routes']['restricted_areas'])} "
              f"| bottlenecks: {len(scn['bottlenecks'])} | risks: {len(scn['risks'])}")
        print(f"narrative: {scn['narrative'][:280]}...")
