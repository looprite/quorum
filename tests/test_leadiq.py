"""The LeadIQ provider: what it asks for, what it accepts, and what it refuses.

No network. Every call goes through an injected `post`, and the responses are
invented — shaped like the real API's, holding nobody real.
"""

from __future__ import annotations

import pytest

from quorum import enrich
from quorum.enrich import Company, Person
from quorum.enrich.leadiq import (
    COMPANY_QUERY,
    PERSON_QUERY,
    PROVIDER,
    LeadIQ,
    LeadIQError,
)

KEY = "test-key-not-real"


class _Resp:
    def __init__(self, body=None, status=200):
        self._body = body if body is not None else {}
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._body


class _Post:
    """Records every call and answers from a queue of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _people(*records):
    return _Resp({"data": {"searchPeople": {"results": list(records)}}})


def _record(name, title, employer, domain, current_emails, past_emails=(), linkedin=""):
    return {
        "name": {"fullName": name},
        "linkedin": {"linkedinUrl": linkedin} if linkedin else None,
        "updatedAt": "2026-09-01T00:00:00Z",
        "currentPositions": [
            {
                "title": title,
                "companyInfo": {"name": employer, "domain": domain},
                "workEmail": {"value": current_emails[0]} if current_emails else None,
                "emails": [{"value": e} for e in current_emails],
            }
        ],
        "pastPositions": [
            {"workEmail": {"value": e}, "emails": [{"value": e}]} for e in past_emails
        ],
    }


# --- configuration ---------------------------------------------------------- #


def test_off_unless_the_key_is_set():
    assert LeadIQ.from_env({}) is None
    assert LeadIQ.from_env({"LEADIQ_API_KEY": "  "}) is None
    assert isinstance(LeadIQ.from_env({"LEADIQ_API_KEY": KEY}), LeadIQ)


def test_it_is_discovered_without_being_named_elsewhere():
    """The rest of the pipeline finds the provider through the package, so no
    file outside this provider's own needs to spell it."""
    assert PROVIDER is LeadIQ
    assert "LEADIQ_API_KEY" in enrich.env_vars()
    assert isinstance(enrich.configured({"LEADIQ_API_KEY": KEY}), LeadIQ)
    assert enrich.configured({}) is None


# --- what is asked for ------------------------------------------------------- #


def test_a_person_lookup_never_asks_for_a_phone_or_a_personal_email():
    """The map uses neither, and a phone number is the expensive field — ten
    times a record on the rate card. Asking for one by accident is how a
    one-credit lookup became eleven on a real call."""
    for query in (PERSON_QUERY, COMPANY_QUERY):
        lowered = query.lower()
        assert "phone" not in lowered
        assert "personal" not in lowered


def test_the_key_goes_in_basic_auth_and_nowhere_else():
    post = _Post(_people())
    LeadIQ(KEY, post=post).person_by_email("dana@acme.example")

    call = post.calls[0]
    assert call["auth"] == (KEY, "")
    assert KEY not in repr(call["json"])


# --- what is accepted -------------------------------------------------------- #


def test_a_record_for_someone_else_is_not_accepted():
    """Observed on a real call: a lookup by email returned a different person,
    at a different company, at the highest confidence score — and none of the
    record's emails was the one searched for. Accepted, it would report a
    stakeholder as having moved to a company they never worked at."""
    post = _Post(
        _people(_record("Someone Else", "EVP", "Other Co", "other.example",
                        ["someone@other.example"]))
    )
    assert LeadIQ(KEY, post=post).person_by_email("dana@acme.example") is None


def test_the_searched_email_on_a_current_position_is_a_match():
    post = _Post(
        _people(_record("Dana Reyes", "VP Sales", "Acme", "www.acme.example",
                        ["Dana@Acme.example"], linkedin="https://www.linkedin.com/in/dana"))
    )
    got = LeadIQ(KEY, post=post).person_by_email("dana@acme.example")

    assert got == Person(
        name="Dana Reyes", title="VP Sales", employer_name="Acme",
        employer_domain="acme.example", linkedin="https://www.linkedin.com/in/dana",
        updated="2026-09-01", current_jobs=(("acme.example", "Acme", "VP Sales"),),
    )


def test_the_searched_email_on_a_past_position_is_a_match_and_shows_the_move():
    """The address the CRM holds is often the one they left behind. The record
    is still theirs, and its current employer is the finding."""
    post = _Post(
        _people(_record("Lee Park", "CMO", "Globex", "globex.example",
                        ["lee@globex.example"], past_emails=["lee@acme.example"]))
    )
    got = LeadIQ(KEY, post=post).person_by_email("lee@acme.example")

    assert got is not None
    assert got.employer_domain == "globex.example"


def test_no_results_is_not_found():
    assert LeadIQ(KEY, post=_Post(_people())).person_by_email("x@acme.example") is None


