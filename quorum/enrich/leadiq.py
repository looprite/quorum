"""LeadIQ as an enrichment provider — its GraphQL API, read-only.

The only file under `quorum/` that names this provider. Everything else asks the
`enrich` package for whichever provider is configured, so the rest of the
pipeline reads `display_name` rather than spelling it.

What is asked for, and what is not. Each person lookup selects the current and
past positions (title, employer, work emails), the LinkedIn URL, the name and
when the record was last updated. It never selects a phone number or a personal
email: the map does not use them, and a phone number is the field that costs
ten times a record. See docs/enrichment.md for what a lookup costs.

**A result is accepted only if it is the person we asked about.** A lookup by
email can return a record whose emails do not include the one searched for, at
the provider's highest confidence score — observed on a real call. Taken at
face value, that record would report a stakeholder as having moved to a company
they never worked at. So a person is accepted only when the searched email is
on the record, in a current or past position; anything else is "not found".
A lookup by LinkedIn URL is held to the same rule on the profile handle, and
companies on domain.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

import requests

from . import Company, Person, Withheld, linkedin_handle

ENDPOINT = "https://api.leadiq.com/graphql"
TIMEOUT = 30

# Throttling is waited out, as the HubSpot adapter does; any other error raises.
# A lookup that fails loudly is better than a column that is silently wrong.
MAX_ATTEMPTS = 5
BACKOFF_BASE = 2.0
MAX_BACKOFF = 60.0

ACCOUNT_QUERY = """
query Account {
  account { universalPlan { name status available used } }
}
"""

PERSON_QUERY = """
query Person($input: SearchPeopleInput!) {
  searchPeople(input: $input) {
    results {
      name { fullName }
      linkedin { linkedinUrl }
      updatedAt
      currentPositions {
        title
        companyInfo { name domain }
        workEmail { value }
        emails { value }
      }
      pastPositions {
        workEmail { value }
        emails { value }
      }
    }
  }
}
"""

COMPANY_QUERY = """
query Company($input: SearchCompanyInput!) {
  searchCompany(input: $input) {
    results { name domain numberOfEmployees country locationInfo { country } }
  }
}
"""


class LeadIQError(RuntimeError):
    pass


def _bare_domain(value: str) -> str:
    v = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    v = v.split("/", 1)[0]
    return v[4:] if v.startswith("www.") else v


def _emails(position: dict) -> set[str]:
    found = set()
    work = position.get("workEmail") or {}
    if work.get("value"):
        found.add(work["value"].strip().lower())
    for e in position.get("emails") or []:
        if e.get("value"):
            found.add(e["value"].strip().lower())
    return found


class LeadIQ:
    display_name = "LeadIQ"
    env_vars = ("LEADIQ_API_KEY",)

    def __init__(self, api_key: str, post: Optional[Callable] = None) -> None:
        self._key = api_key
        # Injectable so tests never make a network call.
        self._post = post or requests.post

    @classmethod
    def from_env(cls, environ=None) -> Optional["LeadIQ"]:
        key = (environ if environ is not None else os.environ).get("LEADIQ_API_KEY", "")
        return cls(key.strip()) if key and key.strip() else None

    # --- transport -------------------------------------------------------- #

    def _query(
        self, query: str, variables: Optional[dict] = None, withhold_under: str = ""
    ) -> dict:
        """`withhold_under` names the root field of a person lookup. Errors that
        all lie under it are the provider declining to return a record, and
        raise Withheld; anything else raises LeadIQError. See _withheld()."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self._post(
                ENDPOINT,
                auth=(self._key, ""),
                json={"query": query, "variables": variables or {}},
                timeout=TIMEOUT,
            )
            if resp.status_code != 429:
                if resp.status_code >= 400:
                    # The body is not included: an error response can echo the
                    # request, and the request carries the credential.
                    raise LeadIQError(f"{self.display_name} API returned HTTP {resp.status_code}")
                body = resp.json()
                if body.get("errors"):
                    if withhold_under and _withheld(body["errors"], withhold_under):
                        raise Withheld(_codes(body["errors"]))
                    messages = "; ".join(e.get("message", "") for e in body["errors"])
                    raise LeadIQError(f"{self.display_name} API error: {messages}")
                return body.get("data") or {}
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(BACKOFF_BASE * (2 ** (attempt - 1)), MAX_BACKOFF))
        raise LeadIQError(
            f"{self.display_name} API returned 429 on {MAX_ATTEMPTS} consecutive attempts."
        )

    # --- the three calls the run makes ----------------------------------- #

    def check(self) -> str:
        """A free call proving the key works, made before any other work.
        Returns a line for the log: the plan and the credits available."""
        plan = (self._query(ACCOUNT_QUERY).get("account") or {}).get("universalPlan") or {}
        return (
            f"{plan.get('name') or 'plan unknown'} ({plan.get('status') or 'status unknown'}), "
            f"{plan.get('available', 'unknown')} credits available"
        )

    def person_by_email(self, email: str) -> Optional[Person]:
        email = (email or "").strip().lower()
        if not email:
            return None
        data = self._query(
            PERSON_QUERY, {"input": {"email": email}}, withhold_under="searchPeople"
        )
        for record in (data.get("searchPeople") or {}).get("results") or []:
            current = record.get("currentPositions") or []
            past = record.get("pastPositions") or []
            on_record = set().union(*(_emails(p) for p in current + past)) if current or past else set()
            if email not in on_record:
                continue  # a different person — see the module docstring
            return _person(record)
        return None

    def person_by_linkedin(self, url: str) -> Optional[Person]:
        """Accepted only when the record's profile handle is the one searched.

        Whether it is the *right* person is a separate question: a CRM's
        LinkedIn URL can point at someone else. The weekly run checks the name
        against the CRM's before using the result.
        """
        handle = linkedin_handle(url)
        if not handle:
            return None
        data = self._query(
            PERSON_QUERY,
            {"input": {"linkedinUrl": f"https://www.linkedin.com/in/{handle}"}},
            withhold_under="searchPeople",
        )
        for record in (data.get("searchPeople") or {}).get("results") or []:
            found = linkedin_handle((record.get("linkedin") or {}).get("linkedinUrl") or "")
            if found != handle:
                continue  # a different profile
            return _person(record)
        return None

    def company_by_domain(self, domain: str) -> Optional[Company]:
        domain = _bare_domain(domain)
        if not domain:
            return None
        data = self._query(COMPANY_QUERY, {"input": {"domain": domain, "strict": True}})
        for record in (data.get("searchCompany") or {}).get("results") or []:
            if _bare_domain(record.get("domain") or "") != domain:
                continue
            country = record.get("country") or (record.get("locationInfo") or {}).get("country") or ""
            employees = record.get("numberOfEmployees")
            return Company(
                name=(record.get("name") or "").strip(),
                domain=domain,
                employees=int(employees) if isinstance(employees, (int, float)) else None,
                country=country.strip(),
            )
        return None


