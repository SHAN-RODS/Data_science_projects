import os
import sys
import streamlit as st
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core_backend.exit_names import exit_names, name_exit_ids, named
from core_backend.ifc_parser import parser_summary
from core_backend.uk_regulation_checking import regulation_gate
from core_backend.llm import select_llm
from core_backend.scenario_generation_llm_1 import build_full_scenario
from core_backend.validation import validate
from core_backend.export_results import (evacuation_time, export_records, build_records,
                                         fire_related_conditions, movement_characteristic,
                                         pre_movement_time)

load_dotenv()
os.makedirs("uploads", exist_ok=True)

jurisdictions = {
    "Approved Document B Volume 1(England)": "england",
    "Approved Document B Volume 1(Wales)": "wales",
    "Technical Booklet E-2012 (Northern ireland)": "northern_ireland",
    "Technical Handbooks 2022(Scotland)": "scotland",
}

st.set_page_config(page_title="NLP Evacuation Scenario Generator", layout="wide")

for key in ("gate_result", "gate_context", "ifc_path", "scenario_object"):
    st.session_state.setdefault(key, None)

try:
    llm, model_label = select_llm()
except Exception:
    model_label = "no LLM configured"

# header
st.title("AI-Assisted Evacuation Scenario Generator")
st.caption(
    "Upload an IFC/BIM model. The building is first checked against the selected regulation either pass or fail "
    "and later gives the AI generated scenarios."

)
st.caption(f"The Scenario reasoning model used: {model_label}")
st.divider()

