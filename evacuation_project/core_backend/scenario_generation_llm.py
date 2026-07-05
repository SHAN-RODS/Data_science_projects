# Turns each regulation violation ("flag") into a reviewable scenario:
# the AI's job here is explanation, not decision-making — every scenario
# is grounded in a real IFC element and a real regulation, and the LLM
# only writes the professional fire-safety explanation for it.

import os
import uuid

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

load_dotenv()   # reads provider API keys from .env


def _select_llm():
    """
    Picks the LLM provider from whichever API key is available.
    Anthropic is the primary/preferred provider; Mistral is a fallback
    for testing when no Anthropic key is configured. Checked in that
    order regardless of which keys happen to be present in .env.

    Model name and temperature are configurable per provider via .env
    (ANTHROPIC_MODEL/ANTHROPIC_TEMPERATURE, MISTRAL_MODEL/MISTRAL_TEMPERATURE)
    so switching models doesn't require touching code — each provider has
    its own pair of settings, not a shared one.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        temperature = float(os.getenv("ANTHROPIC_TEMPERATURE", "0.3"))
        return (
            ChatAnthropic(
                model_name=model,
                temperature=temperature,
                max_tokens=400
            ),
            f"{model} (Anthropic API)"
        )

    # .env in this project uses a lowercase "mistral" key rather than the
    # conventional MISTRAL_API_KEY — both are accepted.
    mistral_key = os.getenv("MISTRAL_API_KEY") or os.getenv("mistral")
    if mistral_key:
        model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
        temperature = float(os.getenv("MISTRAL_TEMPERATURE", "0.3"))
        return (
            ChatMistralAI(
                model=model,
                api_key=mistral_key,
                temperature=temperature,
                max_tokens=400
            ),
            f"{model} (Mistral AI)"
        )

    raise RuntimeError(
        "No LLM API key found. Set ANTHROPIC_API_KEY (preferred) or "
        "MISTRAL_API_KEY / mistral in your .env file."
    )


llm, MODEL_LABEL = _select_llm()

prompt_template = PromptTemplate(
    input_variables=[
        "issue", "element_type", "element_name", "element_id", "attribute",
        "reg_id", "reg_name", "reg_description", "reg_reference", "severity",
        "total_spaces", "total_doors", "total_stairs", "total_windows", "total_exits"
    ],
    template="""You are a UK fire safety consultant reviewing a residential \
building model against UK Approved Document B (Fire Safety).

A regulation check has found the following violation:

VIOLATION:
{issue}

IFC BUILDING ELEMENT:
- Type      : {element_type}
- Name      : {element_name}
- IFC GUID  : {element_id}
- Attribute : {attribute}

REGULATION:
- ID          : {reg_id}
- Name        : {reg_name}
- Description : {reg_description}
- Reference   : {reg_reference}
- Severity    : {severity}

BUILDING CONTEXT:
- Total spaces   : {total_spaces}
- Total doors    : {total_doors}
- Total stairs   : {total_stairs}
- Total windows  : {total_windows}
- Emergency exits: {total_exits}

Write exactly 3 sentences as a professional UK fire safety explanation:
Sentence 1 — Which regulation is violated, the exact non-compliance, \
and the fire safety risk for occupants.
Sentence 2 — How this affects people evacuating from this residential building.
Sentence 3 — The remediation required, referencing the regulation reference \
and the specific measurement or requirement to be met.

