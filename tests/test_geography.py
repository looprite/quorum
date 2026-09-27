"""Geography selection: whole values, two levels, nothing silently ignored.

No network, no database — the ICP geography test is a pure comparison and this
is where it is pinned.
"""

from __future__ import annotations

import psycopg
import pytest

from quorum import geography
from quorum.weekly.coverage import meets_profile
from quorum.weekly.run import run_weekly

from tests.test_import_and_weekly import ACCOUNT, _cfg, _import

PROFILE = {
    "employee_count_min": 50,
    "employee_count_max": 500,
    "hq_geographies": ["North America"],
    "focus_seniority": ["vp"],
}


# --- The bug the whole-value comparison removes ---------------------------- #


def test_a_country_is_not_matched_inside_another_country():
    """The reason the old country list carried ' us' with a leading space: it
    substring-matched a joined 'city, state, country' string, so 'us' matched
    inside 'Australia' and every Australian company read as North American.
    Whole values remove the class of bug, not just this instance."""
    na = geography.parse_selections(["North America"])

    assert geography.matches(na, "United States") is True
    assert geography.matches(na, "Australia") is False
    assert geography.matches(na, "Austria") is False


@pytest.mark.parametrize(
    "value", ["United States", "united states", "  USA ", "US", "U.S.", "usa"],
)
def test_the_common_aliases_all_resolve(value):
    assert geography.matches(geography.parse_selections(["North America"]), value)


def test_a_country_the_org_holds_but_no_region_lists_is_simply_outside():
    """CRM data is never refused — only configuration is. An unrecognised
    country is outside the selection, which is an answer."""
    assert geography.matches(geography.parse_selections(["EMEA"]), "Atlantis") is False


@pytest.mark.parametrize(
    "region, country",
    [
        ("EMEA", "Germany"), ("EMEA", "United Kingdom"), ("EMEA", "Saudi Arabia"),
        ("EMEA", "South Africa"), ("APAC", "Australia"), ("APAC", "Japan"),
        ("APAC", "Singapore"), ("North America", "Canada"),
    ],
)
def test_each_region_covers_its_countries(region, country):
    assert geography.matches(geography.parse_selections([region]), country)


def test_regions_do_not_overlap():
    for region in geography.REGIONS:
        others = [r for r in geography.REGIONS if r != region]
        mine = set(geography.REGIONS[region]["countries"])
        for other in others:
            assert not (mine & set(geography.REGIONS[other]["countries"])), (
                f"{region} and {other} both claim a country"
            )


# --- Levels ----------------------------------------------------------------- #


def test_a_bare_string_is_a_region():
    """A real profile can hold ['North America']. Rewriting someone's
    configuration to add a key they did not type is not a migration worth
    doing."""
    assert geography.parse_selections(["North America"]) == [
        {"level": "region", "value": "north america"}
    ]


def test_a_country_selection_matches_only_that_country():
    sel = geography.parse_selections([{"level": "country", "value": "Germany"}])

    assert geography.matches(sel, "Germany") is True
    assert geography.matches(sel, "France") is False       # same region, not selected


def test_selections_combine():
    sel = geography.parse_selections(["North America", {"level": "country", "value": "uk"}])

    assert geography.matches(sel, "Canada") is True
    assert geography.matches(sel, "England") is True       # an alias of the country
    assert geography.matches(sel, "France") is False
    assert geography.label(sel) == "NA/United Kingdom"


@pytest.mark.parametrize(
    "raw, message",
    [
        (["Atlantis"], "Unknown region"),
        ([{"level": "region", "value": "LATAM"}], "Unknown region"),
        ([{"level": "country", "value": "Freedonia"}], "Unknown country"),
        # Deliberately not supported: matching these needs name normalisation
        # the ICP test does not do.
        ([{"level": "state", "value": "California"}], "not one of"),
        ([{"level": "city", "value": "Washington"}], "not one of"),
        ([{"level": "region"}], "needs a value"),
        ([42], "not a geography selection"),
    ],
)
def test_configuration_this_cannot_act_on_is_refused(raw, message):
    with pytest.raises(geography.GeographyError, match=message):
        geography.parse_selections(raw)


# --- Through the ICP test --------------------------------------------------- #


@pytest.mark.parametrize(
    "employees, country, expected",
    [
        (300, "United States", (True, "")),
        (300, "Australia", (False, "HQ not NA")),
        (300, "", (False, "HQ unknown")),
        (300, None, (False, "HQ unknown")),
        (15, "United Kingdom", (False, "<50 emp; HQ not NA")),
        (None, "", (False, "no size; HQ unknown")),
        (900, "Canada", (False, ">500 emp")),
    ],
)
def test_the_icp_test_reads_the_country_on_its_own(employees, country, expected):
    assert meets_profile(PROFILE, employees, country) == expected


def test_the_icp_test_does_not_judge_data_it_never_fetched():
    """The third state. Identical inputs, two different answers.

    Empty firmographics because the CRM holds none is a finding about the
    company — "no size; HQ unknown". Empty firmographics because no CRM was
    ever called is not a finding at all, and returning False for it states a
    verdict about data nobody looked up. Worse, this test is also the filter
    feeding tab 3, so a False here silently empties the stakeholder map.
    """
    from quorum.weekly.coverage import NOT_ASSESSED

    fetched = meets_profile(PROFILE, None, "")
    never_fetched = meets_profile(PROFILE, None, "", firmographics_fetched=False)

    assert fetched == (False, "no size; HQ unknown")
    assert never_fetched == (None, NOT_ASSESSED)
    # None is not False: a caller testing truthiness alone must not be able to
    # read "not assessed" as "does not meet the profile".
    assert never_fetched[0] is None


def test_hq_unknown_is_not_the_same_answer_as_hq_outside():
    """One is missing CRM data, the other is a decision the profile made. They
    read identically before this, and the reader could not tell which."""
    _, missing = meets_profile(PROFILE, 300, "")
    _, outside = meets_profile(PROFILE, 300, "Australia")

    assert missing == "HQ unknown"
    assert outside != missing


def test_the_reason_names_the_selection_that_was_missed():
    emea = {**PROFILE, "hq_geographies": ["EMEA"]}

    assert meets_profile(emea, 300, "United States")[1] == "HQ not EMEA"
    assert meets_profile(
        {**PROFILE, "hq_geographies": [{"level": "country", "value": "japan"}]},
        300, "United States",
    )[1] == "HQ not Japan"


# --- Before the run starts -------------------------------------------------- #


def test_the_run_refuses_a_profile_whose_geography_it_cannot_act_on(
    database, gong_calls, tmp_path
):
    """Before this, a profile naming a region the test did not know applied no
    geography filter at all: every company met passed the ICP test, and the
    workbook looked entirely normal."""
    with psycopg.connect(database, autocommit=True) as conn:
        account_id = str(conn.execute(
            "insert into accounts (name, internal_domains) values (%s, %s) returning id",
            (ACCOUNT, ["northwind.com"]),
        ).fetchone()[0])
        conn.execute(
            "insert into user_focus_profiles (account_id, version_number, is_active, "
            'profile_data) values (%s, 1, true, \'{"hq_geographies": ["LATAM"], '
            '"employee_count_min": 50, "employee_count_max": 500}\'::jsonb)',
            (account_id,),
        )
    _import(database, account_id, gong_calls)

    with pytest.raises(geography.GeographyError, match="Unknown region"):
        run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    assert list(tmp_path.iterdir()) == []
