"""Export the whole-building evacuation scenario object.

JSON is the primary format (the object is already the schema-shaped dict from the generator/validator).
XML is offered as a secondary export via a generic dict -> element conversion.
"""

import json
import re
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString


def export_json(scenario_object):
    return json.dumps(scenario_object, indent=2, ensure_ascii=False)


def _safe_tag(key):
    tag = re.sub(r"[^A-Za-z0-9_]", "_", str(key))
    if not tag or not (tag[0].isalpha() or tag[0] == "_"):
        tag = "n_" + tag
    return tag


def _to_xml(parent, key, value):
    if isinstance(value, dict):
        node = SubElement(parent, _safe_tag(key))
        for k, v in value.items():
            _to_xml(node, k, v)
    elif isinstance(value, list):
        node = SubElement(parent, _safe_tag(key))
        for item in value:
            _to_xml(node, "item", item)
    else:
        node = SubElement(parent, _safe_tag(key))
        node.text = "" if value is None else str(value)


def export_xml(scenario_object):
    root = Element("EvacuationScenarioObject")
    for key, value in scenario_object.items():
        _to_xml(root, key, value)
    return parseString(tostring(root, "unicode")).toprettyxml(indent="  ")


if __name__ == "__main__":
    import sys
    from core_backend.scenario_generation_llm import build_full_scenario
    from core_backend.validation import validate
    from core_backend.sample_paths import resolve_ifc

    args = [a for a in sys.argv if not a.startswith("--")]
    obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))
    print(export_json(obj))