def _withheld(errors: list, root: str) -> bool:
    """True when every error is a field error under `root`.

    Recognised from the GraphQL specification, not from LeadIQ's wording, which
    it does not document. A request error — a malformed or invalid query — has
    no `path`; an error raised while resolving a field must carry one, naming
    that field. So errors that all carry a path starting at the person search
    are the API declining part of an answer it was otherwise able to give: a
    record withheld for privacy compliance is that case. Authentication, credits
    and throttling arrive as HTTP statuses before any of this, and still raise.

    The whole lookup is treated as withheld, even if other records came back
    beside the error: a lookup is for one person, and part of an answer about
    them is not one to report.
    """
    return bool(errors) and all(
        isinstance(e.get("path"), list) and e["path"][:1] == [root] for e in errors
    )


def _codes(errors: list) -> str:
    """The provider's codes for a withheld record, for the log — never its
    message, which can name the person."""
    codes = sorted(
        {str((e.get("extensions") or {}).get("code") or "") for e in errors} - {""}
    )
    return ", ".join(codes)


def _person(record: dict) -> Person:
    current = record.get("currentPositions") or []
    jobs = tuple(
        (
            _bare_domain((p.get("companyInfo") or {}).get("domain") or ""),
            ((p.get("companyInfo") or {}).get("name") or "").strip(),
            (p.get("title") or "").strip(),
        )
        for p in current
    )
    first = jobs[0] if jobs else ("", "", "")
    return Person(
        name=((record.get("name") or {}).get("fullName") or "").strip(),
        title=first[2],
        employer_name=first[1],
        employer_domain=first[0],
        linkedin=((record.get("linkedin") or {}).get("linkedinUrl") or "").strip(),
        updated=str(record.get("updatedAt") or "")[:10],
        current_jobs=jobs,
    )


PROVIDER = LeadIQ
