"""Export the whole-building evacuation scenario object.

JSON is the export format (the object is already the schema-shaped dict from the generator/validator).
"""

import json


def export_json(scenario_object):
    return json.dumps(scenario_object, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    from core_backend.scenario_generation_llm import build_full_scenario
    from core_backend.validation import validate
    from core_backend.sample_paths import resolve_ifc

    args = [a for a in sys.argv if not a.startswith("--")]
    obj = validate(build_full_scenario(resolve_ifc(args), jurisdiction="england"))
    print(export_json(obj))
