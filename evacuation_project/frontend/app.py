# app.py — EvacAI: AI-Assisted Evacuation Scenario Suggestion
#
# Run with:  streamlit run frontend/app.py
#
# Streamlit reruns this whole file every time the user clicks something
# or uploads a file. session_state is how results survive between reruns.

import os
import sys

# core_backend is a sibling folder of frontend/, so the project root needs
# to be on sys.path before "import core_backend.xxx" will work regardless
# of the working directory Streamlit was launched from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
from dotenv import load_dotenv

from core_backend.ifc_parser import get_summary
from core_backend.uk_regulation_checking import check_all_rules
from core_backend.scenario_generation_llm import generate_scenarios, MODEL_LABEL
from core_backend.export_results import export_as_json, export_as_xml

load_dotenv()   # reads provider API keys from .env

os.makedirs("uploads", exist_ok=True)

JURISDICTIONS = {
    "England — Approved Document B Volume 1":        "england",
    "Wales — Approved Document B Volume 2":          "wales",
    "Northern Ireland — Technical Booklet E (2012)":  "northern_ireland",
    "Scotland — Technical Handbooks 2022":            "scotland",
}


# ── Page config ─────────────────────────────────────────────────────────
# Must be the very first Streamlit call in the file

st.set_page_config(
    page_title="EvacAI — Evacuation Scenario Generator",
    page_icon="🏢",
    layout="wide"
)


# ── Session state setup ─────────────────────────────────────────────────

if "scenarios" not in st.session_state:
    st.session_state.scenarios = []

if "project_name" not in st.session_state:
    st.session_state.project_name = ""

if "stats" not in st.session_state:
    st.session_state.stats = {}

if "analysed" not in st.session_state:
    st.session_state.analysed = False


# ── Header ───────────────────────────────────────────────────────────────

st.title("🏢 EvacAI — Evacuation Scenario Generator")
st.caption("AI-assisted evacuation scenario suggestion from a BIM (IFC) model, guided by UK fire safety regulations")
st.caption(f"AI provider in use: **{MODEL_LABEL}** (set by whichever API key is present in `.env` — Anthropic checked first, Mistral as fallback)")
st.divider()


# ── Sidebar ──────────────────────────────────────────────────────────────
# Upload + controls live in the sidebar so results have the full page width

