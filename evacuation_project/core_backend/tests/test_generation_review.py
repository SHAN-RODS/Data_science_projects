"""The occupant total is computed from the scenario's multipliers, not named by the model.

The model used to state occupants_total itself, and a review loop caught the contradictions that
followed: a total its own multipliers could not seat, a total at the computed ceiling, two scenarios
evacuating the same number. Those repairs cost a full regeneration each and routinely ran out the
attempt budget, because the model was being asked for a sum the code already computes -- once the
multipliers are chosen, the total is determined.

Deriving it makes all three faults unreachable rather than repaired. What is left for review is the
modelling choices the arithmetic cannot settle. Two scenarios sharing a multiplier set seed
occupants into the same rooms in the same proportions and are the same simulation run twice. And in
a building people sleep in, a night state seating fewer occupants than a daytime one has the
population moving the wrong way -- residents are home and asleep at night and out during the day.

The last tests pin the deliberate asymmetry with the schema retry: a finding is a flaw in the set,
not an unusable reply, so an unrepaired one keeps the closest set rather than losing an expensive
call.
"""

from core_backend.scenario_generation_llm_1 import (BuildingAnalysis, assemble_scenario,
                                                    direction_findings, invoke_structured,
                                                    occupancy_findings, room_capacity)
from core_backend.occupant_placement import scenario_weights
from core_backend.tests.test_generation_retry import sample_scenario, structured_reply

# Two dwellings holding 74 between them; the multipliers below thin that to the capacity each test
# needs, and 74 is the whole computed load.
SPACES = [{"guid": "a", "use_type": "dwelling", "occupant_load": 40},
          {"guid": "b", "use_type": "dwelling", "occupant_load": 34}]


def scenario(multipliers, type_name="base_case"):
    """A schema-valid scenario carrying a given multiplier set. It states no total -- there is no
    field for one, which is the point. ``type_name`` doubles as the occupancy_state, so a scenario
    called 'night' here reads as one to the direction check too."""
    sc = sample_scenario(1.0)
    sc["type"] = type_name
    sc["conditions"]["occupancy_state"] = type_name
    sc["simulation"]["occupancy_multipliers"] = [
        {"use_type": use_type, "multiplier": m, "reason": "r"}
        for use_type, m in multipliers.items()]
    return sc


def review(analysis):
    return occupancy_findings(analysis, SPACES)


def total_of(sc):
    """The total as the assembled object reports it."""
    return assemble_scenario(sc, 1, {}, SPACES)["occupancy"]["occupants_total"]


class FakeLLM:
    """Returns the scenario set at each position of ``sets``, sticking on the last one."""

    def __init__(self, sets):
        self.sets = sets
        self.prompts = []

    def with_structured_output(self, model, **kwargs):
        llm = self

        class Structured:
            def invoke(self, prompt):
                llm.prompts.append(prompt)
                index = min(len(llm.prompts) - 1, len(llm.sets) - 1)
                return structured_reply(BuildingAnalysis(scenarios=llm.sets[index]))

        return Structured()


# ---- the total is derived, not stated --------------------------------------------------------

def test_the_total_is_the_computed_load_thinned_by_the_multipliers():
    analysis = BuildingAnalysis(scenarios=[scenario({"dwelling": 0.5})])

    assert total_of(analysis.scenarios[0]) == 37        # (40 + 34) * 0.5


def test_a_use_type_without_a_multiplier_stays_at_its_computed_load():
    """The default is 1.0, not 0 -- an unmentioned use type is untouched, not emptied."""
    analysis = BuildingAnalysis(scenarios=[scenario({"commercial": 0.0})])

    assert total_of(analysis.scenarios[0]) == 74


def test_the_total_is_the_capacity_occupant_placement_seats_into():
    """The derived total is only right if it is what the placement downstream actually seats. Both
    read the same field off the same rooms, and this pins them together."""
    assembled = assemble_scenario(BuildingAnalysis(
        scenarios=[scenario({"dwelling": 0.6})]).scenarios[0], 1, {}, SPACES)

    downstream = int(sum(scenario_weights({"spaces": SPACES}, assembled).values()))

    assert assembled["occupancy"]["occupants_total"] == downstream == 44


