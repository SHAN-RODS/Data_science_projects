"""The occupancy states are a table, and the table is what makes the night/day direction safe.

The multipliers used to come back from the model, one set per scenario, and getting them wrong was
the single most common reason a generation run had to be repaired: the model was choosing numbers
whose effect it could not see, because the headcount they produce is computed downstream and it is
told not to state one. The direction it got wrong most often is the one that matters — in a building
people sleep in, the night is the busy state, and it kept filling the dwellings by day instead.

Moving the multipliers into a table turns that from a fault a reply can commit into one the data
makes unreachable. The night state holds every residential use type at 1.0 — the schema maximum —
so no daytime state can exceed it, on any building, whatever its use mix. That is proved here, once,
instead of by a repair attempt on every run.
"""

import pytest

from core_backend.occupancy_states import (check_states, multiplier_map, multipliers_for,
                                           occupancy_states, occupants_under, period_of,
                                           residential_occupants, residential_use_types,
                                           state_keys, state_menu, use_type_families)

# A block of flats with a large shared amenity — the shape that broke the old total-based check.
BUILDING = [{"guid": "a", "use_type": "dwelling", "occupant_load": 27},
            {"guid": "b", "use_type": "communal_amenity", "occupant_load": 26},
            {"guid": "c", "use_type": "gym", "occupant_load": 9},
            {"guid": "d", "use_type": "commercial", "occupant_load": 7},
            {"guid": "e", "use_type": "circulation", "occupant_load": 0}]


# ---- the table's own invariants ---------------------------------------------------------------

def test_the_table_is_internally_consistent():
    """check_states() runs at import, so a broken table cannot even be loaded. This is the same
    check as an assertion rather than an exception, so a failure names what is wrong."""
    assert check_states() == []


def test_no_daytime_state_can_hold_more_residents_than_the_night():
    """The invariant that replaced the retry loop. It is asserted over the multipliers themselves,
    not over a building, so it holds for every building rather than the ones we happened to test."""
    night = next(k for k, v in occupancy_states.items() if v["period"] == "night")
    night_values = multiplier_map(night)

    for key, entry in occupancy_states.items():
        if entry["period"] != "day":
            continue
        for use_type in sorted(residential_use_types):
            assert multiplier_map(key)[use_type] <= night_values[use_type], (
                f"{key} holds more {use_type} than {night}")


def test_the_night_state_sits_at_the_schema_ceiling_for_residential_space():
    """Why the invariant above cannot be broken by adding a state: 1.0 is the maximum the schema
    allows, so there is no value a new daytime state could take that would exceed it."""
    night = next(k for k, v in occupancy_states.items() if v["period"] == "night")

    for use_type in residential_use_types:
        assert multiplier_map(night)[use_type] == 1.0


def test_exactly_one_state_is_the_night():
    assert [k for k, v in occupancy_states.items() if v["period"] == "night"] == ["night_sleeping"]


def test_every_multiplier_is_within_the_schema_bounds():
    for key in state_keys():
        for use_type, value in multiplier_map(key).items():
            assert 0.0 <= value <= 1.0, f"{key}/{use_type} = {value}"


def test_no_two_states_are_the_same_scenario():
    """Two states with identical multipliers would seed identical rooms — a menu with a duplicate
    on it invites the model to pick the same run twice while believing it varied something."""
    signatures = [tuple(sorted(multiplier_map(key).items())) for key in state_keys()]

    assert len(set(signatures)) == len(signatures)


def test_every_occupiable_use_type_has_a_family():
    """A use type missing from the table would silently default to 1.0 everywhere — untouched by
    every state, so it would carry the same occupants at 01:00 as at 14:00."""
    from core_backend.occupancy import non_occupable, occupancy_load_factors

    for use_type in occupancy_load_factors:
        if use_type in non_occupable:
            continue
        assert use_type in use_type_families, f"{use_type} takes no part in any occupancy state"


def test_a_sauna_is_amenity_not_sleeping_space():
    """Nobody sleeps in a sauna. Counting it as sleeping space made a night state that correctly
    closes it read as thinner than a weekend afternoon, which is the inversion this table exists to
    make impossible."""
    assert use_type_families["sauna"] == "amenity"
    assert "sauna" not in residential_use_types


# ---- what a state does to a building ----------------------------------------------------------

def test_the_night_state_fills_the_dwellings_and_empties_the_amenity():
    assert residential_occupants(BUILDING, "night_sleeping") == 27
    assert occupants_under(BUILDING, "night_sleeping") == 27      # amenity, gym, commercial all 0


def test_the_working_day_thins_the_dwellings_and_fills_the_workplace():
    assert residential_occupants(BUILDING, "working_day") == 8     # 27 * 0.3
    assert occupants_under(BUILDING, "working_day") == 8 + 13 + 4 + 7


def test_a_building_whose_amenity_dominates_still_has_the_direction_right():
    """The regression the old check could not satisfy. The night TOTAL falls below the day's here,
    correctly — every sane night state closes the amenity. Residential occupancy is what the
    direction actually claims, and it moves the right way."""
    amenity_heavy = [{"guid": "a", "use_type": "dwelling", "occupant_load": 20},
                     {"guid": "b", "use_type": "communal_amenity", "occupant_load": 60}]

    assert occupants_under(amenity_heavy, "night_sleeping") < \
           occupants_under(amenity_heavy, "working_day")
    assert residential_occupants(amenity_heavy, "night_sleeping") > \
           residential_occupants(amenity_heavy, "working_day")


def test_every_state_reaches_a_different_population_on_a_mixed_building():
    totals = [occupants_under(BUILDING, key) for key in state_keys()]

    assert len(set(totals)) == len(totals)


def test_only_night_and_day_are_compared_for_direction():
    """The two transitional states sit between the others by construction; comparing them says
    nothing, so they are deliberately outside the comparison."""
    assert period_of("night_sleeping") == "night"
    assert period_of("working_day") == "day"
    assert period_of("evening_communal") == "transitional"
    assert period_of("never_heard_of_it") is None


# ---- the shape the scenario object carries ----------------------------------------------------

def test_multipliers_come_out_in_the_shape_the_object_stores():
    """placement, validation and the export all read occupancy_multipliers as a list of records.
    The state supplies exactly that, so nothing downstream had to change when the author did."""
    records = multipliers_for("night_sleeping")

    assert all(set(r) == {"use_type", "multiplier", "reason"} for r in records)
    assert {r["use_type"] for r in records} == set(use_type_families)


def test_every_multiplier_carries_a_stated_reason():
    """Every derived value in this project carries its basis. A code-authored multiplier is held to
    the same rule as the AI-authored one it replaced — more firmly, since the reason is now written
    once and reviewed rather than regenerated per run."""
    for key in state_keys():
        for record in multipliers_for(key):
            assert record["reason"].strip()


def test_the_list_narrows_to_the_use_types_the_building_actually_has():
    records = multipliers_for("working_day", {"dwelling", "commercial"})

    assert {r["use_type"] for r in records} == {"dwelling", "commercial"}


@pytest.mark.parametrize("key", state_keys())
def test_the_menu_shows_the_model_what_each_state_costs(key):
    """The fix for the model choosing blind: it used to set multipliers without ever being shown
    the headcount they produce. The menu carries that number for this building."""
    menu = state_menu(BUILDING)

    assert key in menu
    assert f"Occupants under this state: {occupants_under(BUILDING, key)}." in menu


def test_the_menu_works_without_a_building():
    assert "night_sleeping" in state_menu()
    assert "Occupants under this state" not in state_menu()
