import os
import sys
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core_backend.scenario_generation_llm import build_full_scenario
from core_backend.validation import validate
from core_backend.export_results import export_json, export_xml
from core_backend.llm import select_llm

load_dotenv()
os.makedirs("uploads", exist_ok=True)

jurisdictions = {
    "England Approved Document B Volume 1": "england",
    "Wales Approved Document B Volume 1": "wales",
    "Northern Ireland Technical Booklet E-2012": "northern_ireland",
    "Scotland Technical Handbooks 2022": "scotland",
}

st.set_page_config(page_title="AI Evacuation Scenario Generator", layout="wide")

if "scenario_object" not in st.session_state:
    st.session_state.scenario_object = None

try:
    _, model_label = select_llm()
except Exception:
    model_label = "no LLM configured"

# --- header ------------------------------------------------------------------------------------
st.title("AI Evacuation Scenario Generator")
st.caption(
    "Generates a **whole-building evacuation scenario** (base case + one-exit-discounted) from an "
    "uploaded IFC/BIM model, grounded in the real building data. It is **not** a simulation and "
    "**never** issues a compliance verdict — it reports measured values, limits, and flags, and "
    "surfaces anything it could not assess."
)
st.caption(f"Scenario reasoning model: {model_label}")
st.divider()

# --- sidebar: inputs ---------------------------------------------------------------------------
with st.sidebar:
    st.header("1 · Regulation reference")
    jurisdiction_label = st.selectbox("Cite thresholds from:", list(jurisdictions.keys()))
    jurisdiction = jurisdictions[jurisdiction_label]

    st.divider()
    st.header("2 · Upload IFC model")
    uploaded_file = st.file_uploader("Select an .ifc file", type=["ifc"],
                                     help="IFC export from Revit or ArchiCAD.")

    if uploaded_file is not None:
        st.success(f"Ready: {uploaded_file.name}")
        if st.button("Generate scenarios", type="primary", use_container_width=True):
            save_path = os.path.join("uploads", uploaded_file.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            with st.spinner("Parsing IFC · classifying spaces (AI) · grounding · generating scenarios (AI)…"):
                obj = validate(build_full_scenario(save_path, jurisdiction=jurisdiction))
            st.session_state.scenario_object = obj
            st.rerun()

    if st.session_state.scenario_object is not None:
        st.divider()
        if st.button("Upload a new file", use_container_width=True):
            st.session_state.scenario_object = None
            st.rerun()


def _per_storey_occupants(spaces):
    rollup = {}
    for s in spaces:
        storey = s.get("storey") or "Unknown"
        rollup[storey] = rollup.get(storey, 0) + (s.get("occupant_load") or 0)
    return rollup


obj = st.session_state.scenario_object

# --- instructions (no result yet) --------------------------------------------------------------
if obj is None:
    st.subheader("How it works")
    c1, c2, c3, c4 = st.columns(4)
    c1.info("**1 · Parse**\nExtract spaces, doors, stairs, exits, centroids and storeys from the IFC.")
    c2.info("**2 · Classify (AI)**\nA dictionary resolves clear room labels; the LLM handles the messy, "
            "multilingual ones.")
    c3.info("**3 · Ground**\nCompute occupant load, connectivity, nearest exit and approximate travel "
            "distance — deterministically.")
    c4.info("**4 · Generate (AI)**\nThe LLM composes routes, bottlenecks, risks and a narrative for "
            "each variant, grounded in those numbers.")
    st.stop()

# --- results: whole-building view --------------------------------------------------------------
building = obj["building"]
validation = obj.get("validation", {})

st.subheader(f"Building — {building.get('project')}")

# validation status
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

# building metrics
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Storeys", building.get("storeys"))
m2.metric("Total occupant load", building.get("total_occupant_load"))
m3.metric("Total floor area (m²)", building.get("total_floor_area_m2"))
m4.metric("Final exits", len(obj.get("exits", [])))
m5.metric("Spaces", len(obj.get("spaces", [])))

rollup = _per_storey_occupants(obj["spaces"])
if any(rollup.values()):
    st.caption("Occupant load by storey")
    st.bar_chart(pd.DataFrame({"occupants": rollup}))

# not_assessed — front and centre
not_assessed = obj.get("not_assessed", [])
with st.expander(f"⚠ Not assessed — {len(not_assessed)} item(s) (never silently passed)",
                 expanded=len(not_assessed) > 0):
    st.caption("Missing data or no egress path — surfaced explicitly rather than assumed safe.")
    if not_assessed:
        st.dataframe(not_assessed, use_container_width=True, hide_index=True)
    else:
        st.success("Nothing outstanding.")

st.divider()

# scenario switcher
st.subheader("Evacuation scenarios")
scenarios = obj.get("scenarios", [])
labels = {f"{s['id']} — {s.get('type')}": s for s in scenarios}
choice = st.radio("Select a scenario variant:", list(labels.keys()), horizontal=True)
scn = labels[choice]

st.markdown(f"#### {scn.get('title')}")
cond = scn.get("conditions", {})
cc1, cc2, cc3 = st.columns(3)
cc1.metric("Occupants to evacuate", cond.get("occupants_total"))
cc2.metric("Exits available", len(cond.get("exits_available", [])))
cc3.metric("Exits discounted", len(cond.get("exits_discounted", [])))
if cond.get("exits_discounted"):
    st.warning(f"Exit(s) discounted in this variant: {', '.join(cond['exits_discounted'])}")

st.markdown("**Narrative**")
st.info(scn.get("narrative", ""))

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

# spaces table
with st.expander(f"Spaces ({len(obj['spaces'])}) — use-type, occupant load, nearest exit, travel distance"):
    st.dataframe(obj["spaces"], use_container_width=True, hide_index=True)

# regulation reference (building-level, secondary) — summary + only the flagged exceptions
reg_check = obj.get("regulation_check", {})
by_reg = reg_check.get("by_regulation", [])
flagged = reg_check.get("requires_manual_review", [])
with st.expander(f"Regulation reference — {jurisdiction_label} "
                 f"({len(by_reg)} rules checked, {len(flagged)} item(s) to review, non-verdict)"):
    st.caption("Measured value + applicable limit + flag. Reference only — not a compliance verdict.")
    if by_reg:
        st.markdown("**Per-regulation summary**")
        st.dataframe(by_reg, use_container_width=True, hide_index=True)
    if flagged:
        st.markdown("**Requires manual review**")
        st.dataframe(flagged, use_container_width=True, hide_index=True)
    elif by_reg:
        st.success("No elements flagged for manual review.")
    if not by_reg:
        st.caption("No measurable notes for this jurisdiction/model.")

st.divider()

# export
st.subheader("Export")
ex1, ex2 = st.columns(2)
with ex1:
    st.download_button("Download JSON", data=export_json(obj),
                       file_name="evacuation_scenario.json", mime="application/json",
                       use_container_width=True)
with ex2:
    st.download_button("Download XML", data=export_xml(obj),
                       file_name="evacuation_scenario.xml", mime="application/xml",
                       use_container_width=True)
