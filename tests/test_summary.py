"""The Summary tab's counts: each against its denominator, each only when the
source it counts was configured.

No real Config(): it reads the environment, and a failing assertion can put a
live credential in a traceback. Stubs throughout; names are invented.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from quorum.crm.contact import Contact
from quorum.crm.fieldmap import NOT_CHECKED
from quorum.weekly import stakeholders as stakeholders_mod
from quorum.weekly import summary as summary_mod
from quorum.weekly.stakeholders import ICP_NOT_ASSESSED, NO_SENIOR_CONTACT

PROFILE = {"focus_seniority": ["vp", "c-level"]}


def _cfg(sf=True, hs=False, shortlist=3):
    return SimpleNamespace(
        salesforce=SimpleNamespace(configured=sf),
        hubspot=SimpleNamespace(configured=hs),
        recent_days=90,
        group_call_min=8,
        shortlist_size=shortlist,
    )


def _by_key(stats):
    return {s["key"]: (s["count"], s["out_of"]) for s in stats}


COVERAGE = [
    {"domain": "a.example", "assessed": True, "is_target": True},
    {"domain": "b.example", "assessed": True, "is_target": True},
    {"domain": "c.example", "assessed": True, "is_target": False},
]

# c.example had recent senior contact but is not a target: it must not count.
BENCH_RAW = [
    {"domain": "a.example", "any_recent_contact": True},
    {"domain": "b.example", "any_recent_contact": False},
    {"domain": "c.example", "any_recent_contact": True},
]

RECONCILED = [
    {"in_salesforce": True, "in_hubspot": None, "title": "VP Sales",
     "linkedin_in_crm": True, "mobile_in_crm": True},
    {"in_salesforce": True, "in_hubspot": None, "title": "",
     "linkedin_in_crm": False, "mobile_in_crm": False},
    {"in_salesforce": False, "in_hubspot": None, "title": "",
     "linkedin_in_crm": False, "mobile_in_crm": False, "other_found": True},
    # A shared inbox is never looked up, so it has no `other_found`.
    {"in_salesforce": False, "in_hubspot": None, "title": "",
     "linkedin_in_crm": False, "mobile_in_crm": False},
    {"in_salesforce": False, "in_hubspot": None, "title": "",
     "linkedin_in_crm": False, "mobile_in_crm": False, "other_found": False},
]

STAKEHOLDERS = [
    {"domain": "a.example", "name": "Robin Hale", "title": "VP Sales",
     "linkedin": "https://www.linkedin.com/in/robin-hale", "mobile": "yes"},
    {"domain": "a.example", "name": "Casey Morrow", "title": "",
     "linkedin": "", "mobile": "no"},
    # A stated gap is a row on the tab, not a person on the list.
    {"domain": "b.example", "name": NO_SENIOR_CONTACT, "title": "", "linkedin": "",
     "mobile": ""},
]


def test_every_count_comes_with_what_it_is_out_of():
    stats = summary_mod.build(
        _cfg(), RECONCILED, COVERAGE, STAKEHOLDERS, BENCH_RAW, PROFILE,
        enrichment="Example",
        queue=[{"kind": "May have left"}],
        queue_kinds=("Profile fit disputed", "May have left"),
    )

    assert _by_key(stats) == {
        "companies_met": (3, None),
        "companies_fit": (2, 3),
        "companies_fit_recent_senior": (1, 2),
        "people_met": (5, None),
        "people_not_in_crm": (3, 5),
        "people_not_in_crm_found": (1, 2),
        "people_in_crm": (2, 5),
        "people_in_crm_no_title": (1, 2),
        "people_in_crm_no_linkedin": (1, 2),
        "people_in_crm_no_mobile": (1, 2),
        "stakeholders": (2, None),
        "stakeholders_no_title": (1, 2),
        "stakeholders_no_linkedin": (1, 2),
        "stakeholders_no_mobile": (1, 2),
        # Zeros stay, so a kind going to zero week on week can be seen.
        "queue:Profile fit disputed": (0, None),
        "queue:May have left": (1, None),
    }
    # The recent-contact line states the profile's own terms.
    what = next(s["what"] for s in stats if s["key"] == "companies_fit_recent_senior")
    assert "VP or C-suite level" in what and "last 90 days" in what
    # The area is the tab a reader checks the count against.
    assert {s["area"] for s in stats} == {
        "Company coverage", "Met this week", "Stakeholder list", "Review queue",
    }


def test_no_crm_counts_nothing_a_crm_would_have_answered():
    """With no CRM, "0 not in your CRM" would read as "everyone is in it"."""
    coverage = [{"domain": "a.example", "assessed": False, "is_target": False}]
    reconciled = [
        {"in_salesforce": None, "in_hubspot": None, "title": "",
         "linkedin_in_crm": NOT_CHECKED, "mobile_in_crm": NOT_CHECKED},
    ]
    stakeholders = [{"domain": "a.example", "name": ICP_NOT_ASSESSED}]

    stats = summary_mod.build(
        _cfg(sf=False), reconciled, coverage, stakeholders, [], PROFILE
    )

    assert _by_key(stats) == {
        "companies_met": (1, None),
        "companies_not_assessed": (1, 1),
        "people_met": (1, None),
    }


def test_one_crm_without_a_linkedin_field_or_a_bench_omits_those_lines():
    """HubSpot alone: the LinkedIn presence column reads "not checked" and
    there is no Salesforce bench, so neither count is offered."""
    reconciled = [
        {"in_salesforce": None, "in_hubspot": True, "title": "CRO",
         "linkedin_in_crm": NOT_CHECKED, "mobile_in_crm": False},
    ]
    stats = _by_key(
        summary_mod.build(
            _cfg(sf=False, hs=True), reconciled, COVERAGE, [], [], PROFILE
        )
    )

    assert "people_in_crm_no_linkedin" not in stats
    assert "companies_fit_recent_senior" not in stats
    assert stats["people_in_crm_no_mobile"] == (1, 1)
    # No provider configured: no provider line and no review queue.
    assert "people_not_in_crm_found" not in stats
    assert not any(k.startswith("queue:") for k in stats)


def test_recent_contact_reads_the_whole_bench_not_the_capped_list():
    """With a cap of one, the CEO is listed and the VP is not — but the VP was
    contacted last week, and that is what the summary asks about."""
    today = dt.date.today().isoformat()

    class _SF:
        configured = True

        def senior_bench(self, domain, terms):
            return [
                Contact(name="Robin Hale", title="CEO", email="r@a.example"),
                Contact(name="Casey Morrow", title="VP Sales", email="c@a.example",
                        last_activity=today),
            ]

    cfg = _cfg(shortlist=1)
    coverage = [{"domain": "a.example", "name": "A", "assessed": True, "is_target": True}]
    rows, raw = stakeholders_mod.build(cfg, coverage, [], {}, _SF())

    assert [r["name"] for r in rows] == ["Robin Hale"]
    assert raw[0]["any_recent_contact"] is True
    stats = _by_key(summary_mod.build(cfg, [], coverage, rows, raw, PROFILE))
    assert stats["companies_fit_recent_senior"] == (1, 1)


def test_a_company_with_no_senior_contact_has_had_no_recent_one():
    class _SF:
        configured = True

        def senior_bench(self, domain, terms):
            return []

    coverage = [{"domain": "a.example", "name": "A", "assessed": True, "is_target": True}]
    _, raw = stakeholders_mod.build(_cfg(), coverage, [], {}, _SF())

    assert raw[0]["any_recent_contact"] is False


def test_the_file_says_null_when_no_provider_was_configured(tmp_path):
    """Null, not missing: a missing key reads the same as a file from before
    the key existed, and a delivery step needs to tell the two apart."""
    import json

    path = tmp_path / "summary_2026-08-17.json"
    summary_mod.write(str(path), "2026-08-17", [], enrichment=None, recent_days=90)
    data = json.loads(path.read_text())
    assert "enrichment_provider" in data
    assert data["enrichment_provider"] is None
    assert data["recent_days"] == 90

    summary_mod.write(str(path), "2026-08-17", [], enrichment="Example", recent_days=30)
    data = json.loads(path.read_text())
    assert data["enrichment_provider"] == "Example"
    assert data["recent_days"] == 30
    assert data["schema"] == 1
