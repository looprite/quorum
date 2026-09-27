"""The CRM field map: resolved from the org, never hardcoded.

No network. The describe payload below is synthetic, but every awkward field in
it is a shape observed in a real Salesforce org during this work — a double
labelled '20% of Employees', a picklist headcount, a *Code twin holding 'US'
where its sibling holds 'United States', a LeanData status field whose label is
'State Info', and a LinkedIn field that is better populated than the one the
pipeline wants because it holds the *company's* page. A customer's actual field
list is not committed here: the repository holds patterns, never names.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import psycopg
import pytest

from quorum import bootstrap, db
from quorum.config import SalesforceConfig
from quorum.crm.fieldmap import (
    NOT_AVAILABLE,
    SPECS,
    FieldMap,
    FieldMapError,
    candidates,
    describe_lines,
    resolve,
)
from quorum.crm.contact import Contact
from quorum.crm.salesforce import Salesforce
from quorum.weekly.run import MissingFieldMap, run_weekly

from tests.conftest import crm_config as _crm_cfg
from tests.test_import_and_weekly import ACCOUNT, _cfg, _import


def f(name, label, type_="string", filterable=True):
    return {"name": name, "label": label, "type": type_, "filterable": filterable}


ACCOUNT_FIELDS = [
    f("Name", "Account Name"),
    f("Type", "Account Type", "picklist"),
    f("NumberOfEmployees", "Employees", "int"),
    f("Pkg__Employee_Count__c", "Pkg Employee Count", "double"),
    f("Pkg__Employee_Range__c", "Pkg Employee Range"),          # a band, not a count
    f("Headcount_Bracket__c", "Headcount Bracket", "picklist"), # ditto, and a picklist
    f("Employee_Count__c", "Employee Count", "picklist"),       # a count in name only
    f("X20_of_Employees__c", "20% of Employees", "double"),     # a fraction of one
    f("BillingCountry", "Billing Country"),
    f("BillingCountryCode", "Billing Country Code", "picklist"),  # 'US', not 'United States'
    f("ShippingCountry", "Shipping Country"),                     # where goods go
    f("Pkg__Company_Country__c", "Pkg Company Country"),
    f("BillingCity", "Billing City"),
    f("ShippingCity", "Shipping City"),
    f("Pkg__Company_City__c", "Pkg Company City"),
    f("BillingState", "Billing State/Province"),
    f("BillingStateCode", "Billing State/Province Code", "picklist"),
    f("Vendor__Status_Info__c", "State Info"),                  # a status, not a state
    f("Value_Statements__c", "Value Statements", "textarea"),   # contains 'state'
    f("Pkg__Company_State__c", "Pkg Company State"),
]

CONTACT_FIELDS = [
    f("Email", "Email", "email"),
    f("Pkg__Linkedin_Url__c", "Pkg LinkedIn URL", "url"),
    f("Linkedin_Profile__c", "Linkedin Profile", "url"),
    f("Pkg__Company_LinkedIn_URL__c", "Pkg Company LinkedIn URL", "url"),
    f("vendorlinkedinbio__c", "Vendor: Linkedin Bio", "url"),
    f("Linkedin_Location__c", "Linkedin Location", "textarea"),
    f("Using_LinkedIn__c", "Using LinkedIn", "boolean"),
    f("Linkedin_Notes__c", "Linkedin Notes", "string", False),  # cannot be counted
]

# How many rows have each field populated. The numbers are the org-wide shape
# measured on one real org: standard wins on employees, the package on
# city/state, and the two country fields are all but tied.
POPULATED = {
    ("Account", "NumberOfEmployees"): 141290,
    ("Account", "Pkg__Employee_Count__c"): 111715,
    ("Account", "BillingCountry"): 109165,
    ("Account", "Pkg__Company_Country__c"): 109533,
    ("Account", "BillingCity"): 96987,
    ("Account", "Pkg__Company_City__c"): 107750,
    ("Account", "BillingState"): 78157,
    ("Account", "Pkg__Company_State__c"): 107609,
    ("Contact", "Pkg__Linkedin_Url__c"): 331618,
    ("Contact", "Linkedin_Profile__c"): 125735,
    # The best-populated /linkedin/ field in the org, and the wrong one.
    ("Contact", "Pkg__Company_LinkedIn_URL__c"): 504882,
}
TOTALS = {"Account": 150361, "Contact": 835912}


class FakeOrg:
    """Stands in for Salesforce with the two methods the resolver calls."""

    def __init__(self, fields=None, populated=None):
        self._fields = fields or {"Account": ACCOUNT_FIELDS, "Contact": CONTACT_FIELDS}
        self._populated = POPULATED if populated is None else populated
        self.counted: list[str] = []

    def describe(self, sobject):
        return {"fields": self._fields.get(sobject, [])}

    def count(self, sobject, where=None):
        if where is None:
            return TOTALS[sobject]
        self.counted.append(where)
        name = where.split(" ")[0]
        return self._populated.get((sobject, name), 0)


# --- Matching: what is a candidate, and what is not ------------------------ #


def _spec(sobject, logical):
    return next(s for s in SPECS[sobject] if s.logical == logical)


@pytest.mark.parametrize(
    "sobject, logical, fields, expected",
    [
        ("Account", "employee_count", ACCOUNT_FIELDS,
         ["NumberOfEmployees", "Pkg__Employee_Count__c"]),
        ("Account", "hq_country", ACCOUNT_FIELDS,
         ["BillingCountry", "Pkg__Company_Country__c"]),
        ("Account", "hq_city", ACCOUNT_FIELDS,
         ["BillingCity", "Pkg__Company_City__c"]),
        ("Account", "hq_state", ACCOUNT_FIELDS,
         ["BillingState", "Pkg__Company_State__c"]),
        ("Contact", "linkedin_url", CONTACT_FIELDS,
         ["Pkg__Linkedin_Url__c", "Linkedin_Profile__c"]),
    ],
)
def test_only_the_right_fields_are_candidates(sobject, logical, fields, expected):
    kept, _ = candidates(fields, _spec(sobject, logical))

    assert [k["name"] for k in kept] == expected


@pytest.mark.parametrize(
    "sobject, logical, field, why",
    [
        # A percentage of headcount is not headcount.
        ("Account", "employee_count", "X20_of_Employees__c", "excluded by pattern"),
        # A band is not a number, twice over.
        ("Account", "employee_count", "Pkg__Employee_Range__c", "excluded by pattern"),
        ("Account", "employee_count", "Headcount_Bracket__c", "excluded by pattern"),
        # Nothing wrong with the name — a picklist headcount is a band.
        ("Account", "employee_count", "Employee_Count__c", "type picklist cannot hold this"),
        # 'US' and 'United States' are different value spaces.
        ("Account", "hq_country", "BillingCountryCode", "excluded by pattern"),
        # Where goods are sent is not a head office.
        ("Account", "hq_country", "ShippingCountry", "excluded by pattern"),
        # A status labelled 'State Info' is not a state.
        ("Account", "hq_state", "Vendor__Status_Info__c", "excluded by pattern"),
        # THE trap: the best-populated /linkedin/ field in the org is the
        # company's page. Counting alone would choose it.
        ("Contact", "linkedin_url", "Pkg__Company_LinkedIn_URL__c", "excluded by pattern"),
        ("Contact", "linkedin_url", "vendorlinkedinbio__c", "excluded by pattern"),
        ("Contact", "linkedin_url", "Linkedin_Location__c", "excluded by pattern"),
        ("Contact", "linkedin_url", "Using_LinkedIn__c", "excluded by pattern"),
        # What cannot be counted cannot be ranked by what has data.
        ("Contact", "linkedin_url", "Linkedin_Notes__c", "not filterable"),
    ],
)
def test_rejections_are_recorded_with_the_rule_that_made_them(sobject, logical, field, why):
    fields = ACCOUNT_FIELDS if sobject == "Account" else CONTACT_FIELDS
    _, rejected = candidates(fields, _spec(sobject, logical))

    assert {r["field"]: r["why"] for r in rejected}[field] == why


def test_a_field_that_matches_nothing_is_not_reported_as_rejected():
    """Rejections are for near-misses. Listing all 455 fields as 'rejected'
    would bury the three that were actually considered and turned down."""
    _, rejected = candidates(ACCOUNT_FIELDS, _spec("Account", "employee_count"))

    assert "BillingCity" not in {r["field"] for r in rejected}


# --- Resolution: ordered by what actually has data ------------------------- #


def test_resolution_orders_by_populated_rows():
    field_map, _ = resolve(FakeOrg(), log=lambda *_: None)

    assert field_map["Account"]["employee_count"] == [
        "NumberOfEmployees", "Pkg__Employee_Count__c",       # 94.0% then 74.3%
    ]
    assert field_map["Account"]["hq_country"] == [
        "Pkg__Company_Country__c", "BillingCountry",         # 72.8% then 72.6%
    ]
    assert field_map["Account"]["hq_city"] == [
        "Pkg__Company_City__c", "BillingCity",               # 71.7% then 64.5%
    ]
    assert field_map["Account"]["hq_state"] == [
        "Pkg__Company_State__c", "BillingState",             # 71.6% then 52.0%
    ]
    assert field_map["Contact"]["linkedin_url"] == [
        "Pkg__Linkedin_Url__c", "Linkedin_Profile__c",       # 39.7% then 15.0%
    ]


def test_provenance_records_the_counts_that_justified_the_choice():
    _, prov = resolve(FakeOrg(), log=lambda *_: None)
    emp = prov["Account"]["fields"]["employee_count"]

    assert prov["Account"]["total_rows"] == 150361
    assert emp["candidates"][0] == {
        "field": "NumberOfEmployees", "type": "int",
        "populated": 141290, "pct": 94.0,
    }
    assert emp["required"] is True
    assert "ICP employee band" in emp["serves"]
    assert "X20_of_Employees__c" in {r["field"] for r in emp["rejected"]}


def test_a_required_field_that_resolves_to_nothing_stops_the_resolution():
    """The failure it prevents: with no headcount field the ICP employee band
    cannot be applied, and every company would be reported as a fit."""
    org = FakeOrg(fields={"Account": [f("Name", "Account Name")], "Contact": []})

    with pytest.raises(FieldMapError, match="employee_count"):
        resolve(org, log=lambda *_: None)


def test_an_optional_field_that_resolves_to_nothing_does_not():
    """LinkedIn is the one logical field with no standard equivalent. An org
    without it gets a column that says so, not a failed run."""
    org = FakeOrg(fields={"Account": ACCOUNT_FIELDS, "Contact": [f("Email", "Email")]})

    field_map, _ = resolve(org, log=lambda *_: None)

    assert field_map["Contact"]["linkedin_url"] == []
    assert FieldMap(field_map).available("Contact", "linkedin_url") is False
    assert describe_lines(field_map)[-1] == f"Contact.linkedin_url: {NOT_AVAILABLE}"


def test_counting_is_a_real_aggregate_per_candidate():
    org = FakeOrg()
    resolve(org, log=lambda *_: None)

    assert "NumberOfEmployees != null" in org.counted
    assert len(org.counted) == 10  # every candidate on both objects, and no more


# --- Reading through the map ------------------------------------------------ #


def test_the_first_populated_candidate_wins():
    """Why the map holds a list and not a winner. Measured on one real org,
    three of thirty-nine companies met in a week have an empty package country
    and a populated BillingCountry; a single-name map blanks their HQ."""
    fm = FieldMap({"Account": {"hq_country": ["Pkg__Company_Country__c", "BillingCountry"]}})

    assert fm.value({"Pkg__Company_Country__c": "Austria"}, "Account", "hq_country") == "Austria"
    assert fm.value(
        {"Pkg__Company_Country__c": None, "BillingCountry": "United States"},
        "Account", "hq_country",
    ) == "United States"
    assert fm.value({"BillingCountry": "   "}, "Account", "hq_country") == ""
    assert FieldMap({"Account": {"employee_count": ["N"]}}).value(
        {"N": 377}, "Account", "employee_count"
    ) == 377
    assert fm.value(None, "Account", "hq_country") == ""


def test_the_select_list_is_the_standard_fields_plus_the_resolved_ones():
    fm = FieldMap({"Account": {"employee_count": ["NumberOfEmployees", "Pkg__E__c"]}})

    # NumberOfEmployees is both standard and resolved; SOQL will not take it twice.
    assert fm.select("Account", ["Name", "Type", "NumberOfEmployees"]) == (
        "Name, Type, NumberOfEmployees, Pkg__E__c"
    )


def test_an_empty_map_still_produces_a_valid_query():
    """A run with no Salesforce configured reads standard fields only."""
    assert FieldMap().select("Account", ["Name", "Type"]) == "Name, Type"
    assert FieldMap().value({"x": 1}, "Account", "hq_city") == ""


def test_salesforce_queries_are_built_from_the_map():
    """The whole point: not one non-standard API name is written in the code."""
    sf = Salesforce(_crm_cfg(), FieldMap({
        "Account": {"employee_count": ["Pkg__E__c"], "hq_country": ["Pkg__C__c"]},
        "Contact": {"linkedin_url": ["Pkg__L__c"]},
    }))
    sent: list[str] = []
    sf.query = lambda soql: sent.append(soql) or {"records": [{}]}

    sf.contact_by_email("a@b.com")
    sf.senior_bench("b.com", ["VP"])
    sf.account_firmographics("001")

    assert "Pkg__L__c" in sent[0]                      # contact_by_email
    assert "Pkg__L__c" in sent[1]                      # senior_bench
    assert "Pkg__E__c" in sent[2] and "Pkg__C__c" in sent[2]


def test_the_salesforce_module_names_no_custom_field():
    """The rule this whole change exists to enforce, as a test rather than a
    convention: a custom API name in this file is a run that dies at the next
    customer, because a field that does not exist raises INVALID_FIELD and
    kills the whole query rather than blanking a column."""
    import quorum.crm.salesforce as sf_mod

    source = open(sf_mod.__file__).read()

    assert "__c" not in source


def test_firmographics_returns_the_parts_as_well_as_the_display_string():
    sf = Salesforce(_crm_cfg(), FieldMap({"Account": {
        "employee_count": ["N__c"], "hq_city": ["City__c"],
        "hq_state": ["State__c"], "hq_country": ["Country__c"],
    }}))
    sf.query = lambda soql: {"records": [{
        "Name": "Acme", "Type": "Customer", "N__c": 300,
        "City__c": "Vienna", "State__c": "Wien", "Country__c": "Austria",
    }]}

    got = sf.account_firmographics("001")

    assert got["hq"] == "Vienna, Wien, Austria"
    assert (got["city"], got["state"], got["country"]) == ("Vienna", "Wien", "Austria")
    # Unconverted: an int headcount stays an int, or every Employees cell in
    # the workbook silently becomes text.
    assert got["employees"] == 300


# --- The LinkedIn column, when there is no LinkedIn field ------------------- #


def _sf(available: bool, configured: bool = True):
    return Salesforce(
        _crm_cfg(configured),
        FieldMap({"Contact": {"linkedin_url": ["L__c"] if available else []}}),
    )


@pytest.mark.parametrize(
    "available, contact, expected",
    [
        (True, Contact(linkedin="https://linkedin.com/in/x"), True),
        (True, Contact(linkedin=""), False),     # the field exists, this row is empty
        (True, None, False),                     # not in the CRM at all
        (False, Contact(), None),                # the CRM has no such field
    ],
)
def test_linkedin_presence_has_three_answers(available, contact, expected):
    from quorum.weekly.people import _linkedin_presence

    assert _linkedin_presence(_sf(available), contact) is expected


def test_an_unresolved_linkedin_field_says_so_in_the_cell():
    from quorum.weekly.workbook import _linkedin_cell

    assert _linkedin_cell(None) == NOT_AVAILABLE
    assert _linkedin_cell(True) == "yes"
    assert _linkedin_cell(False) == ""


# --- Storage ---------------------------------------------------------------- #


def _seed(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "insert into accounts (name, internal_domains) values (%s, %s) returning id",
            (ACCOUNT, ["northwind.com"]),
        ).fetchone()
        conn.execute(
            "insert into user_focus_profiles (account_id, version_number, is_active, "
            "profile_data) values (%s, 1, true, %s)",
            (row[0], json.dumps({
                "employee_count_min": 200, "employee_count_max": 10000,
                "hq_geographies": ["North America"],
                "focus_seniority": ["c-level", "vp", "director"],
            })),
        )
    return str(row[0])


def test_a_stored_map_is_read_back_by_the_pipeline(database):
    account_id = _seed(database)
    field_map, provenance = resolve(FakeOrg(), log=lambda *_: None)

    with psycopg.connect(database) as conn:
        result = bootstrap.install_field_map(conn, account_id, field_map, provenance)
        conn.commit()

    assert (result.action, result.version) == ("created", 1)
    with psycopg.connect(database) as conn:
        assert db.crm_field_map(conn, SimpleNamespace(account=ACCOUNT)) == field_map


def test_re_resolving_supersedes_and_keeps_the_old_version(database):
    """What last week's artifact read has to stay answerable after an admin adds
    a better-populated field."""
    account_id = _seed(database)
    first, prov = resolve(FakeOrg(), log=lambda *_: None)

    with psycopg.connect(database) as conn:
        bootstrap.install_field_map(conn, account_id, first, prov)
        second = bootstrap.install_field_map(conn, account_id, {"Account": {}}, {})
        conn.commit()

    assert (second.action, second.version) == ("replaced", 2)
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "select version_number, is_active from crm_field_maps order by version_number"
        ).fetchall() == [(1, False), (2, True)]
        assert bootstrap.has_field_map(conn, account_id) == 2


def test_the_run_refuses_to_start_with_a_crm_and_no_map(database, gong_calls, tmp_path):
    """Without a map every query falls back to standard fields only, and the ICP
    test then judges companies on data that was never fetched."""
    account_id = _seed(database)
    _import(database, account_id, gong_calls)
    cfg = _cfg(
        database, tmp_path,
        # Every field given, so nothing is defaulted from the environment.
        salesforce=SalesforceConfig(
            access_token="t", instance_url="https://x",
            token_url="", client_id="", client_secret="",
        ),
    )

    with pytest.raises(MissingFieldMap, match="resolve-fields"):
        run_weekly(cfg, log=lambda *_: None)

    assert list(tmp_path.iterdir()) == []


def test_no_crm_configured_needs_no_map(database, gong_calls, tmp_path):
    """The map is only meaningful where there is an org to have described."""
    account_id = _seed(database)
    _import(database, account_id, gong_calls)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    assert paths["xlsx"]
