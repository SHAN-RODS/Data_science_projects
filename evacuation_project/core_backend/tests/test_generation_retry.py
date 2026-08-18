"""The scenario generation call repairs a schema rejection instead of losing the whole run.

Generation is a single expensive call (up to 64k tokens and a 600 s read). One field the model puts
out of range otherwise throws all of it away. The case that drove this was an occupancy multiplier
above 1.0, from a model reaching for a "crowded" scenario; the multipliers now come from
occupancy_states rather than the reply, so the same repair is pinned here on a bound the model does
still set -- a profile fraction, which is a share of the population and cannot exceed 1.0.

The repair hands the model its own validation error. What it must NOT do is relax the constraint, so
the last test pins that a model which never complies still fails loudly.
"""

import pytest
from pydantic import ValidationError

from core_backend.scenario_generation_llm_1 import (BuildingAnalysis, NoStructuredReply,
                                                    invoke_structured)


def structured_reply(parsed=None, parsing_error=None, raw=None):
    """The shape ``with_structured_output(include_raw=True)`` returns: a parse failure arrives as
    data on the same dict rather than as an exception, so the reason a reply was unusable can be
    read off the provider's own metadata."""
    return {"raw": raw, "parsed": parsed, "parsing_error": parsing_error}


def sample_scenario(fraction=1.0, state="night_sleeping", discounted=()):
    """A schema-valid scenario. ``fraction`` is the profile share -- the bound the repair tests put
    out of range -- and ``state`` is the occupancy state, which is now an enum the model chooses
    from rather than a multiplier set it invents."""
    return {
        "id": "S1", "type": "base_case", "title": "Base", "purpose": "p",
        "conditions": {"exits_available": ["E"], "occupancy_state": state,
                       "exits_discounted": list(discounted)},
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
            "profiles": [{"name": "adult", "fraction": fraction, "speed_distribution": "normal",
                          "speed_ms_mean": 1.2, "speed_ms_sd": 0.0, "shoulder_width_m": 0.45,
                          "basis": "b"}],
            "evacuation_time": {"estimated_total_s": 300.0, "basis": "b"},
        },
        "fire_conditions": {"fire_origin": "not fire-specific", "fire_origin_storey": "",
                            "affected_exits": [], "affected_routes": [],
                            "detection_and_alarm": "d", "smoke_conditions": "s", "basis": "b"},
        "occupancy_rationale": "why this state", "regulatory_justification": "j",
        "ai_explanation": "e",
    }


class FakeLLM:
    """Returns the profile fraction at each position of ``sequence``, sticking on the last one."""

    def __init__(self, sequence):
        self.sequence = sequence
        self.prompts = []

    def with_structured_output(self, model, **kwargs):
        llm = self

        class Structured:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                index = min(len(llm.prompts) - 1, len(llm.sequence) - 1)
                # include_raw catches the validation failure rather than raising it, as the real
                # parser does — the caller reads it off parsing_error
                try:
                    parsed = BuildingAnalysis(scenarios=[sample_scenario(llm.sequence[index])])
                except ValidationError as exc:
                    return structured_reply(parsing_error=exc)
                return structured_reply(parsed)

        return Structured()


def test_a_valid_reply_is_not_retried():
    llm = FakeLLM([1.0])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 1
    assert analysis.scenarios[0].simulation.profiles[0].fraction == 1.0


def test_an_out_of_range_bound_is_repaired_rather_than_losing_the_run():
    llm = FakeLLM([2.0, 1.0])          # rejected, then corrected

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 2
    assert analysis.scenarios[0].simulation.profiles[0].fraction == 1.0
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
    assert "profiles" in str(excinfo.value)


def test_attempts_are_bounded():
    llm = FakeLLM([2.0])

    with pytest.raises(ValidationError):
        invoke_structured(llm, "PROMPT", attempts=2)

    assert len(llm.prompts) == 2


class RawReply:
    """A provider message as the parser hands it back — metadata and text, nothing parsed."""

    def __init__(self, stop=None, content="", output_tokens=None):
        self.response_metadata = {"finish_reason": stop} if stop else {}
        self.usage_metadata = {"output_tokens": output_tokens} if output_tokens else {}
        self.content = content


class SilentLLM:
    """A reply carrying no structure parses to None instead of raising.

    Seen in practice on providers that cannot be forced to call a named tool, and when a reply runs
    past the token ceiling part-way through. ``replies`` is what each attempt returns, sticking on
    the last one.
    """

    def __init__(self, replies):
        self.replies = replies
        self.prompts = []

    def with_structured_output(self, model, **kwargs):
        llm = self

        class Structured:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                index = min(len(llm.prompts) - 1, len(llm.replies) - 1)
                return llm.replies[index]

        return Structured()


def test_an_empty_reply_is_retried_not_handed_back_as_none():
    # returning None here reached the caller as `'NoneType' object has no attribute 'scenarios'`
    llm = SilentLLM([structured_reply(raw=RawReply(stop="stop")),
                     structured_reply(BuildingAnalysis(scenarios=[sample_scenario(1.0)]))])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 2
    assert analysis.scenarios[0].simulation.profiles[0].fraction == 1.0
    assert "PROMPT" in llm.prompts[1]
    assert "no structured result" in llm.prompts[1]


def test_a_truncated_reply_says_so_and_names_the_ceiling():
    """The error must report what the provider said, not guess. A cut-off is the one case where
    raising the ceiling is the right advice, so it is the only case that gives it."""
    llm = SilentLLM([structured_reply(raw=RawReply(stop="length", output_tokens=24000))])

    with pytest.raises(NoStructuredReply) as excinfo:
        invoke_structured(llm, "PROMPT", attempts=2)

    assert len(llm.prompts) == 2
    message = str(excinfo.value)
    assert "cut off" in message
    assert "24000 tokens" in message
    assert "EVAC_GEN_MAX_TOKENS" in message


def test_a_prose_reply_quotes_what_the_model_said_instead():
    """The failure this project actually hits: the model answers in prose because its provider
    cannot be forced to call a named tool. Blaming the token ceiling here sends the reader to the
    wrong knob, so the message quotes the reply instead."""
    llm = SilentLLM([structured_reply(
        raw=RawReply(stop="stop", content="I cannot produce that structure because"))])

    with pytest.raises(NoStructuredReply) as excinfo:
        invoke_structured(llm, "PROMPT", attempts=1)

    message = str(excinfo.value)
    assert "prose instead of the structure" in message
    assert "I cannot produce that structure" in message
    assert "EVAC_GEN_MAX_TOKENS" not in message
