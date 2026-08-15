"""The generation call reviews its own scenario set for occupancy faults and repairs them.

Every field can sit inside its bound and the set still be wrong, because the fault is between the
fields: a scenario asks for 50 occupants while its own multipliers thin the rooms down to 37, or two
scenarios carry the same multipliers and so seed occupants into the same rooms in the same
proportions. The schema cannot see either -- 50 is a valid integer and 0.5 is a valid multiplier --
so they used to reach the user as validation findings on the finished object, to reconcile by hand.

These pin the loop that hands them back instead. The last two pin the deliberate asymmetry with the
schema retry: a finding is a flaw in the set, not an unusable reply, so an unrepaired one keeps the
closest set rather than losing an expensive call.
"""

from core_backend.scenario_generation_llm_1 import (BuildingAnalysis, assemble_scenario,
                                                    invoke_structured, occupancy_findings,
                                                    room_capacity)
from core_backend.occupant_placement import scenario_weights
from core_backend.tests.test_generation_retry import sample_scenario

# Two dwellings holding 74 between them: the multipliers below thin that to the capacity each test
# needs, and 74 is the ceiling a scenario may not sit at.
SPACES = [{"guid": "a", "use_type": "dwelling", "occupant_load": 40},
          {"guid": "b", "use_type": "dwelling", "occupant_load": 34}]
CEILING = 74


def scenario(total, multipliers, type_name="base_case"):
    """A schema-valid scenario carrying a given total and multiplier set."""
    sc = sample_scenario(1.0)
    sc["type"] = type_name
    sc["conditions"]["occupants_total"] = total
    sc["simulation"]["occupancy_multipliers"] = [
        {"use_type": use_type, "multiplier": m, "reason": "r"}
        for use_type, m in multipliers.items()]
    return sc


def review(analysis):
    return occupancy_findings(analysis, SPACES, CEILING)


class FakeLLM:
    """Returns the scenario set at each position of ``sets``, sticking on the last one."""

    def __init__(self, sets):
        self.sets = sets
        self.prompts = []

    def with_structured_output(self, model):
        llm = self

        class Structured:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                index = min(len(llm.prompts) - 1, len(llm.sets) - 1)
                return BuildingAnalysis(scenarios=llm.sets[index])

        return Structured()


# ---- the capacity check ---------------------------------------------------------------------

def test_capacity_is_the_computed_load_thinned_by_the_multipliers():
    analysis = BuildingAnalysis(scenarios=[scenario(37, {"dwelling": 0.5})])

    assert room_capacity(SPACES, analysis.scenarios[0]) == 37       # (40 + 34) * 0.5


def test_a_use_type_without_a_multiplier_stays_at_its_computed_load():
    """The default is 1.0, not 0 -- an unmentioned use type is untouched, not emptied."""
    analysis = BuildingAnalysis(scenarios=[scenario(74, {"commercial": 0.0})])

    assert room_capacity(SPACES, analysis.scenarios[0]) == 74


def test_capacity_here_is_the_capacity_occupant_placement_seats_into():
    """The check is only worth making if it computes what the placement downstream computes. Both
    read the same field off the same rooms, and this pins them together."""
    sc = scenario(30, {"dwelling": 0.6})
    analysis = BuildingAnalysis(scenarios=[sc])

    obj = {"spaces": SPACES}
    downstream = int(sum(scenario_weights(obj, assemble_scenario(analysis.scenarios[0], 1, {}))
                         .values()))

    assert room_capacity(SPACES, analysis.scenarios[0]) == downstream == 44


def test_a_total_its_own_multipliers_cannot_seat_is_repaired():
    llm = FakeLLM([[scenario(50, {"dwelling": 0.5})],      # rooms hold 37, 13 homeless
                   [scenario(37, {"dwelling": 0.5})]])     # corrected

    analysis = invoke_structured(llm, "PROMPT", review=review)

    assert len(llm.prompts) == 2
    assert analysis.scenarios[0].conditions.occupants_total == 37
    # the retry carries the original facts and names the shortfall in the terms the model must fix
    assert "PROMPT" in llm.prompts[1]
    assert "occupants_total is 50" in llm.prompts[1]
    assert "holding only 37" in llm.prompts[1]
    assert "13 occupant(s)" in llm.prompts[1]


