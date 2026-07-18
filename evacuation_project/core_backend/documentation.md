Steps followed for the project(june 26):


1. Ifc parsing extraction- only important elements related to the evacuation scenarios (done but needs check)
Important elements extracted required for evacuation scenarios:
ifcspace, ifcdoor, ifcslabs, ifcwalls, ifctransportelement, ifcrelspaceboundary, ifcwindows, escalators, elevators, ifcstairs, 

2. Hardcoded rules JSON regulation- based on england, wales, ireland and scotland documents

3. Pre- check verifying the uk regulations of england, wales, northern ireland and scotland with the IFC elements extracted (done but needs check)

4. Scenario generation using LLMs with API KEY(NLP)  (still doing it)
- First test with free models during testing the system 
- Later use paid API key for testing the system
- code follows langchain pattern


implementation schema of using any LLM model: (19th june 26)
- call the Mistral API 
- define how the scenarios should be present
- finally calling and setting the token and temperature limit to avoid loosing api model limit.



5. export the results into a JSON structure(directly in streamlit)

6. Build the User interface using Streamlit(final)
- backup - using streamlit(connection and web design) 

7. Run and test the system if it gives the scenarios or not and fix the codes wherever possible(2nd week of july)

Target- finish the codes by 12th july(both backend and frontend)


## Scenario schema (pivot — whole-building scenario generator)

The project is being pivoted (see `Plan.md`) from a per-violation compliance checker to a
whole-building evacuation *scenario* generator. The output is a single structured object per
uploaded IFC (not a flat list of violations), with >=2 variants (base case + one-exit-discounted).

The machine-checked specification of that object lives in `core_backend/scenario_schema.py`
(JSON Schema + pydantic models) once built. Field ownership: `use_type*` and the scenario
reasoning/narrative fields are AI-produced; everything else is deterministic computation. Every
derived value carries its source IFC GUID or a stated method, and missing data is surfaced in an
explicit `not_assessed` list rather than being silently passed.

