"""What an adapter hands back, and that `weekly/` no longer knows any CRM's
field names.

The field map made Salesforce's custom names portable; this is the other half —
the standard ones. `Title` and `MobilePhone` are spelled the same in every
Salesforce org, which is not the same as being spelled the same in every CRM.

No network: both adapters are driven through stubbed transports.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from quorum.config import HubSpotConfig
from tests.conftest import crm_config as _crm_cfg
from quorum.crm.contact import Contact
from quorum.crm.fieldmap import NOT_AVAILABLE, FieldMap
from quorum.crm.hubspot import HubSpot
from quorum.crm.salesforce import Salesforce

SF_RECORD = {
    "attributes": {"type": "Contact"},
    "Id": "003x",
    "Name": "Dana Reyes",
    "Title": "  Chief Revenue Officer  ",
    "Email": "dana@acme.com",
    "MobilePhone": "+1 555 0100",
    "LastActivityDate": "2026-08-01",
    "AccountId": "001x",
    "Pkg__Linkedin__c": "https://linkedin.com/in/dana",
}

HS_RECORD = {
    "id": "1",
    "properties": {
        "email": "dana@acme.com",
        "firstname": "Dana",
        "lastname": "Reyes",
        "jobtitle": "Chief Revenue Officer",
        "mobilephone": "+1 555 0100",
    },
}


def _sf(linkedin: bool = True) -> Salesforce:
    return Salesforce(_crm_cfg(), FieldMap(
        {"Contact": {"linkedin_url": ["Pkg__Linkedin__c"] if linkedin else []}}
    ))


def _hs() -> HubSpot:
    return HubSpot(SimpleNamespace(hubspot=HubSpotConfig(api_key="k")))


# --- Salesforce ------------------------------------------------------------- #


def test_a_salesforce_record_becomes_a_contact():
    got = _sf()._contact(SF_RECORD)

    assert got.name == "Dana Reyes"
    assert got.title == "Chief Revenue Officer"          # trimmed
    assert got.email == "dana@acme.com"
    assert got.mobile is True                            # presence, not the number
    assert got.linkedin == "https://linkedin.com/in/dana"
    assert got.last_activity == "2026-08-01"


def test_missing_fields_become_empty_rather_than_none():
    """Every caller treats these as strings. None would reach a spreadsheet
    cell as the word 'None'."""
    got = _sf()._contact({"Id": "003x"})

    assert (got.name, got.title, got.email, got.last_activity) == ("", "", "", "")
    assert got.mobile is False
    assert got.linkedin == ""            # the field exists; this row is empty


def test_linkedin_is_none_when_the_org_has_no_such_field():
    assert _sf(linkedin=False)._contact(SF_RECORD).linkedin is None


def test_the_mobile_number_never_leaves_the_adapter():
    """Presence is all the artifact needs, and `provenance` goes into the JSON
    dump verbatim — so the number has to be reduced here, not by the caller."""
    got = _sf()._contact(SF_RECORD)

    assert got.mobile is True
    assert got.provenance["MobilePhone"] is True
    assert "+1 555 0100" not in repr(got)


def test_provenance_is_otherwise_the_record_as_it_arrived():
    """The dump exists so the ranking can be re-tuned without re-querying the
    CRM, which needs more than the six fields the pipeline reads."""
    got = _sf()._contact(SF_RECORD)

    assert got.provenance == {**SF_RECORD, "MobilePhone": True}


def test_the_bench_and_the_lookup_both_return_contacts():
    sf = _sf()
    sf.query = lambda soql: {"records": [SF_RECORD]}

    assert isinstance(sf.contact_by_email("dana@acme.com"), Contact)
    assert [type(c) for c in sf.senior_bench("acme.com", ["VP"])] == [Contact]


def test_a_lookup_that_finds_nobody_is_none():
    sf = _sf()
    sf.query = lambda soql: {"records": []}

    assert sf.contact_by_email("nobody@acme.com") is None


def test_linkedin_available_answers_without_exposing_the_field_map():
    assert _sf().linkedin_available is True
    assert _sf(linkedin=False).linkedin_available is False


# --- HubSpot ---------------------------------------------------------------- #


def test_a_hubspot_record_becomes_the_same_shape():
    got = _hs()._contact(HS_RECORD)

    assert got.name == "Dana Reyes"          # firstname + lastname, joined here
    assert got.title == "Chief Revenue Officer"
    assert got.email == "dana@acme.com"
    assert got.mobile is True
    assert got.provenance["properties"]["mobilephone"] is True


def test_hubspot_reports_no_linkedin_field_rather_than_an_empty_one():
    """None is 'this CRM has no such field'. '' would claim HubSpot has one and
    this person left it blank."""
    assert _hs()._contact(HS_RECORD).linkedin is None


def test_half_a_name_is_not_padded_with_a_stray_space():
    assert _hs()._contact({"properties": {"lastname": "Reyes"}}).name == "Reyes"
    assert _hs()._contact({"properties": {}}).name == ""


# --- Reconciliation reads neither CRM's vocabulary -------------------------- #


class _Stub:
    """An adapter that answers with a Contact and nothing else."""

    def __init__(self, contact, configured=True, linkedin_available=True):
        self._contact_value = contact
        self.configured = configured
        self.linkedin_available = linkedin_available

    def contact_by_email(self, email):
        return self._contact_value


def test_salesforce_still_wins_on_title():
    from quorum.weekly.people import reconcile

    got = reconcile(
        {"email": "a@b.com", "attendee_name": "A", "flag": ""},
        _Stub(Contact(title="VP Sales")),
        _Stub(Contact(title="Head of Sales")),
    )

    assert got["title"] == "VP Sales"
    assert "title differs (SF: VP Sales / HS: Head of Sales)" in got["flag"]


def test_a_name_the_crm_holds_is_not_reported_missing():
    """The meeting source frequently has an address and no name for an attendee
    the CRM knows by name. The row already takes its *title* from that record,
    so reporting "needs name" beside it was the run calling something missing
    that it had in hand — and the same person appeared blank on tab 1 while
    correctly named on tab 3."""
    from quorum.weekly.people import reconcile

    got = reconcile(
        {"email": "m@acme.com", "attendee_name": "", "flag": "needs enrichment"},
        _Stub(Contact(name="Robin Hale", title="VP Sales")),
        _Stub(None),
    )

    assert got["attendee_name"] == "Robin Hale"
    assert "needs name" not in got["flag"]

    # Salesforce wins, HubSpot is the fallback — the same precedence as title.
    either = reconcile(
        {"email": "m@acme.com", "attendee_name": "", "flag": ""},
        _Stub(None),
        _Stub(Contact(name="From HubSpot")),
    )
    assert either["attendee_name"] == "From HubSpot"

    # And a name nobody has is still reported missing.
    nowhere = reconcile(
        {"email": "m@acme.com", "attendee_name": "", "flag": ""},
        _Stub(None),
        _Stub(None),
    )
    assert "needs name" in nowhere["flag"]


def test_needs_title_is_about_a_record_that_exists():
    """A missing title is a finding about a CRM record. Someone with no record
    is not in the CRM at all — the in-CRM column says so — and flagging
    "needs title" beside that reported a gap in a record that does not exist."""
    from quorum.weekly.people import reconcile

    person = {"email": "a@b.com", "attendee_name": "A", "flag": ""}
    no_record = reconcile(person, _Stub(None), _Stub(None, configured=False))
    assert "needs title" not in no_record["flag"]
    assert no_record["in_salesforce"] is False

    thin_record = reconcile(person, _Stub(Contact(title="")), _Stub(None, configured=False))
    assert "needs title" in thin_record["flag"]


@pytest.mark.parametrize(
    "sf_mobile, hs_mobile, expected",
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_a_mobile_in_either_crm_counts(sf_mobile, hs_mobile, expected):
    from quorum.weekly.people import reconcile

    got = reconcile(
        {"email": "a@b.com", "attendee_name": "A", "flag": ""},
        _Stub(Contact(mobile=sf_mobile)),
        _Stub(Contact(mobile=hs_mobile)),
    )

    assert got["mobile_in_crm"] is expected


def test_the_stakeholder_row_is_built_from_the_contact(tmp_path):
    from quorum.weekly.stakeholders import build

    class _Bench:
        configured = True

        def senior_bench(self, domain, terms):
            return [
                Contact(name="Dana Reyes", title="Chief Revenue Officer | Cyber",
                        email="Dana@Acme.com", mobile=True, linkedin="https://li/dana",
                        last_activity="2026-08-01", provenance={"raw": 1}),
                Contact(name="Sam Fox", title="Director of RevOps",
                        email="sam@acme.com", linkedin=None),
            ]

    cfg = SimpleNamespace(recent_days=90, group_call_min=8, shortlist_size=3)
    rows, raw = build(
        cfg, [{"domain": "acme.com", "name": "Acme", "is_target": True, "met": 1}],
        ["VP"], {}, _Bench(),
    )

    assert rows[0]["name"] == "Dana Reyes"
    assert rows[0]["title"] == "Chief Revenue Officer"     # billboard trimmed
    assert rows[0]["mobile"] == "yes"
    assert rows[0]["linkedin"] == "https://li/dana"
    assert rows[0]["_email"] == "dana@acme.com"            # lowercased for the join
    # "no", not "GAP" — a plain fact about the contact record, not a defect.
    assert rows[1]["mobile"] == "no"
    assert rows[1]["linkedin"] == NOT_AVAILABLE            # no such field here
    # The dump carries what the CRM returned, untouched by this layer.
    assert raw[0]["bench"] == [{"raw": 1}, {}]


# --- The regression guard --------------------------------------------------- #

# Names belonging to one CRM's vocabulary. Salesforce's are capitalised, so the
# check is case-sensitive: `person.get("email")` is our own key and must not trip
# it, while `record.get("Email")` must.
CRM_FIELD_NAMES = (
    "Title", "Name", "Email", "MobilePhone", "LastActivityDate", "AccountId",
    "jobtitle", "mobilephone", "firstname", "lastname", "hs_email_domain",
)
ACCESSOR = re.compile(
    r"""\.get\(\s*["'](%s)["']|\[\s*["'](%s)["']\s*\]"""
    % ("|".join(CRM_FIELD_NAMES), "|".join(CRM_FIELD_NAMES))
)


@pytest.mark.parametrize(
    "line, caught",
    [
        ('sf_title = (sfp.get("Title") or "").strip()', True),
        ('"mobile": "yes" if r.get("MobilePhone") else "GAP"', True),
        ('contact = recent_contact(cfg, h, r.get("LastActivityDate"))', True),
        ('hs_title = (hp.get("jobtitle") or "").strip()', True),
        ('name = props["firstname"]', True),
        # Our own keys, and spreadsheet column headers, stay legal.
        ('email = (person.get("email") or "").strip()', False),
        ('["Name", "Email", "Title (SF)", "LinkedIn?"]', False),
        ('if col == "Name" and v.startswith("—"):', False),
        ('out["name"] = firmo.get("name", "")', False),
    ],
)
def test_the_guard_catches_what_it_claims_to(line, caught):
    """A regression guard nobody has seen fail is a guard nobody should trust.
    Every 'True' line here is one this change actually removed."""
    assert bool(ACCESSOR.search(line)) is caught


def test_weekly_reads_no_crm_field_name():
    """The rule this change exists to enforce, as a test rather than a habit.

    Spreadsheet column headers are allowed to say "Name" and "Email" — those are
    display strings. What is banned is *reaching into a CRM record* by a name
    only that CRM uses, which is what would break the moment a second one is
    added.
    """
    weekly = pathlib.Path(__file__).resolve().parents[1] / "quorum" / "weekly"
    offenders = []
    for path in sorted(weekly.glob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ACCESSOR.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")

    assert offenders == []