def test_a_total_the_multipliers_can_seat_raises_nothing():
    analysis = BuildingAnalysis(scenarios=[scenario(37, {"dwelling": 0.5})])

    assert occupancy_findings(analysis, SPACES, CEILING) == []


def test_the_whole_computed_load_is_not_an_occupancy_state():
    analysis = BuildingAnalysis(scenarios=[scenario(74, {"dwelling": 1.0})])

    findings = occupancy_findings(analysis, SPACES, CEILING)

    assert len(findings) == 1
    assert "capacity ceiling, not an occupancy state" in findings[0]


# ---- the variance checks --------------------------------------------------------------------

def test_two_scenarios_seeding_the_same_rooms_are_repaired():
    duplicated = [scenario(50, {"dwelling": 1.0}, "night"),
                  scenario(45, {"dwelling": 1.0}, "day")]
    varied = [scenario(60, {"dwelling": 1.0}, "night"),
              scenario(20, {"dwelling": 0.4}, "day")]
    llm = FakeLLM([duplicated, varied])

    analysis = invoke_structured(llm, "PROMPT", review=review)

    assert len(llm.prompts) == 2
    assert "identical occupancy_multipliers" in llm.prompts[1]
    assert "same simulation run twice" in llm.prompts[1]
    assert [sc.conditions.occupants_total for sc in analysis.scenarios] == [60, 20]


def test_two_scenarios_evacuating_the_same_number_are_flagged():
    analysis = BuildingAnalysis(scenarios=[scenario(30, {"dwelling": 1.0}, "night"),
                                           scenario(30, {"dwelling": 0.5}, "day")])

    findings = occupancy_findings(analysis, SPACES, CEILING)

    assert len(findings) == 1
    assert "all evacuate 30 occupant(s)" in findings[0]


def test_a_set_that_varies_both_the_number_and_the_rooms_passes():
    analysis = BuildingAnalysis(scenarios=[scenario(60, {"dwelling": 1.0}, "night"),
                                           scenario(20, {"dwelling": 0.4}, "day")])

    assert occupancy_findings(analysis, SPACES, CEILING) == []


# ---- what happens when the model never complies ----------------------------------------------

def test_an_unrepaired_set_is_kept_rather_than_losing_the_run():
    """The asymmetry with the schema retry: findings are reported downstream, so a flawed set is
    still worth having. Raising here would throw away a call that costs minutes."""
    llm = FakeLLM([[scenario(50, {"dwelling": 0.5})]])

    analysis = invoke_structured(llm, "PROMPT", attempts=3, review=review)

    assert len(llm.prompts) == 3                                   # it did try to repair
    assert analysis.scenarios[0].conditions.occupants_total == 50  # and handed back what it got


def test_the_closest_set_is_the_one_kept():
    """A model can answer a repair by breaking something else. What comes back is the best attempt,
    not merely the last."""
    two_faults = [scenario(50, {"dwelling": 0.5}, "night"),        # 13 unseated
                  scenario(50, {"dwelling": 0.5}, "day")]          # + duplicate total + signature
    one_fault = [scenario(50, {"dwelling": 0.5}, "night"),         # 13 unseated only
                 scenario(20, {"dwelling": 0.4}, "day")]
    llm = FakeLLM([one_fault, two_faults])                         # second attempt is worse

    analysis = invoke_structured(llm, "PROMPT", attempts=2, review=review)

    assert [sc.conditions.occupants_total for sc in analysis.scenarios] == [50, 20]


def test_no_review_leaves_the_old_behaviour_alone():
    """Callers that pass no review -- and every existing test -- see exactly what they saw before."""
    llm = FakeLLM([[scenario(50, {"dwelling": 0.5})]])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 1
    assert analysis.scenarios[0].conditions.occupants_total == 50
