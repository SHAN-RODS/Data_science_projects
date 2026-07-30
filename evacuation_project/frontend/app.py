import os
import sys
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core_backend.ifc_parser import parser_summary
from core_backend.uk_regulation_checking import regulation_gate
from core_backend.scenario_generation_llm import build_full_scenario
from core_backend.validation import validate
from core_backend.export_results import export_json, export_records, build_records
from core_backend.llm import select_llm

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
    _, model_label = select_llm()
except Exception:
    model_label = "no LLM configured"

# header
st.title("NLP Assisted Evacuation Scenario Generator")
st.caption(
    "Upload an IFC/BIM model. The building is first checked against the selected regulation (pass/fail) "
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
        if st.button("Validate", type="primary", use_container_width=True):
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
        if st.button("Generate scenarios", type="primary", use_container_width=True):
            with st.spinner("Classifying spaces with AI · computing occupancy & travel distances · "
                            "generating the scenarios in one AI call…"):
                obj = validate(build_full_scenario(st.session_state.ifc_path,
                                                   jurisdiction=jurisdiction, gate=gate))
            st.session_state.scenario_object = obj
            st.rerun()

    if st.session_state.gate_result is not None or st.session_state.scenario_object is not None:
        st.divider()
        if st.button("Start over / upload a new file", use_container_width=True):
            for key in ("gate_result", "gate_context", "ifc_path", "scenario_object"):
                st.session_state[key] = None
            st.rerun()


def _per_storey_occupants(spaces):
    rollup = {}
    for s in spaces:
        storey = s.get("storey") or "Unknown"
        rollup[storey] = rollup.get(storey, 0) + (s.get("occupant_load") or 0)
    return rollup


def render_gate(gate, label):
    st.subheader("Step 1 — Regulation check")
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
            use_container_width=True, hide_index=True)
    if gate["manual_review"]:
        with st.expander(f"Manual review — {n_mr} item(s) (cannot be decided from the IFC; non-blocking)"):
            st.dataframe(
                [{"rule": v["regulation_id"],
                  "element": f"{v['element_type']}/{v['element_name']}",
                  "issue": v["issue"]} for v in gate["manual_review"]],
                use_container_width=True, hide_index=True)


gate = st.session_state.gate_result
obj = st.session_state.scenario_object

# landing / instructions
if gate is None:
    st.subheader("How it works")
    c1, c2, c3, c4 = st.columns(4)
    c1.info("**1. Parse-**\nExtract spaces, doors, stairs, exits, centroids and storeys from the IFC.")
    c2.info("**2. Regulation gate-**\nThe building is checked against your selected regulation and shown "
            "a clear PASS/FAIL. Generation is blocked unless it passes.")
    c3.info("**3. Classifier (AI)-**\nA dictionary resolves clear room labels and the LLM handles the "
            "messy, multilingual ones.")
    c4.info("**4. Compute, then generate (AI)-**\nOccupant loads and travel distances are computed from "
            "the geometry. One grounded AI call then decides the scenario set (which exits open/closed) "
            "and writes routes, bottlenecks, risks and a narrative from those numbers.")
    st.stop()

# Step 1 verdict — always shown once a check has run
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

# ---- generated scenario view ----
building = obj["building"]
validation = obj.get("validation", {})

st.subheader(f"Building — {building.get('project')}")

inv = validation.get("invariants_checked", {})
vb1, vb2, vb3 = st.columns(3)
with vb1:
    if validation.get("schema_valid"):
        st.success("Schema valid")
    else:
        st.error("Schema invalid")
with vb2:
    if all(v for v in inv.values() if v is not None):
        st.success("Invariants passed")
    else:
        failed = [k for k, v in inv.items() if v is False]
        st.warning(f"Invariants to review: {', '.join(failed)}")
