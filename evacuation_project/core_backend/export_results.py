import json
import sys
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString
from core_backend.ifc_parser import parser_summary
from core_backend.uk_regulation_checking import check_all_rules
from core_backend.scenario_generation_llm import generate_scenarios, model_label

imp_fields = [
    "id", "description", "ifc_element_type", "ifc_element_name",
    "ifc_element_guid", "ifc_attribute", "regulation_id", "regulation_name",
    "regulation_description", "regulation_reference", "severity",
    "requires_manual_review", "ai_explanation"
]

def final_scenario_data(scenarios, ai_model_used):
    return {
        "title":"Final Evacuation Scenario results",
        "regulation_sources":"UK Approved Regulation Documents",
        "ai_model_used": ai_model_used,
        "export_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scenarios":len(scenarios),
        "note": ("AI-assisted evacuation scenarios.")
    }

def export_json(scenarios, ai_model_used="Unknown"):
    output = {
        "export_info": final_scenario_data(scenarios, ai_model_used),
        "scenarios": [
            {field: s.get(field) for field in imp_fields}
            for s in scenarios
        ]
    }
    return json.dumps(output, indent=2, ensure_ascii=False)

def export_xml(scenarios, ai_model_used="Unknown"):
    root = Element("EvacuationScenarios")
    root.set("regulation_source", "UK Approved Document B (Fire Safety)")
    root.set("ai_model_used", ai_model_used)
    root.set("export_timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    root.set("total_scenarios", str(len(scenarios)))

    for s in scenarios:
        node = SubElement(root, "Scenario",
                           id=str(s.get("id", "")),
                           severity=str(s.get("severity", "")))
        for field in imp_fields:
            if field in ("id", "severity"):
                continue
            child = SubElement(node, field)
            child.text = str(s.get(field, ""))

    return parseString(tostring(root, "unicode")).toprettyxml(indent="  ")

if __name__ == "__main__":

    ifc_path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\Shannan\Desktop\Msc data science uog\term 3- msc project"
        r"\bim residential models\ARK_NordicLCA_Housing_Concrete_As-Built_Revit-IFC4X3 original.ifc"
    )

    summary = parser_summary(ifc_path)

    flags = check_all_rules(summary, jurisdiction="england")
    print(f" {len(flags)} flags found")

    scenarios = generate_scenarios(flags, summary)

    print("\nJSON export")
    print(export_json(scenarios, ai_model_used=model_label))

    print("\nXML export")
    print(export_xml(scenarios, ai_model_used=model_label))