def test_a_total_its_own_multipliers_cannot_seat_is_now_unreachable():
    """The fault that used to drive most repairs. The total is the seat count, so the shortfall it
    described cannot arise -- and costs no attempt."""
    for multipliers in ({"dwelling": 0.5}, {"dwelling": 0.0}, {"dwelling": 1.0}):
        sc = BuildingAnalysis(scenarios=[scenario(multipliers)]).scenarios[0]
        assert total_of(sc) == room_capacity(SPACES, sc)
        assert occupancy_findings(BuildingAnalysis(scenarios=[scenario(multipliers)]),
                                  SPACES) == []


def test_different_multipliers_give_different_totals():
    night = BuildingAnalysis(scenarios=[scenario({"dwelling": 1.0}, "night")]).scenarios[0]
    day = BuildingAnalysis(scenarios=[scenario({"dwelling": 0.4}, "day")]).scenarios[0]

    assert total_of(night) == 74
    assert total_of(day) == 29
    assert occupancy_findings(BuildingAnalysis(scenarios=[night, day]), SPACES) == []


def test_multipliers_that_differ_but_cancel_to_the_same_total_are_still_caught():
    """Deriving the total does NOT make duplicate populations impossible — thinning one use type
    while filling another lands on the same sum from a different multiplier set. Seen in a real run:
    two scenarios with distinct signatures both evacuating 111."""
    mixed = [{"guid": "a", "use_type": "dwelling", "occupant_load": 40},
             {"guid": "b", "use_type": "commercial", "occupant_load": 40}]
    day = scenario({"dwelling": 0.5, "commercial": 1.0}, "day")        # 20 + 40 = 60
    evening = scenario({"dwelling": 1.0, "commercial": 0.5}, "evening")  # 40 + 20 = 60

    findings = occupancy_findings(BuildingAnalysis(scenarios=[day, evening]), mixed)

    assert len(findings) == 1
    assert "each work out to 60 occupant(s)" in findings[0]
    assert "cancel to the same population" in findings[0]


# ---- which way the population moves between night and day ------------------------------------

def test_a_night_thinner_than_its_day_is_repaired():
    """Seen in the generated output: the model thinned the dwellings at night and filled them by day,
    so the residential building evacuated fewer people asleep than awake."""
    inverted = [scenario({"dwelling": 0.3}, "night"), scenario({"dwelling": 1.0}, "day")]
    corrected = [scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 0.3}, "day")]
    llm = FakeLLM([inverted, corrected])

    analysis = invoke_structured(llm, "PROMPT", review=review)

    assert len(llm.prompts) == 2
    assert "inverted" in llm.prompts[1]
    assert "seat 22 occupant(s)" in llm.prompts[1]      # night: (40 + 34) * 0.3
    assert "seats 74" in llm.prompts[1]                 # day at the full computed load
    assert [total_of(sc) for sc in analysis.scenarios] == [74, 22]


def test_a_night_above_its_day_passes():
    """Not every building thins by day; only the inversion is wrong."""
    assert occupancy_findings(BuildingAnalysis(scenarios=[
        scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 0.4}, "day")]), SPACES) == []


def test_equal_populations_are_not_an_inversion():
    """They are still two runs of the same simulation, which the duplicate check says on its own —
    but the population is not moving the wrong way, so the direction check stays quiet."""
    equal = BuildingAnalysis(scenarios=[scenario({"dwelling": 0.6}, "night"),
                                        scenario({"dwelling": 0.6}, "daytime peak")])

    assert direction_findings(equal, SPACES) == []
    assert all("inverted" not in f for f in occupancy_findings(equal, SPACES))


def test_a_building_nobody_sleeps_in_is_left_alone():
    """An office is genuinely busier by day — the check must not fire on it."""
    offices = [{"guid": "a", "use_type": "commercial", "occupant_load": 40},
               {"guid": "b", "use_type": "commercial", "occupant_load": 34}]

    assert occupancy_findings(BuildingAnalysis(scenarios=[
        scenario({"commercial": 0.1}, "night"), scenario({"commercial": 1.0}, "day")]),
        offices) == []


