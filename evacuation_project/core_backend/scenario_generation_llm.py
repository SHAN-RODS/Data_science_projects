#Gives Summary about each Violation

import os
import uuid
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
import sys
from core_backend.ifc_parser import parser_summary
from core_backend.uk_regulation_checking import check_all_rules

load_dotenv()  

#Checks For Anthropic first and if it's missing fallbacks to mistral
def select_llm():
    if os.getenv("ANTHROPIC_API_KEY"):
        model = os.getenv("ANTHROPIC_MODEL")
        temperature = float(os.getenv("ANTHROPIC_TEMPERATURE"))
        return (
            ChatAnthropic(
                model_name=model,
                temperature=temperature,
                max_tokens=400
            ),
            f"{model} (Anthropic API)"
        )
    mistral_key = os.getenv("MISTRAL_API_KEY") or os.getenv("mistral")
    if mistral_key:
        model = os.getenv("MISTRAL_MODEL")
        temperature = float(os.getenv("MISTRAL_TEMPERATURE"))
        return (
            ChatMistralAI(
                model=model,
                api_key=mistral_key,
                temperature=temperature,
                max_tokens=400
            ),
            f"{model} (Mistral AI)"
        )


llm, model_label = select_llm()

prompt_template = PromptTemplate(
    input_variables=[
        "issue", "element_type", "element_name", "element_id", "attribute",
        "reg_id", "reg_name", "reg_description", "reg_reference", "severity",
        "total_spaces", "total_doors", "total_stairs", "total_windows", "total_exits"
    ],
    template="""This is an NLP-assisted evacuation scenario suggestion residential building model 
check against the regulation documents.

After checking the regulations, it has found out the following violations:

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
    return [building_scenarios(flag, summary) for flag in flags]


def building_scenarios(flag, summary):
    rule = flag["rule"]
    requires_manual_review = flag.get("requires_manual_review", False)

    if requires_manual_review:
        explanation = (
            f"{rule['description']} This cannot be confirmed automatically "
            f"from the IFC model — a qualified expert should verify this "
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
        "selected": True
    }

def run_chain(flag, summary):
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

        "total_spaces":  len(summary.get("spaces", [])),
        "total_doors":   len(summary.get("doors", [])),
        "total_stairs":  len(summary.get("stairs", [])),
        "total_windows": len(summary.get("windows", [])),
        "total_exits":   len(summary.get("emergency_exits",[]))
    }

    try:
        result = chain.invoke(chain_input)
        return result.strip()
    except Exception as e:
        print(f"LLM error: {e}")
        return (
            f"AI explanation unavailable for this scenario. "
            f"Regulation {rule['unique_id']} ({rule['regulation_name']}) "
            f"was violated as described above. "
            f"See {rule['doc_reference']} for guidance."
        )

if __name__ == "__main__":

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )

    summary = parser_summary(ifc_path)
    flags = check_all_rules(summary, jurisdiction="england")
    print(f"{len(flags)} flags found")
    if not flags:
        print("No flags found — the IFC file may be compliant or the attributes are missing")
    else:
        print("\nTesting the chain with the first flag only. "
              "This keeps API cost low during testing.\n")

        scenario = building_scenarios(flags[0], summary)
        print("The evacuation scenarios with AI explanation has been built successfully:")
        print(f"ID : {scenario['id']}")
        print(f"Regulation : {scenario['regulation_id']} — {scenario['regulation_name']}")
        print(f"IFC Element : {scenario['ifc_element_type']} — {scenario['ifc_element_name']}")
        print(f"Attribute : {scenario['ifc_attribute']}")
        print(f"Severity : {scenario['severity']}")
        print(f"\nAI Explanation:\n  {scenario['ai_explanation']}")