# sidebar
with st.sidebar:
    st.header("1. Regulation")
    jurisdiction_label = st.selectbox("Select documents from:", list(jurisdictions.keys()))
    jurisdiction = jurisdictions[jurisdiction_label]

    st.divider()
    st.header("2. Upload an IFC model")
    uploaded_file = st.file_uploader("Select an .ifc file", type=["ifc"],
                                     help="IFC export from Revit or ArchiCAD.")

    if uploaded_file is not None:
        st.success(f"Ready: {uploaded_file.name}")
        if st.button("Validate", type="primary", width='stretch'):
            save_path = os.path.join("uploads", uploaded_file.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            with st.spinner("Parsing IFC · checking against the selected regulation…"):
                summary = parser_summary(save_path)
                gate = regulation_gate(summary, jurisdiction=jurisdiction)
            st.session_state.ifc_path = save_path
            st.session_state.gate_result = gate
            st.session_state.gate_context = (uploaded_file.name, jurisdiction)
            st.session_state.scenario_object = None
            st.rerun()

    # Step 2 unlocks only when the current file+jurisdiction passed the gate.
    gate = st.session_state.gate_result
    context_ok = (uploaded_file is not None
                  and st.session_state.gate_context == (uploaded_file.name, jurisdiction))
    if (gate is not None and gate.get("passed") and context_ok
            and st.session_state.scenario_object is None):
        st.divider()
        st.caption("Building passed — generation unlocked.")
        if st.button("Generate scenarios", type="primary", width='stretch'):
            with st.spinner("Classifying spaces with AI · computing occupancy & travel distances · "
                            "generating the scenarios in one AI call…"):
                obj = validate(build_full_scenario(st.session_state.ifc_path,
                                                   jurisdiction=jurisdiction, gate=gate))
            st.session_state.scenario_object = obj
            st.rerun()

    if st.session_state.gate_result is not None or st.session_state.scenario_object is not None:
        st.divider()
        if st.button("Start over / upload a new file", width='stretch'):
            for key in ("gate_result", "gate_context", "ifc_path", "scenario_object"):
                st.session_state[key] = None
            st.rerun()


# ---- presentation helpers ---------------------------------------------------------------------
# The page shows one long object. These keep every block to the same rhythm — a heading, a line of
# plain-English context, then the content — so a reader can skim it rather than hunt through it.

def section(title, caption=None):
    st.subheader(title)
    if caption:
        st.caption(caption)


def field(label, value):
    """A labelled prose value. Long text overflows a metric tile, so it gets its own line."""
    st.markdown(f"**{label}**  \n{value if value not in (None, '') else '—'}")


def bullets(title, items, empty="Nothing recorded for this scenario."):
    st.markdown(f"**{title}**")
    if items:
        for line in items:
            st.markdown(f"- {line}")
    else:
        st.caption(empty)


def render_gate(gate, label):
    section("Step 1 — Regulation check",
            "Measured against the selected regulation before any scenario is written.")
    n_viol, n_mr = len(gate["violations"]), len(gate["manual_review"])
    if gate["passed"]:
        st.success(f"PASS — {label}: no threshold violations found "
                   f"({n_mr} manual-review item(s), non-blocking).")
    else:
        st.error(f"FAIL — {label}: {n_viol} violation(s). Scenario generation is blocked "
                 f"until the building passes.")
    if gate["violations"]:
        st.markdown("**Violations (block generation)**")
        st.dataframe(
            [{"severity": v["severity"], "rule": v["regulation_id"],
              "element": f"{v['element_type']}/{v['element_name']}",
              "issue": v["issue"], "reference": v["reference"]} for v in gate["violations"]],
            width='stretch', hide_index=True)
    if gate["manual_review"]:
        with st.expander(f"Manual review — {n_mr} items (cannot be decided from the IFC; non-blocking)"):
            st.dataframe(
                [{"rule": v["regulation_id"],
                  "element": f"{v['element_type']}/{v['element_name']}",
                  "issue": v["issue"]} for v in gate["manual_review"]],
                width='stretch', hide_index=True)

gate = st.session_state.gate_result
obj = st.session_state.scenario_object

if gate is None:
    st.subheader("How it works")
    c1, c2, c3, c4 = st.columns(4)
    c1.info("**1. Parse-**\nExtract spaces, doors, stairs, exits, centroids and storeys from the IFC.")
    c2.info("**2. Regulation gate-**\nThe building is checked against the selected regulation and shown "
            "a clear PASS/FAIL. Generation of scenarios is blocked unless it passes.")
    c3.info("**3. Generate scenarios-**\n This generates the possible evacuation scenarios based on the ifc building file.")
    c4.info("**4. Scenario Results exported in JSON-**\n The generated scenario are finally shown in JSON so that those results "
            "can be used to later take evacuation scenario decision for any residential buildings.")
    st.stop()

render_gate(gate, jurisdiction_label)
st.divider()

if not gate["passed"]:
    st.warning("Fix the flagged violations (or select a different regulation) and re-check. "
               "The AI will not generate scenarios for a building that fails the regulation.")
    st.stop()

if obj is None:
    st.subheader("Step 2 — Generate scenarios")
    st.info("Building passes the regulation. Use **Generate scenarios** in the sidebar to run the "
            "single AI generation call.")
    st.stop()

building = obj["building"]
validation = obj.get("validation", {})
exit_labels = exit_names(obj.get("exits", []))
scenarios = obj.get("scenarios", [])
not_assessed = obj.get("not_assessed", [])


# ================================================================================================
# Step 2 — the building the scenarios are written about
# ================================================================================================

section(f"Step 2 — Building: {building.get('project')}",
        "Everything below is computed from the IFC geometry, not chosen by the AI.")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Storeys", building.get("storeys"))
m2.metric("Floor area (m²)", building.get("total_floor_area_m2"))
m3.metric("Final exits", len(obj.get("exits", [])))
m4.metric("Spaces", len(obj.get("spaces", [])))
m5.metric("Computed occupant load", building.get("total_occupant_load"))

with st.container(border=True):
    st.markdown("**Quality checks**")
    st.caption("Run over the generated object before it is exported — nothing here is the AI marking "
               "its own homework.")

    inv = validation.get("invariants_checked", {})
    q1, q2, q3 = st.columns(3)
    with q1:
        if validation.get("schema_valid"):
            st.success("Schema valid")
        else:
            st.error("Schema invalid")
    with q2:
        if all(v for v in inv.values() if v is not None):
            st.success("Invariants passed")
        else:
            failed = [k for k, v in inv.items() if v is False]
            st.warning(f"Invariants to review: {', '.join(failed)}")
    with q3:
        fc = validation.get("number_factcheck")
        if fc == "passed":
            st.success("Number fact-check passed")
        else:
            st.warning(f"Fact-check: {len(validation.get('ungrounded_numbers', []))} number(s) "
                       f"to review")

    sim_issues = validation.get("simulation_parameter_issues", [])
    place_issues = validation.get("placement_issues", [])
    state_issues = validation.get("occupancy_state_issues", [])
    fire_issues = validation.get("fire_condition_issues", [])
    all_issues = sim_issues + place_issues + state_issues + fire_issues
    if all_issues:
        with st.expander(f"Issues to review before running the study — {len(sim_issues)} parameter, "
                         f"{len(place_issues)} placement, {len(state_issues)} occupancy, "
                         f"{len(fire_issues)} fire", expanded=True):
            st.dataframe(all_issues, width='stretch', hide_index=True)
    else:
        st.caption("No outstanding issues on the generated scenarios.")

ref_exits, ref_gaps = st.tabs([f"Final exits ({len(obj.get('exits', []))})",
                               f"Not assessed ({len(not_assessed)})"])
with ref_exits:
    st.caption("Exits are named Exit 1, Exit 2 … across the plan. The IFC name and GlobalId are here "
               "so a name can be traced back to the door in the model.")
    exit_by_id = {e["id"]: e for e in obj.get("exits", [])}
    st.dataframe(
        [{"exit": name, "width_m": exit_by_id[exit_id].get("width_m"),
          "IFC name": exit_by_id[exit_id].get("name"), "IFC GlobalId": exit_id}
         for exit_id, name in exit_labels.items()],
        width='stretch', hide_index=True)
with ref_gaps:
    st.caption("Missing data or no egress path — surfaced explicitly rather than assumed safe.")
    if not_assessed:
        st.dataframe(not_assessed, width='stretch', hide_index=True)
    else:
        st.success("Nothing outstanding.")

st.divider()


# ================================================================================================
# Step 3 — the scenarios
# ================================================================================================

section("Step 3 — Evacuation scenarios",
        f"{len(scenarios)} scenarios generated for this building. Select one to inspect it.")

options = {f"{s['id']} · {str(s.get('type', '')).replace('_', ' ')}": s for s in scenarios}
choice = st.segmented_control("Scenario", list(options.keys()),
                              default=next(iter(options), None), label_visibility="collapsed")
scn = options[choice] if choice in options else next(iter(options.values()))

scn = name_exit_ids(scn, exit_labels)

objective = scn.get("scenario_objective", {})
conditions = objective.get("conditions", {})
routes_block = scn.get("evacuation_routes", {})
occupancy = scn.get("occupancy") or {}
discounted = [str(named(e, exit_labels)) for e in conditions.get("exits_discounted", [])]
available = [str(named(e, exit_labels)) for e in routes_block.get("exits_available", [])]
estimate = evacuation_time(scn)

# --- headline card: what this scenario is, in one glance ---
with st.container(border=True):
    st.markdown(f"### {scn.get('title')}")
    if occupancy.get("occupancy_state"):
        st.caption(f"Occupancy state — {occupancy['occupancy_state']}")
    if objective.get("purpose"):
        st.write(objective["purpose"])

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Occupants to evacuate", occupancy.get("occupants_total"))
    s2.metric("Exits available", len(available))
    s3.metric("Exits discounted", len(discounted))
    s4.metric("Est. time to clear (s)",
              estimate.get("estimated_total_s") if estimate.get("estimated_total_s") is not None
              else "—")

    if discounted:
        st.warning(f"Exit(s) discounted in this variant: {', '.join(discounted)}")

tab_story, tab_people, tab_exits, tab_fire, tab_timing = st.tabs(
    ["Overview", "People", "Exits & risks", "Fire", "Movement & timing"])

# --- Overview: the plain-English account and why this scenario exists ---
with tab_story:
    st.markdown("**Narrative**")
    st.info(scn.get("narrative", ""))
    o1, o2 = st.columns(2)
    with o1:
        field("Why the AI chose this scenario", scn.get("ai_explanation"))
    with o2:
        field("Regulatory justification", scn.get("regulatory_justification"))

# --- People: who is in the building and what was assumed about them ---
with tab_people:
    p1, p2 = st.columns(2)
    with p1:
        bullets("Occupant distribution", scn.get("occupant_distribution", []))
    with p2:
        bullets("Assumptions", scn.get("assumptions", []))

    if occupancy.get("unplaced_total"):
        st.warning(f"{occupancy['unplaced_total']} occupant(s) sit in "
                   f"{len(occupancy.get('unplaced_rooms', []))} room(s) with no traced egress path. "
                   f"They are reported, not moved into rooms that can escape and a simulation run "
                   f"from this record will be short by that many people until those rooms are "
                   f"resolved.")
    if occupancy.get("unallocated_why"):
        st.warning(occupancy["unallocated_why"])

# --- Exits & risks: where people may go, and what makes it hard ---
with tab_exits:
    e1, e2 = st.columns(2)
    with e1:
        bullets("Exits available", available, empty="No exit is available in this variant.")
    with e2:
        bullets("Exits discounted", discounted, empty="No exit is discounted in this variant.")

    st.markdown("**Restricted areas** — where occupants may not go in this variant.")
    if routes_block.get("restricted_areas"):
        st.dataframe(routes_block["restricted_areas"], width='stretch', hide_index=True)
    else:
        st.caption("Nothing restricted in this variant.")

    r1, r2 = st.columns(2)
    with r1:
        bullets("Bottlenecks", scn.get("bottlenecks", []))
    with r2:
        bullets("Risks", scn.get("risks", []))

# --- Fire: the one part neither the IFC nor the computed facts can supply ---
with tab_fire:
    st.caption("What the IFC cannot carry: what burns, where, what it takes out, and when people "
               "find out.")
    fire = fire_related_conditions(scn)
    if any(fire.get(k) for k in ("fire_origin", "detection_and_alarm", "smoke_conditions")):
        f1, f2, f3 = st.columns(3)
        f1.metric("Origin storey", fire.get("fire_origin_storey") or "—")
        f2.metric("Exits lost to the fire", len(fire.get("affected_exits") or []))
        f3.metric("Routes degraded", len(fire.get("affected_routes") or []))

        field("Fire origin", fire.get("fire_origin"))
        if fire.get("affected_exits"):
            st.warning(f"Made unusable by this fire: {', '.join(fire['affected_exits'])}")
        if fire.get("affected_routes"):
            field("Routes degraded", ", ".join(fire["affected_routes"]))
        field("Detection and alarm", fire.get("detection_and_alarm"))
        field("Smoke conditions", fire.get("smoke_conditions"))
        if fire.get("basis"):
            st.caption(f"Basis: {fire['basis']}")
    else:
        st.caption("No fire-related conditions were generated for this scenario.")

# --- Movement & timing: how long before people move, and how fast once they do ---
with tab_timing:
    st.markdown("**Pre-movement time**")
    st.caption("Everything between the fire starting and people starting to move: the fire is "
               "detected, the alarm is raised, occupants recognise it, then they finish what they "
               "are doing.")
    pre = pre_movement_time(scn)
    t1, t2, t3 = st.columns(3)
    with t1:
        field("Detection", pre.get("detection"))
    with t2:
        field("Alarm", pre.get("alarm"))
    with t3:
        field("Recognition", pre.get("recognition"))

    delay = pre.get("response_delay") or {}
    st.caption("Response delay — the pre-travel activity time once the alarm has been recognised.")
    d1, d2, d3 = st.columns(3)
    d1.metric("Distribution", delay.get("distribution") or "—")
    d2.metric("Mean (s)", delay.get("mean_s") if delay.get("mean_s") is not None else "—")
    d3.metric("Spread, std dev (s)", delay.get("sd_s") if delay.get("sd_s") is not None else "—")
    if delay.get("basis"):
        st.caption(f"Basis: {delay['basis']}")

    st.divider()

    st.markdown("**Movement characteristic**")
    st.caption("How the population moves once it starts.")
    movement = movement_characteristic(scn)
    st.caption(f"Movement model: {movement.get('movement_model') or '—'}")
    if movement.get("profiles"):
        st.dataframe(movement["profiles"], width='stretch', hide_index=True)
    else:
        st.caption("No occupant profiles given for this scenario.")

    if estimate.get("estimated_total_s") is not None:
        st.divider()
        st.markdown("**Estimated evacuation time**")
        st.metric("Estimated time to clear the building (s)", estimate["estimated_total_s"])
        st.caption(f"An engineering estimate worked out ahead of the run, not a simulation result — "
                   f"the egress run this record feeds is what determines the real figure. "
                   f"Basis: {estimate.get('basis') or '—'}")

st.divider()


# ================================================================================================
# Step 4 — the deliverable
# ================================================================================================

section("Step 4 — Export",
        "One JSON record per scenario, carrying every input an egress study needs.")

records = build_records(obj)
records_json = export_records(obj)

st.download_button(f"Download JSON — {len(records)} scenario record(s)", data=records_json,
                   file_name="evacuation_scenario_records.json", mime="application/json",
                   type="primary", width='stretch')

with st.expander("Preview the records JSON before downloading", expanded=False):
    st.json(records)
