"""The scenario generation call repairs a schema rejection instead of losing the whole run.

Generation is a single expensive call (16k tokens, up to a 600 s read). One field the model puts out
of range otherwise throws all of it away. The case seen in practice is an ``occupancy_multiplier``
above 1.0: a model reaching for a "crowded" scenario tries to scale occupancy up, which the schema
forbids by design -- a computed occupant load is already the room's code-derived capacity, so there
is no valid multiplier above 1.0.

The repair hands the model its own validation error. What it must NOT do is relax the constraint, so
the last test pins that a model which never complies still fails loudly.
"""

import pytest
from pydantic import ValidationError

from core_backend.scenario_generation_llm_1 import BuildingAnalysis, invoke_structured


def sample_scenario(multiplier):
    return {
        "id": "S1", "type": "base_case", "title": "Base", "purpose": "p",
        "conditions": {"exits_available": ["E"], "exits_discounted": [],
                       "occupants_total": 10, "occupancy_state": "day"},
        "assumptions": [], "occupant_distribution": [], "routes": [], "restricted_areas": [],
        "bottlenecks": [], "risks": [],
        "narrative": "n",
        "simulation": {
            "movement_model": "steering",
            "simulation_settings": {"start_conditions": "s",
                                    "duration": {"seconds": 600.0, "basis": "b"}},
            "pre_movement": {"detection": "d", "alarm": "a", "recognition": "r",
                             "response_delay": {"distribution": "normal", "mean_s": 60.0,
                                                "sd_s": 10.0, "basis": "b"}},
            "profiles": [{"name": "adult", "fraction": 1.0, "speed_distribution": "normal",
                          "speed_ms_mean": 1.2, "speed_ms_sd": 0.0, "shoulder_width_m": 0.45,
                          "basis": "b"}],
            "occupancy_multipliers": [{"use_type": "dwelling", "multiplier": multiplier,
                                       "reason": "r"}],
            "evacuation_time": {"estimated_total_s": 300.0, "basis": "b"},
        },
        "fire_conditions": {"fire_origin": "not fire-specific", "fire_origin_storey": "",
                            "affected_exits": [], "affected_routes": [],
                            "detection_and_alarm": "d", "smoke_conditions": "s", "basis": "b"},
        "regulatory_justification": "j", "ai_explanation": "e",
    }


class FakeLLM:
    """Returns the multiplier at each position of ``sequence``, sticking on the last one."""

    def __init__(self, sequence):
        self.sequence = sequence
        self.prompts = []

    def with_structured_output(self, model):
        llm = self

        class Structured:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                index = min(len(llm.prompts) - 1, len(llm.sequence) - 1)
                return BuildingAnalysis(scenarios=[sample_scenario(llm.sequence[index])])

        return Structured()


def test_a_valid_reply_is_not_retried():
    llm = FakeLLM([1.0])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 1
    assert analysis.scenarios[0].simulation.occupancy_multipliers[0].multiplier == 1.0


def test_an_out_of_range_multiplier_is_repaired_rather_than_losing_the_run():
    llm = FakeLLM([2.0, 1.0])          # rejected, then corrected

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 2
    assert analysis.scenarios[0].simulation.occupancy_multipliers[0].multiplier == 1.0
    # the retry must carry both the original facts and the reason the last reply was rejected
    assert "PROMPT" in llm.prompts[1]
    assert "less_than_equal" in llm.prompts[1]


def test_a_model_that_never_complies_still_fails_and_the_bound_holds():
    """The repair loop must not become a way to smuggle an invalid value through."""
    llm = FakeLLM([2.0])

    with pytest.raises(ValidationError) as excinfo:
        invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) > 1                       # it did try to repair
    # and the error the user sees names the real offending field
    assert "occupancy_multipliers" in str(excinfo.value)


def test_attempts_are_bounded():
    llm = FakeLLM([2.0])

    with pytest.raises(ValidationError):
        invoke_structured(llm, "PROMPT", attempts=2)

    assert len(llm.prompts) == 2