def test_a_company_is_accepted_only_for_the_domain_asked_about():
    post = _Post(
        _Resp({"data": {"searchCompany": {"results": [
            {"name": "Near Miss", "domain": "acme-labs.example", "numberOfEmployees": 9},
            {"name": "Acme", "domain": "https://www.acme.example/", "numberOfEmployees": 480,
             "country": None, "locationInfo": {"country": "United States"}},
        ]}}})
    )
    got = LeadIQ(KEY, post=post).company_by_domain("acme.example")

    assert got == Company(name="Acme", domain="acme.example", employees=480,
                          country="United States")
    assert post.calls[0]["json"]["variables"]["input"] == {
        "domain": "acme.example", "strict": True,
    }


def test_a_missing_headcount_is_none_not_zero():
    """A 0 would fail the ICP test as a verdict; None says the value is absent."""
    post = _Post(
        _Resp({"data": {"searchCompany": {"results": [
            {"name": "Acme", "domain": "acme.example", "numberOfEmployees": None,
             "country": "Canada"}
        ]}}})
    )
    assert LeadIQ(KEY, post=post).company_by_domain("acme.example").employees is None


# --- failures ---------------------------------------------------------------- #


def test_an_http_error_raises_without_echoing_the_response():
    post = _Post(_Resp({"echo": KEY}, status=401))
    with pytest.raises(LeadIQError) as err:
        LeadIQ(KEY, post=post).person_by_email("dana@acme.example")
    assert "401" in str(err.value)
    assert KEY not in str(err.value)


def test_a_graphql_error_raises():
    post = _Post(_Resp({"errors": [{"message": "Cannot query field"}]}))
    with pytest.raises(LeadIQError, match="Cannot query field"):
        LeadIQ(KEY, post=post).company_by_domain("acme.example")


def test_throttling_is_waited_out(monkeypatch):
    monkeypatch.setattr("quorum.enrich.leadiq.time.sleep", lambda s: None)
    post = _Post(_Resp(status=429), _people())

    assert LeadIQ(KEY, post=post).person_by_email("dana@acme.example") is None
    assert len(post.calls) == 2


def test_the_check_reports_credits_available():
    post = _Post(_Resp({"data": {"account": {"universalPlan": {
        "name": "Annual", "status": "Active", "available": 1200, "used": 30}}}}))

    line = LeadIQ(KEY, post=post).check()
    assert "Active" in line and "1200" in line


# --- by LinkedIn URL --------------------------------------------------------- #


def test_a_linkedin_lookup_is_accepted_on_the_handle_searched():
    post = _Post(
        _people(_record("Ray Oh", "COO", "Hooli", "hooli.example", ["ray@hooli.example"],
                        linkedin="https://www.linkedin.com/in/Ray-Oh/"))
    )
    got = LeadIQ(KEY, post=post).person_by_linkedin("linkedin.com/in/ray-oh?trk=x")

    assert got is not None and got.employer_domain == "hooli.example"
    # The URL is normalised to the handle before it is sent.
    assert post.calls[0]["json"]["variables"]["input"] == {
        "linkedinUrl": "https://www.linkedin.com/in/ray-oh",
    }


def test_a_linkedin_lookup_that_returns_another_profile_is_not_accepted():
    post = _Post(
        _people(_record("Someone Else", "EVP", "Other Co", "other.example", [],
                        linkedin="https://www.linkedin.com/in/someone-else"))
    )
    assert LeadIQ(KEY, post=post).person_by_linkedin(
        "https://www.linkedin.com/in/ray-oh") is None


def test_a_url_with_no_profile_handle_is_not_searched():
    """A Sales Navigator URL, or a CRM value that is not a URL, has nothing to
    search by — and nothing is spent finding that out."""
    post = _Post()
    provider = LeadIQ(KEY, post=post)
    assert provider.person_by_linkedin("https://www.linkedin.com/sales/people/ACwAA1") is None
    assert provider.person_by_linkedin("not available in this CRM") is None
    assert post.calls == []


def test_every_current_position_is_kept():
    """Someone can hold a full-time role and advisory seats at once. All of
    them are kept, so "still at the company" can ask whether the company is
    among them rather than whether it is listed first."""
    record = _record("Dana Reyes", "Advisor", "Board Co", "board.example",
                     ["dana@acme.example"])
    record["currentPositions"].append({
        "title": "VP Sales", "companyInfo": {"name": "Acme", "domain": "acme.example"},
        "workEmail": None, "emails": [],
    })
    got = LeadIQ(KEY, post=_Post(_people(record))).person_by_email("dana@acme.example")

    assert got.current_jobs == (
        ("board.example", "Board Co", "Advisor"), ("acme.example", "Acme", "VP Sales"),
    )
    assert got.job_at("acme.example") == ("acme.example", "Acme", "VP Sales")