def test_the_check_needs_both_states_to_compare():
    assert occupancy_findings(BuildingAnalysis(scenarios=[
        scenario({"dwelling": 0.3}, "night"), scenario({"dwelling": 1.0}, "evening")]),
        SPACES) == []


def test_the_fullest_scenario_of_each_state_is_the_one_compared():
    """A quiet night variant beside a full one is a variant, not an inversion."""
    assert occupancy_findings(BuildingAnalysis(scenarios=[
        scenario({"dwelling": 0.3}, "night, one exit lost"),
        scenario({"dwelling": 1.0}, "night"),
        scenario({"dwelling": 0.4}, "day")]), SPACES) == []


def test_the_finding_names_which_multipliers_to_move():
    """A finding the model cannot act on costs an attempt and changes nothing, so it says which
    use types to raise and which to thin rather than only that the set is wrong."""
    finding = occupancy_findings(BuildingAnalysis(scenarios=[
        scenario({"dwelling": 0.3}, "night"), scenario({"dwelling": 1.0}, "day")]), SPACES)[0]

    assert "dwelling" in finding and "bedroom" in finding
    assert "renaming the states" in finding      # relabelling leaves the same populations in place


# ---- two scenarios that are really one -------------------------------------------------------

def test_two_scenarios_seeding_the_same_rooms_are_repaired():
    duplicated = [scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 1.0}, "day")]
    varied = [scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 0.4}, "day")]
    llm = FakeLLM([duplicated, varied])

    analysis = invoke_structured(llm, "PROMPT", review=review)

    assert len(llm.prompts) == 2
    assert "identical occupancy_multipliers" in llm.prompts[1]
    assert "same simulation run twice" in llm.prompts[1]
    assert [total_of(sc) for sc in analysis.scenarios] == [74, 29]


def test_scenarios_with_no_multipliers_at_all_are_repaired():
    """Empty sets collide the same way -- every scenario would seed every room at full load, so
    they trip both checks at once: the same signature and, inevitably, the same total."""
    findings = occupancy_findings(BuildingAnalysis(
        scenarios=[scenario({}, "night"), scenario({}, "day")]), SPACES)

    assert any("no occupancy_multipliers at all" in f for f in findings)
    assert any("each work out to 74 occupant(s)" in f for f in findings)


# ---- what happens when the model never complies ----------------------------------------------

def test_an_unrepaired_set_is_kept_rather_than_losing_the_run():
    """The asymmetry with the schema retry: findings are reported downstream, so a flawed set is
    still worth having. Raising here would throw away a call that costs minutes."""
    llm = FakeLLM([[scenario({"dwelling": 0.5}, "night"), scenario({"dwelling": 0.5}, "day")]])

    analysis = invoke_structured(llm, "PROMPT", attempts=3, review=review)

    assert len(llm.prompts) == 3                      # it did try to repair
    assert len(analysis.scenarios) == 2               # and handed back what it got


def test_the_closest_set_is_the_one_kept():
    """A model can answer a repair by breaking something else. What comes back is the best attempt,
    not merely the last."""
    one_fault = [scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 1.0}, "day"),
                 scenario({"dwelling": 0.4}, "evening")]
    two_faults = [scenario({"dwelling": 1.0}, "night"), scenario({"dwelling": 1.0}, "day"),
                  scenario({"dwelling": 0.4}, "evening"), scenario({"dwelling": 0.4}, "peak")]
    llm = FakeLLM([one_fault, two_faults])            # second attempt is worse

    analysis = invoke_structured(llm, "PROMPT", attempts=2, review=review)

    assert len(analysis.scenarios) == 3


def test_no_review_leaves_the_old_behaviour_alone():
    """Callers that pass no review -- and every existing test -- see exactly what they saw before."""
    llm = FakeLLM([[scenario({"dwelling": 0.5})]])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 1
    assert total_of(analysis.scenarios[0]) == 37