with vb3:
    fc = validation.get("number_factcheck")
    if fc == "passed":
        st.success("Number fact-check passed")
    else:
        st.warning(f"Fact-check: {len(validation.get('ungrounded_numbers', []))} number(s) to review")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Storeys", building.get("storeys"))
m2.metric("Total occupant load", building.get("total_occupant_load"))
m3.metric("Total floor area (m²)", building.get("total_floor_area_m2"))
m4.metric("Final exits", len(obj.get("exits", [])))
m5.metric("Spaces", len(obj.get("spaces", [])))

rollup = _per_storey_occupants(obj["spaces"])
if any(rollup.values()):
    st.caption("Occupant load by storey (computed from code floor-space factors)")
    st.bar_chart(pd.DataFrame({"occupants": rollup}))

not_assessed = obj.get("not_assessed", [])
with st.expander(f"Not assessed — {len(not_assessed)} item(s) (never silently passed)",
                 expanded=len(not_assessed) > 0):
    st.caption("Missing data or no egress path — surfaced explicitly rather than assumed safe.")
    if not_assessed:
        st.dataframe(not_assessed, use_container_width=True, hide_index=True)
    else:
        st.success("Nothing outstanding.")

st.divider()

st.subheader("Evacuation scenarios (AI-proposed)")
scenarios = obj.get("scenarios", [])
labels = {f"{s['id']} — {s.get('type')}": s for s in scenarios}
choice = st.radio("Select a scenario variant:", list(labels.keys()), horizontal=True)
scn = labels[choice]

st.markdown(f"#### {scn.get('title')}")
cond = scn.get("conditions", {})
cc1, cc2, cc3, cc4 = st.columns(4)
cc1.metric("Occupancy state", cond.get("occupancy_state"))
cc2.metric("Occupants to evacuate", cond.get("occupants_total"))
cc3.metric("Exits available", len(cond.get("exits_available", [])))
cc4.metric("Exits discounted", len(cond.get("exits_discounted", [])))
if cond.get("exits_discounted"):
    st.warning(f"Exit(s) discounted in this variant: {', '.join(cond['exits_discounted'])}")

st.markdown("**Narrative**")
st.info(scn.get("narrative", ""))

if scn.get("ai_explanation"):
    st.markdown("**AI explanation**")
    st.write(scn["ai_explanation"])
if scn.get("regulatory_justification"):
    st.markdown("**Regulatory justification**")
    st.write(scn["regulatory_justification"])

d1, d2 = st.columns(2)
with d1:
    st.markdown("**Occupant distribution**")
    for line in scn.get("occupant_distribution", []):
        st.write(f"- {line}")
    st.markdown("**Assumptions**")
    for line in scn.get("assumptions", []):
        st.write(f"- {line}")
with d2:
    st.markdown("**Bottlenecks**")
    for line in scn.get("bottlenecks", []):
        st.write(f"- {line}")
    st.markdown("**Risks**")
    for line in scn.get("risks", []):
        st.write(f"- {line}")

st.markdown("**Routes (from → via → exit)**")
if scn.get("routes"):
    st.dataframe(scn["routes"], use_container_width=True, hide_index=True)

st.divider()

with st.expander(f"Spaces ({len(obj['spaces'])}) — occupant load, nearest exit and travel distance are "
                 f"computed; use-type is AI-classified"):
    st.dataframe(obj["spaces"], use_container_width=True, hide_index=True)

st.divider()

st.subheader("Export")
st.caption("The deliverable is a record per scenario: unique_id, description, relevant_ifc_element, "
           "regulatory_justification, ai_explanation and the scenario itself.")
records_json = export_records(obj)
with st.expander("Preview records JSON before downloading", expanded=True):
    st.json(build_records(obj))
st.download_button("Download JSON", data=records_json,
                   file_name="evacuation_scenario_records.json", mime="application/json",
                   use_container_width=True)
with st.expander("Full building object (reference — spaces, exits, gate, provenance)"):
    st.json(obj)
