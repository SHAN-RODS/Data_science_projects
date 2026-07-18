# CLAUDE.md

Guidance for working in this repository. Read `Plan.md` for the full project plan and direction.

## Project overview

An **AI-driven evacuation *scenario* generator**. It reads an IFC/BIM building model and produces a
structured, whole-building **evacuation scenario** (JSON + plain-English description) describing how the
structure would be evacuated under defined conditions — occupant load per space, nearest exits, routes,
bottlenecks — in **≥2 variants** (base case + one-exit-discounted).

It **generates scenarios; it does not run simulations.** No evacuation-time / RSET / ASET / smoke
modelling, and it **never issues a compliance verdict** — it reports *measured value + limit + flag*.

> The project is mid-pivot: the current `core_backend/` emits one record **per code violation** and
> mislabels it a "scenario". We are moving to a **whole-building scenario** model. See `Plan.md` §9 for
> what to keep, reframe, rewrite, or add. Prefer the new direction for all new work.

## Tech stack

- **Python 3** (run from the project root).
- **IfcOpenShell** (`ifcopenshell`) — IFC/BIM parsing (`core_backend/ifc_parser.py`).
- **LangChain** — `langchain_anthropic` (`ChatAnthropic`), `langchain_mistralai` (`ChatMistralAI`),
  `langchain_core` (prompts, output parsers). Anthropic is preferred; Mistral is the fallback.
- **python-dotenv** — loads API keys / model / temperature from `.env`.
- **Streamlit** — frontend (`frontend/app.py`).
- Standard library for export (`json`, `xml.etree`).

There is **no `requirements.txt` yet** — add one when you touch dependencies. Known deps:
`ifcopenshell`, `langchain`, `langchain-anthropic`, `langchain-mistralai`, `langchain-core`,
`python-dotenv`, `streamlit`.

## Repository layout

```
evacuation_project/
├─ core_backend/
│  ├─ ifc_parser.py              # IfcOpenShell extraction → summary dict  (KEEP + EXTEND)
│  ├─ uk_regulation_checking.py  # threshold checks → flags               (REFRAME → annotation layer)
│  ├─ scenario_generation_llm.py # LLM per-violation text                 (REWRITE → scenario generator)
│  ├─ export_results.py          # JSON/XML export (per-violation schema) (REWRITE schema)
│  ├─ eng_reg.json / wales_reg.json / ireland_reg.json / scotland_reg.json  # thresholds (KEEP as reference)
│  └─ documentation.md           # legacy running notes
├─ frontend/
│  └─ app.py                     # Streamlit UI (per-defect cards)        (REWORK → whole-building view)
├─ Plan.md                       # full project plan (source of truth for direction)
└─ CLAUDE.md                     # this file
```

New modules to add (see `Plan.md` §5): `space_classifier.py`, `occupancy.py`, `egress.py`,
`scenario_schema.py`, `validation.py`, plus a `tests/` directory.

## How to run

Always run **from the project root** (imports are `from core_backend.<module> import ...`; the
Streamlit app inserts the project root onto `sys.path`).

```bash
# Frontend (main entry point)
streamlit run frontend/app.py

# Backend modules (each has a __main__ that accepts an IFC path arg)
python -m core_backend.ifc_parser "path/to/model.ifc"
python -m core_backend.uk_regulation_checking "path/to/model.ifc"
python -m core_backend.export_results "path/to/model.ifc"
```

Do **not** run modules as bare files (e.g. `python core_backend/uk_regulation_checking.py`) — the
`core_backend.` package imports will fail. Use `python -m core_backend.<module>` instead.

## Environment (.env — gitignored, never commit)

```
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-...
ANTHROPIC_TEMPERATURE=0
MISTRAL_API_KEY=...          # fallback provider (also read as `mistral`)
MISTRAL_MODEL=mistral-...
MISTRAL_TEMPERATURE=0
```

`select_llm()` in `scenario_generation_llm.py` picks Anthropic if `ANTHROPIC_API_KEY` is set, else
Mistral. Keep secrets out of source and out of commits.

## Data flow

`upload .ifc → parser_summary() → space classification (AI) → grounding (occupant load, connectivity,
nearest exit, approx distance) → scenario generator (AI) → validation & fact-check → JSON + narrative →
Streamlit view`. Regulation thresholds are a **reference** the generator may cite, not a gate.

## Coding conventions

- Match the existing style: small pure functions, snake_case, module-level `__main__` blocks that take
  an IFC path via `sys.argv[1]` with a sensible default.
- Keep the parser the **single source of geometry**; downstream stages consume its `summary` dict.
- Every derived value must carry the **source IFC GUID** (or a stated method) so it is traceable.
- Convert units explicitly (the parser already applies `unit_util.calculate_unit_scale`; keep metres
  as the internal unit). Never assume mm vs m.
- Do not hardcode absolute Windows paths in committed code (the legacy `__main__` defaults point at a
  personal desktop — replace with a repo-relative sample or a required CLI arg when you touch them).

## Domain rules (safety-critical — do not violate)

- **Never emit a compliance verdict.** Output is *measured value + applicable limit + flag*; borderline
  cases get `requires_manual_review`.
- **Never let missing data read as a pass.** If an attribute is absent, record it in `not_assessed` —
  do not silently `continue` (the legacy checker does; new code must not).
- **Label approximations.** Centroid/straight-line travel distances are `approx`, not compliance-grade;
  flag near-limit results for manual review.
- **Always produce ≥2 scenarios** (base + one-exit-discounted) — a single scenario is not a resilience test.

## AI / LLM usage rules

- **Justified AI only** — the test is *"would a formula do this better?"* If yes, use the formula.
  AI is for (1) space-use classification and (2) scenario generation/narrative. Numbers are computed,
  not invented.
- **Ground the model** — feed it the extracted facts; it must never source building data itself.
- **Temperature 0** for reproducibility.
- **Fact-check narratives** — any number/clause in generated prose must appear verbatim in the
  structured record; otherwise quarantine it. Prefer templates for critical statements.
- Keep the LLM **off the critical path for safety numbers** — it reasons over them, it does not decide them.
- Refer to the Anthropic/Claude model guidance and current model IDs when configuring providers.

## Testing & verification

- Add `tests/` with known-answer models: a rectangular room (path = hand measurement) and an L-shaped
  corridor (path bends past the Euclidean distance).
- Assert invariants each run: `travel_distance ≥ straight-line`; no space isolated from all exits;
  `total occupants ≤ summed exit capacity`; ≥2 scenarios present.
- Evaluate the classifier with accuracy / confusion matrix on a hand-labelled space set.

## Do / Don't

- ✅ Extend the parser, add new modules, keep the regulation JSONs as reference thresholds.
- ✅ Keep every output value traceable and every assumption explicit.
- ❌ Don't reintroduce the per-violation "scenario" model.
- ❌ Don't add simulation, RSET/ASET, or a formal graph library — out of scope (see `Plan.md` §12).
- ❌ Don't commit `.env`, `uploads/`, or personal absolute paths.
