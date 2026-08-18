"""The scenario's population comes from its occupancy state, not from numbers the model invents.

The model used to return a multiplier per use type per scenario, and a review loop caught what
followed: a total its own multipliers could not seat, two scenarios cancelling to the same
population, and — the one that fired on most runs — a night state holding fewer residents than a
daytime one. Each repair cost a full regeneration of every scenario, and the direction fault often
survived all three attempts, because the model was picking numbers whose effect it was never shown.

The multipliers are a table now (occupancy_states.py) and the model picks a state key from an enum.
The first two faults are unreachable because the total IS the seat count; the third is unreachable
because the table's night state sits at the schema maximum for residential space. What is left to
review is the one thing the model can still get wrong: pairing the same state with the same
discounted exits, which describes one simulation twice.

The last tests pin the deliberate asymmetry with the schema retry — a finding is a flaw in the set,
not an unusable reply, so an unrepaired one keeps the closest set rather than losing an expensive
call.
"""

from core_backend.occupancy_states import multipliers_for, occupants_under
from core_backend.occupant_placement import scenario_weights
from core_backend.scenario_generation_llm_1 import (BuildingAnalysis, assemble_scenario,
                                                    invoke_structured, occupancy_findings,
                                                    room_capacity, scenario_signature)
from core_backend.tests.test_generation_retry import sample_scenario, structured_reply

# Two dwellings holding 74 between them, plus an amenity — the mix that makes the states differ in
# where the people are and not only in how many.
SPACES = [{"guid": "a", "use_type": "dwelling", "occupant_load": 40},
          {"guid": "b", "use_type": "dwelling", "occupant_load": 34},
          {"guid": "c", "use_type": "communal_amenity", "occupant_load": 20}]

DWELLINGS = [s for s in SPACES if s["use_type"] == "dwelling"]


def scenario(state, discounted=(), type_name="base_case"):
    sc = sample_scenario(state=state, discounted=discounted)
    sc["type"] = type_name
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


# ---- the total follows from the state --------------------------------------------------------

def test_the_total_is_the_computed_load_thinned_by_the_state():
    night = BuildingAnalysis(scenarios=[scenario("night_sleeping")])

    assert total_of(night.scenarios[0]) == 74          # dwellings full, amenity closed


def test_a_different_state_reaches_a_different_population():
    day = BuildingAnalysis(scenarios=[scenario("working_day")])

    assert total_of(day.scenarios[0]) == int(74 * 0.3) + int(20 * 0.5)


def test_the_total_is_the_capacity_occupant_placement_seats_into():
    """The derived total is only right if it is what the placement downstream actually seats. Both
    read the same multipliers off the same rooms, and this pins them together."""
    assembled = assemble_scenario(
        BuildingAnalysis(scenarios=[scenario("weekend_daytime")]).scenarios[0], 1, {}, SPACES)

    downstream = int(sum(scenario_weights({"spaces": SPACES}, assembled).values()))

    assert assembled["occupancy"]["occupants_total"] == downstream


def test_a_total_the_state_cannot_seat_is_unreachable():
    """The fault that used to drive most repairs. The total is the seat count, so the shortfall it
    described cannot arise, and it costs no attempt."""
    for state in ("night_sleeping", "working_day", "evening_communal"):
        sc = BuildingAnalysis(scenarios=[scenario(state)]).scenarios[0]
        assert total_of(sc) == room_capacity(SPACES, sc)


def test_the_object_carries_the_multipliers_the_state_defines():
    """The model no longer returns them, but the object still stores them where placement,
    validation and the export already look — supplied by the table instead."""
    assembled = assemble_scenario(
        BuildingAnalysis(scenarios=[scenario("night_sleeping")]).scenarios[0], 1, {}, SPACES)

    stored = assembled["simulation"]["occupancy_multipliers"]

    assert {m["use_type"] for m in stored} == {"dwelling", "communal_amenity"}
    assert {m["use_type"]: m["multiplier"] for m in stored} == \
           {m["use_type"]: m["multiplier"]
            for m in multipliers_for("night_sleeping", {"dwelling", "communal_amenity"})}


def test_the_multipliers_only_cover_use_types_the_building_has():
    """A row for a room type nobody has is noise in the deliverable and reads as though the
    building had one."""
    assembled = assemble_scenario(
        BuildingAnalysis(scenarios=[scenario("working_day")]).scenarios[0], 1, {}, SPACES)

    assert all(m["use_type"] in {"dwelling", "communal_amenity"}
               for m in assembled["simulation"]["occupancy_multipliers"])


def test_the_state_label_travels_with_the_scenario():
    assembled = assemble_scenario(
        BuildingAnalysis(scenarios=[scenario("night_sleeping")]).scenarios[0], 1, {}, SPACES)

    assert assembled["occupancy"]["occupancy_state"] == "night_sleeping"
    assert "asleep" in assembled["occupancy"]["occupancy_state_label"]


# ---- the direction is now a property of the table, not of the reply --------------------------

