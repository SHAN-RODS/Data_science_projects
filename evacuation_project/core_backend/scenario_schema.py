
#JSON Schema for the whole-building evacuation scenarios.

SCENARIO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EvacuationScenarioObject",
    "type": "object",
    "required": ["schema_version", "provenance", "building", "exits", "spaces",
                 "scenarios", "validation", "not_assessed"],
    "properties": {
        "schema_version": {"type": "string"},
        "provenance": {
            "type": "object",
            "required": ["generated_by_model", "distance_method", "llm_grounded"],
            "properties": {
                "generated_by_model": {"type": ["string", "null"]},
                "generated_at": {"type": "string"},
                "occupancy_factor_source": {"type": "string"},
                "distance_method": {"type": "string"},
                "llm_grounded": {"type": "boolean"},
                "llm_temperature": {"type": ["string", "number", "null"]},
            },
        },
        "building": {
            "type": "object",
            "required": ["project", "storeys", "total_floor_area_m2", "total_occupant_load"],
            "properties": {
                "project": {"type": ["string", "null"]},
                "source_ifc": {"type": ["string", "null"]},
                "jurisdiction": {"type": "string"},
                "occupancy_type": {"type": "string"},
                "storeys": {"type": "integer", "minimum": 0},
                "total_floor_area_m2": {"type": "number", "minimum": 0},
                "total_occupant_load": {"type": "integer", "minimum": 0},
            },
        },
        "exits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                    "width_m": {"type": ["number", "null"]},
                },
            },
        },
        "circulation": {"type": "array"},
        "spaces": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["guid", "use_type", "reachable"],
                "properties": {
                    "guid": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "use_type": {"type": "string"},
                    "use_type_confidence": {"type": ["number", "null"]},
                    "use_type_source": {"type": ["string", "null"]},
                    "storey": {"type": ["string", "null"]},
                    "area_m2": {"type": ["number", "null"]},
                    "occupant_load": {"type": ["integer", "null"], "minimum": 0},
                    "occupant_basis": {"type": ["string", "null"]},
                    "nearest_exit": {"type": ["string", "null"]},
                    "travel_distance_m": {"type": ["number", "null"], "minimum": 0},
                    "travel_distance_method": {"type": ["string", "null"]},
                    "most_remote_point": {"type": ["array", "null"]},
                    "reachable": {"type": "boolean"},
                },
            },
        },
        "degraded_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["exit_discounted", "per_storey"],
                "properties": {
                    "exit_discounted": {"type": "string"},
                    "method": {"type": "string"},
                    "per_storey": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "storey": {"type": ["string", "null"]},
                                "occupants": {"type": "integer", "minimum": 0},
                                "max_travel_distance_m": {"type": ["number", "null"], "minimum": 0},
                                "unreachable": {"type": "integer", "minimum": 0},
                            },
                        },
                    },
                },
            },
        },
        "scenarios": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["id", "type", "conditions", "narrative"],
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "conditions": {
                        "type": "object",
                        "required": ["exits_available", "exits_discounted", "occupants_total"],
                        "properties": {
                            "exits_available": {"type": "array", "items": {"type": "string"}},
                            "exits_discounted": {"type": "array", "items": {"type": "string"}},
                            "occupancy_state": {"type": "string"},
                            "occupants_total": {"type": "integer", "minimum": 0},
                        },
                    },
                    "assumptions": {"type": "array"},
                    "occupant_distribution": {"type": "array"},
                    "routes": {"type": "array"},
                    "bottlenecks": {"type": "array"},
                    "risks": {"type": "array"},
                    "narrative": {"type": "string"},
                    "regulatory_justification": {"type": "string"},
                    "ai_explanation": {"type": "string"},
                },
            },
        },
        "regulation_check": {
            "type": "object",
            "required": ["passed"],
            "properties": {
                "jurisdiction": {"type": "string"},
                "passed": {"type": "boolean"},
                "basis": {"type": "string"},
                "violations": {"type": "array"},
                "manual_review": {"type": "array"},
            },
        },
        "validation": {"type": "object"},
        "not_assessed": {"type": "array"},
    },
}
