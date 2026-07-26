"""Known-answer tests for the regulation PASS/FAIL gate (deterministic, no API).

The gate is the blocking validation step: any hard threshold violation FAILS the building and blocks
scenario generation; "manual review" items (undecidable from the IFC) are surfaced but do not block.
These drive ``regulation_gate`` with hand-built England summaries against the real eng_reg.json.
"""

from core_backend.uk_regulation_checking import regulation_gate


def _ground_storey():
    return [{"id": "S1", "name": "Ground", "elevation_m": 0.0, "height_above_ground_m": 0.0}]


def _passing_summary():
    # wide doors, two final exits, nothing else measurable -> no hard violations
    return {
        "doors": [{"id": "D1", "name": "D1", "width_m": 0.9},
                  {"id": "D2", "name": "D2", "width_m": 0.9}],
        "emergency_exits": [{"id": "E1", "name": "Exit 1", "width_m": 1.0},
                            {"id": "E2", "name": "Exit 2", "width_m": 1.0}],
        "stairs": [],
        "windows": [],
        "elevators": [],
        "slabs": [],
        "storeys": _ground_storey(),
    }


def test_gate_passes_clean_building():
    gate = regulation_gate(_passing_summary(), "england")
    assert gate["passed"] is True
    assert gate["violations"] == []


def test_gate_fails_on_hard_violations():
    # No final exit -> ENG-R11 (need >=1) and ENG-R12 (need >=2 escape routes) both fire. Uses the
    # count-based exit rules, which are stable, rather than a width threshold that can be tuned in the JSON.
    summary = _passing_summary()
    summary["emergency_exits"] = []
    gate = regulation_gate(summary, "england")

    assert gate["passed"] is False
    ids = {v["regulation_id"] for v in gate["violations"]}
    assert "ENG-R11" in ids      # no final exit


def test_manual_review_does_not_block():
    # an un-rated floor slab can't be decided from the IFC -> manual review, but the building still PASSES
    summary = _passing_summary()
    summary["slabs"] = [{"id": "SL1", "name": "Floor slab", "slab_type": "FLOOR", "fire_rating": None}]
    gate = regulation_gate(summary, "england")

    assert gate["passed"] is True
    assert any(mr["regulation_id"] == "ENG-R10" for mr in gate["manual_review"])