def test_a_night_state_can_no_longer_be_thinner_than_a_daytime_one():
    """The fault the review loop existed for. There is no reply that produces it: the model chooses
    a state key, and every daytime state in the table sits at or below the night one for
    residential space."""
    for day_state in ("working_day", "weekend_daytime"):
        assert occupants_under(DWELLINGS, "night_sleeping") >= \
               occupants_under(DWELLINGS, day_state)


def test_an_amenity_heavy_building_is_not_flagged_for_emptying_it_at_night():
    """The real building this used to break on. communal_amenity carries a large share of the
    computed load, so a night state that closes it — which is correct — lands below the day TOTAL
    however full the dwellings are. Nothing flags it, because the direction is no longer judged by
    comparing reply against reply at all."""
    amenity_heavy = [{"guid": "a", "use_type": "dwelling", "occupant_load": 20},
                     {"guid": "b", "use_type": "communal_amenity", "occupant_load": 60}]
    night, day = scenario("night_sleeping"), scenario("working_day")

    assert occupants_under(amenity_heavy, "night_sleeping") == 20      # below the day's, correctly
    assert occupants_under(amenity_heavy, "working_day") == 36
    assert occupancy_findings(BuildingAnalysis(scenarios=[night, day]), amenity_heavy) == []


# ---- two scenarios that are really one -------------------------------------------------------

def test_the_same_state_closing_the_same_exits_is_repaired():
    duplicated = [scenario("night_sleeping"), scenario("night_sleeping")]
    varied = [scenario("night_sleeping"), scenario("working_day")]
    llm = FakeLLM([duplicated, varied])

    analysis = invoke_structured(llm, "PROMPT", review=review)

    assert len(llm.prompts) == 2
    assert "one simulation described twice" in llm.prompts[1]
    assert "night_sleeping" in llm.prompts[1]
    assert [sc.conditions.occupancy_state for sc in analysis.scenarios] == \
           ["night_sleeping", "working_day"]


def test_the_same_state_with_a_different_exit_lost_is_a_legitimate_pair():
    """The comparison a discounted-exit study exists to make: hold the population still and take an
    exit away, so the exit is the only variable. The old signature keyed on multipliers alone and
    called this a duplicate, which cost a repair attempt to un-vary something worth varying."""
    base = scenario("night_sleeping")
    degraded = scenario("night_sleeping", discounted=("Exit 1",))

    assert scenario_signature(BuildingAnalysis(scenarios=[base]).scenarios[0]) != \
           scenario_signature(BuildingAnalysis(scenarios=[degraded]).scenarios[0])
    assert occupancy_findings(BuildingAnalysis(scenarios=[base, degraded]), SPACES) == []


def test_two_different_states_are_never_duplicates():
    """Identity is the state and the exits, not the headcount. Two states stand people in different
    rooms even when their totals are close, so a near-collision is not something to repair — the old
    check flagged exact collisions and sent the model chasing an arithmetic coincidence."""
    findings = occupancy_findings(
        BuildingAnalysis(scenarios=[scenario("early_morning"), scenario("weekend_daytime")]),
        SPACES)

    assert findings == []


def test_a_varied_set_passes():
    varied = BuildingAnalysis(scenarios=[
        scenario("night_sleeping"),
        scenario("night_sleeping", discounted=("Exit 1",)),
        scenario("working_day"),
        scenario("evening_communal")])

    assert occupancy_findings(varied, SPACES) == []


# ---- what happens when the model never complies ----------------------------------------------

def test_an_unrepaired_set_is_kept_rather_than_losing_the_run():
    """The asymmetry with the schema retry: findings are reported downstream, so a flawed set is
    still worth having. Raising here would throw away a call that costs minutes."""
    llm = FakeLLM([[scenario("night_sleeping"), scenario("night_sleeping")]])

    analysis = invoke_structured(llm, "PROMPT", attempts=3, review=review)

    assert len(llm.prompts) == 3                      # it did try to repair
    assert len(analysis.scenarios) == 2               # and handed back what it got


def test_the_closest_set_is_the_one_kept():
    """A model can answer a repair by breaking something else. What comes back is the best attempt,
    not merely the last."""
    one_fault = [scenario("night_sleeping"), scenario("night_sleeping"), scenario("working_day")]
    two_faults = [scenario("night_sleeping"), scenario("night_sleeping"),
                  scenario("working_day"), scenario("working_day")]
    llm = FakeLLM([one_fault, two_faults])            # second attempt is worse

    analysis = invoke_structured(llm, "PROMPT", attempts=2, review=review)

    assert len(analysis.scenarios) == 3


def test_no_review_leaves_the_old_behaviour_alone():
    """Callers that pass no review -- and every existing test -- see exactly what they saw before."""
    llm = FakeLLM([[scenario("night_sleeping")]])

    analysis = invoke_structured(llm, "PROMPT")

    assert len(llm.prompts) == 1
    assert total_of(analysis.scenarios[0]) == 74