Write only the 3 sentences. No introduction, no bullet points, no sign-off."""
)

output_parser = StrOutputParser()
chain = prompt_template | llm | output_parser


def generate_scenarios(flags, summary):
    """
    Converts every regulation flag into a scenario dict ready for the UI.

    flags   — list of dicts from uk_regulation_checking.check_all_rules()
    summary — dict from ifc_parser.get_summary()

    Returns a list of scenario dicts, one per flag.
    """
    return [build_one_scenario(flag, summary) for flag in flags]


def build_one_scenario(flag, summary):
    """
    Builds one complete scenario dict from one flag.

    Manual-review flags (flag["requires_manual_review"] is True) are not
    confirmed violations — IFC data alone couldn't settle the question, so
    there is nothing for the LLM to "explain a violation" about, and calling
    the API for one would just spend money to restate the regulation text
    with more words. Those get a direct explanation built from the
    regulation's own description instead, with no API call.
    """
    rule = flag["rule"]
    requires_manual_review = flag.get("requires_manual_review", False)

    if requires_manual_review:
        explanation = (
            f"{rule['description']} This cannot be confirmed automatically "
            f"from the IFC model — a qualified assessor should verify this "
            f"against {rule['doc_reference']}."
        )
    else:
        explanation = run_chain(flag, summary)

    return {
        "id": "SCN-" + str(uuid.uuid4())[:8].upper(),

        "description": flag["issue"],

        "ifc_element_type": flag["element_type"],
        "ifc_element_name": flag["element_name"],
        "ifc_element_guid": flag["element_id"],
        "ifc_attribute":    flag["attribute"],

        "regulation_id":          rule["unique_id"],
        "regulation_name":        rule["regulation_name"],
        "regulation_description": rule["description"],
        "regulation_reference":   rule["doc_reference"],
        "severity":               rule["severity_level"],

        "requires_manual_review": requires_manual_review,
        "ai_explanation": explanation,

        # UI checkbox state — True means selected for export by default
        "selected": True
    }


def run_chain(flag, summary):
    """Fills the prompt template with real data and calls the selected LLM via LangChain."""
    rule = flag["rule"]

    chain_input = {
        "issue":           flag["issue"],
        "element_type":    flag["element_type"],
        "element_name":    flag["element_name"],
        "element_id":      flag["element_id"],
        "attribute":       flag["attribute"],
        "reg_id":          rule["unique_id"],
        "reg_name":        rule["regulation_name"],
        "reg_description": rule["description"],
        "reg_reference":   rule["doc_reference"],
        "severity":        rule["severity_level"],

        "total_spaces":  len(summary.get("spaces",          [])),
        "total_doors":   len(summary.get("doors",           [])),
        "total_stairs":  len(summary.get("stairs",          [])),
        "total_windows": len(summary.get("windows",         [])),
        "total_exits":   len(summary.get("emergency_exits", []))
    }

    try:
        result = chain.invoke(chain_input)
        return result.strip()
    except Exception as e:
        # Pipeline must not crash on one bad API call — fall back to a
        # plain-text explanation built from the regulation data itself.
        print(f"    LLM error: {e}")
        return (
            f"AI explanation unavailable for this scenario. "
            f"Regulation {rule['unique_id']} ({rule['regulation_name']}) "
            f"was violated as described above. "
            f"See {rule['doc_reference']} for guidance."
        )


if __name__ == "__main__":
    import sys
    from ifc_parser import get_summary
    from uk_regulation_checking import check_all_rules

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )

    print("Step 1 — Loading IFC file...")
    summary = get_summary(ifc_path)

    print("Step 2 — Checking ADB regulations...")
    flags = check_all_rules(summary, jurisdiction="england")
    print(f"         {len(flags)} flag(s) found")

    if not flags:
        print("No flags found — IFC file may be compliant or attributes missing")
    else:
        print("\nStep 3 — Testing the chain with the first flag only "
              "(keeps API cost low during testing)...\n")

        scenario = build_one_scenario(flags[0], summary)

        print("=" * 55)
        print("SCENARIO BUILT SUCCESSFULLY")
        print("=" * 55)
        print(f"ID           : {scenario['id']}")
        print(f"Regulation   : {scenario['regulation_id']} — {scenario['regulation_name']}")
        print(f"IFC Element  : {scenario['ifc_element_type']} — {scenario['ifc_element_name']}")
        print(f"Attribute    : {scenario['ifc_attribute']}")
        print(f"Severity     : {scenario['severity']}")
        print(f"\nAI Explanation:\n  {scenario['ai_explanation']}")
