
#JSON Schema for the whole-building evacuation scenarios.

SCENARIO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EvacuationScenarioObject",
    "type": "object",
    "required": ["schema_version", "provenance", "building", "exits", "spaces",
                 "scenarios", "validation", "not_assessed"],
    "properties": {
        "schema_version": {"type": "string"},
        # How to line this object up with the geometry an egress simulator imports from the same IFC.
        "model": {
            "type": "object",
            "required": ["units"],
            "properties": {
                "source_ifc": {"type": ["string", "null"]},
                "units": {"type": "string"},
                "coordinate_system": {"type": "string"},
                "geometry_note": {"type": "string"},
            },
        },
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
                    "position": {"type": ["array", "null"]},
                },
            },
        },
        # Internal doors: the room-to-room connections a simulator rebuilds as door components.
        "doors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                    "width_m": {"type": ["number", "null"]},
                    "position": {"type": ["array", "null"]},
                    "connects": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "circulation": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "type": {"type": "string"},
                    "width_m": {"type": ["number", "null"]},
                    "width_source": {"type": ["string", "null"]},
                    "position": {"type": ["array", "null"]},
                    "rise_m": {"type": ["number", "null"]},
                    "going_m": {"type": ["number", "null"]},
                    "slope_m": {"type": ["number", "null"]},
                    "connects_storeys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        # Vertical links the egress graph actually walked (stair space <-> stair space).
        "stair_links": {"type": "array"},
        "elevators": {"type": "array"},
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
                    "centroid": {"type": ["array", "null"]},
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
                    # The egress-simulation set-up. These are the only AI-originated numbers in the
                    # object, so each carries a basis/reason and is range-checked in validation.py.
                    "simulation": {
                        "type": "object",
                        "required": ["movement_model", "pre_movement", "profiles"],
                        "properties": {
                            "movement_model": {"type": "string"},
                            "end_time_s": {"type": "number", "minimum": 0},
                            "pre_movement": {
                                "type": "object",
                                "required": ["distribution", "mean_s", "basis"],
                                "properties": {
                                    "distribution": {"type": "string"},
                                    "mean_s": {"type": "number", "minimum": 0},
                                    "sd_s": {"type": "number", "minimum": 0},
                                    "basis": {"type": "string"},
                                },
                            },
                            "profiles": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": ["name", "fraction", "speed_ms_mean",
                                                 "shoulder_width_m", "basis"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "fraction": {"type": "number", "minimum": 0, "maximum": 1},
                                        "speed_distribution": {"type": "string"},
                                        "speed_ms_mean": {"type": "number", "minimum": 0},
                                        "speed_ms_sd": {"type": "number", "minimum": 0},
                                        "shoulder_width_m": {"type": "number", "minimum": 0},
                                        "basis": {"type": "string"},
                                    },
                                },
                            },
                            "occupancy_multipliers": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["use_type", "multiplier"],
                                    "properties": {
                                        "use_type": {"type": "string"},
                                        "multiplier": {"type": "number", "minimum": 0, "maximum": 1},
                                        "reason": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    # Where this scenario's occupants start: computed deterministically from the
                    # per-room occupant load and the AI's multipliers (occupant_placement.py), not
                    # asked of the model. Optional so objects generated before it existed still pass.
                    "occupancy": {
                        "type": "object",
                        "properties": {
                            "occupants_total": {"type": ["integer", "null"], "minimum": 0},
                            "occupancy_state": {"type": ["string", "null"]},
                            "placed_total": {"type": "integer", "minimum": 0},
                            "unplaced_total": {"type": "integer", "minimum": 0},
                            # occupants_total minus what the scenario's own multipliers leave room for
                            "unallocated_total": {"type": "integer", "minimum": 0},
                            "unallocated_why": {"type": ["string", "null"]},
                            "scenario_room_capacity": {"type": "integer", "minimum": 0},
                            "building_computed_total": {"type": ["integer", "null"], "minimum": 0},
                            "allocation_method": {"type": "string"},
                            "position_note": {"type": "string"},
                            "by_room": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["guid", "occupants", "goal"],
                                    "properties": {
                                        "guid": {"type": "string"},
                                        "name": {"type": ["string", "null"]},
                                        "storey": {"type": ["string", "null"]},
                                        "use_type": {"type": ["string", "null"]},
                                        "computed_occupant_load": {"type": ["integer", "null"],
                                                                   "minimum": 0},
                                        "occupants": {"type": "integer", "minimum": 1},
                                        # the room centroid, in metres, IFC world coordinates
                                        "seed_point": {"type": ["array", "null"]},
                                        "profiles": {"type": "object"},
                                        "goal": {"type": "string"},
                                    },
                                },
                            },
                            "unplaced_rooms": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["guid", "occupants_not_placed", "why"],
                                    "properties": {
                                        "guid": {"type": "string"},
                                        "name": {"type": ["string", "null"]},
                                        "storey": {"type": ["string", "null"]},
                                        "use_type": {"type": ["string", "null"]},
                                        "computed_occupant_load": {"type": ["integer", "null"],
                                                                   "minimum": 0},
                                        "occupants_not_placed": {"type": "integer", "minimum": 1},
                                        "why": {"type": "string"},
                                    },
                                },
                            },
                            "rerouted_rooms": {
                                "type": "object",
                                "properties": {
                                    "count": {"type": "integer", "minimum": 0},
                                    "guids": {"type": "array", "items": {"type": "string"}},
                                    "why": {"type": "string"},
                                },
                            },
                        },
                    },
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