with st.sidebar:

    st.header("1. Choose jurisdiction")
    jurisdiction_label = st.selectbox(
        "UK regulation set to check against:",
        options=list(JURISDICTIONS.keys())
    )
    jurisdiction = JURISDICTIONS[jurisdiction_label]

    st.divider()

    st.header("2. Upload IFC Model")
    uploaded_file = st.file_uploader(
        label="Select a .ifc file",
        type=["ifc"],
        help="Industry Foundation Classes format from Revit, ArchiCAD, etc."
    )

    if uploaded_file is not None:
        st.success(f"File ready: {uploaded_file.name}")

        if st.button("Analyse Model", type="primary", use_container_width=True):
            save_path = os.path.join("uploads", uploaded_file.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            with st.spinner("Running analysis..."):
                progress = st.progress(0, text="Parsing IFC file...")

                summary = get_summary(save_path)
                progress.progress(25, text=f"Checking {jurisdiction_label} regulations...")

                flags = check_all_rules(summary, jurisdiction=jurisdiction)
                progress.progress(50, text=f"Generating AI explanations for {len(flags)} issue(s)...")

                scenarios = generate_scenarios(flags, summary)
                progress.progress(100, text="Done!")

            st.session_state.scenarios = scenarios
            st.session_state.project_name = summary["project"]
            st.session_state.analysed = True
            st.session_state.stats = {
                "Spaces": len(summary["spaces"]),
                "Doors":  len(summary["doors"]),
                "Stairs": len(summary["stairs"]),
                "Exits":  len(summary["emergency_exits"])
            }

            st.rerun()

    if st.session_state.analysed:
        st.divider()
        st.subheader("Model Summary")
        for label, value in st.session_state.stats.items():
            st.metric(label=label, value=value)

        st.divider()
        if st.button("Upload New File", use_container_width=True):
            st.session_state.scenarios = []
            st.session_state.project_name = ""
            st.session_state.stats = {}
            st.session_state.analysed = False
            st.rerun()


# ── Main area: instructions or results ───────────────────────────────────

if not st.session_state.analysed:

    st.subheader("How it works")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.info("**Step 1**\nChoose a jurisdiction and upload a `.ifc` building model")
    with col2:
        st.info("**Step 2**\nThe system extracts doors, spaces, stairs, corridors, windows and exits")
    with col3:
        st.info("**Step 3**\nEach element is checked against the selected UK regulations")
    with col4:
        st.info(f"**Step 4**\n{MODEL_LABEL} explains each confirmed violation; items needing manual review skip the AI call")

else:

    scenarios = st.session_state.scenarios
    st.subheader(f"Results — {st.session_state.project_name}")

    if len(scenarios) == 0:
        st.success("No issues found. All checked elements appear compliant with the applied regulations.")

    else:
        # Manual-review items are not confirmed violations — IFC data alone
        # couldn't settle the question, so they're kept in their own section
        # rather than mixed into the severity-classified violations below.
        # No LLM call was made for these (see scenario_generation_llm.py),
        # so their explanation text is regulation guidance, not an "AI
        # explanation of a violation".
        violations = [s for s in scenarios if not s.get("requires_manual_review")]
        manual_review = [s for s in scenarios if s.get("requires_manual_review")]

        # ── Confirmed violations ──────────────────────────────────────

        st.markdown("### Confirmed Violations")

        if len(violations) == 0:
            st.success("No computed violations — all checked attributes are within the applied regulation limits.")
        else:
            # Severity vocabulary differs slightly by jurisdiction (Wales/Scotland
            # add "major"/"low" alongside England's critical/high/medium), so the
            # summary metrics and filter options are built from whatever severities
            # actually appear rather than a hardcoded England-only list.
            severity_counts = {}
            for s in violations:
                severity_counts[s["severity"]] = severity_counts.get(s["severity"], 0) + 1

            metric_cols = st.columns(len(severity_counts) + 1)
            metric_cols[0].metric("Total violations", len(violations))
            for col, (severity, count) in zip(metric_cols[1:], severity_counts.items()):
                col.metric(severity.capitalize(), count)

            st.divider()

            # ── Filter controls ────────────────────────────────────────

            fc1, fc2, fc3 = st.columns([2, 2, 3])

            with fc1:
                severity_filter = st.selectbox(
                    "Filter by severity",
                    options=["All"] + sorted(severity_counts.keys(), key=str.capitalize)
                )

            with fc2:
                element_types = sorted({s["ifc_element_type"] for s in violations})
                type_filter = st.selectbox(
                    "Filter by element type",
                    options=["All"] + element_types,
                    key="violation_type_filter"
                )

            filtered = violations.copy()
            if severity_filter != "All":
                filtered = [s for s in filtered if s["severity"] == severity_filter]
            if type_filter != "All":
                filtered = [s for s in filtered if s["ifc_element_type"] == type_filter]

            st.caption(f"Showing {len(filtered)} of {len(violations)} confirmed violations")
            st.divider()

            severity_icons = {
                "critical": "🔴", "high": "🟠", "major": "🟠",
                "medium": "🔵", "low": "🟢"
            }

            for scenario in filtered:
                sid = scenario["id"]
                icon = severity_icons.get(scenario["severity"], "⚪")
                default_open = scenario["severity"] == "critical"

                with st.expander(
                    label=f"{icon} {sid} — {scenario['description'][:90]}...",
                    expanded=default_open
                ):
                    selected_key = f"sel_{sid}"
                    if selected_key not in st.session_state:
                        st.session_state[selected_key] = True

                    st.checkbox(label="Include in export", key=selected_key)
                    st.divider()

                    d1, d2, d3 = st.columns(3)
                    with d1:
                        st.markdown("**IFC Element**")
                        st.code(f"{scenario['ifc_element_type']} — {scenario['ifc_element_name']}", language=None)
                        st.caption(f"GUID: {scenario['ifc_element_guid']}")

                    with d2:
                        st.markdown("**Attribute Extracted**")
                        st.code(scenario["ifc_attribute"], language=None)

                    with d3:
                        st.markdown("**Regulation**")
                        st.code(scenario["regulation_id"], language=None)
                        st.caption(scenario["regulation_name"])

                    st.markdown("**Regulatory Justification**")
                    st.success(
                        f"📋 **{scenario['regulation_reference']}**\n\n"
                        f"{scenario['regulation_description']}"
                    )

                    st.markdown(f"**AI Explanation — {MODEL_LABEL}**")
                    st.info(f"🤖 {scenario['ai_explanation']}")

        st.divider()

        # ── Requires manual review ────────────────────────────────────

        st.markdown("### 🔍 Requires Manual Review")
        st.caption(
            "These aren't confirmed violations — IFC data alone can't settle "
            "them. No AI explanation was generated for these (no API call "
            "made); the text below is the regulation's own guidance."
        )

        if len(manual_review) == 0:
            st.caption("None for this jurisdiction/model.")
        else:
            mc1, mc2 = st.columns([2, 3])
            with mc1:
                mr_element_types = sorted({s["ifc_element_type"] for s in manual_review})
                mr_type_filter = st.selectbox(
                    "Filter by element type",
                    options=["All"] + mr_element_types,
                    key="manual_review_type_filter"
                )

            mr_filtered = manual_review.copy()
            if mr_type_filter != "All":
                mr_filtered = [s for s in mr_filtered if s["ifc_element_type"] == mr_type_filter]

            st.caption(f"Showing {len(mr_filtered)} of {len(manual_review)} manual review item(s)")
            st.divider()

            for scenario in mr_filtered:
                sid = scenario["id"]

                with st.expander(
                    label=f"🔍 {sid} — {scenario['description'][:90]}...",
                    expanded=False
                ):
                    selected_key = f"sel_{sid}"
                    if selected_key not in st.session_state:
                        st.session_state[selected_key] = True

                    st.checkbox(label="Include in export", key=selected_key)
                    st.divider()

                    d1, d2, d3 = st.columns(3)
                    with d1:
                        st.markdown("**IFC Element**")
                        st.code(f"{scenario['ifc_element_type']} — {scenario['ifc_element_name']}", language=None)
                        st.caption(f"GUID: {scenario['ifc_element_guid']}")

                    with d2:
                        st.markdown("**Attribute Extracted**")
                        st.code(scenario["ifc_attribute"], language=None)

                    with d3:
                        st.markdown("**Regulation**")
                        st.code(scenario["regulation_id"], language=None)
                        st.caption(scenario["regulation_name"])

                    st.markdown("**Regulatory Justification**")
                    st.success(
                        f"📋 **{scenario['regulation_reference']}**\n\n"
                        f"{scenario['regulation_description']}"
                    )

                    st.markdown("**Why this needs review**")
                    st.warning(f"🔍 {scenario['ai_explanation']}")

        st.divider()

        # ── Export section ───────────────────────────────────────────

        st.subheader("Export Selected Scenarios")

        selected_scenarios = [
            s for s in scenarios
            if st.session_state.get(f"sel_{s['id']}", True)
        ]
        st.caption(f"{len(selected_scenarios)} scenario(s) selected for export")

        ex1, ex2 = st.columns(2)

        with ex1:
            st.download_button(
                label="⬇ Download JSON",
                data=export_as_json(selected_scenarios, ai_model_used=MODEL_LABEL),
                file_name="evacuation_scenarios.json",
                mime="application/json",
                use_container_width=True,
                disabled=len(selected_scenarios) == 0
            )

        with ex2:
            st.download_button(
                label="⬇ Download XML",
                data=export_as_xml(selected_scenarios, ai_model_used=MODEL_LABEL),
                file_name="evacuation_scenarios.xml",
                mime="application/xml",
                use_container_width=True,
                disabled=len(selected_scenarios) == 0
            )
