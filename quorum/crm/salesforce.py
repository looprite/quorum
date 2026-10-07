"""Salesforce — read-only, at read time, no sync.

Quorum holds no CRM data and no CRM tables. Contacts are fetched when a run
needs them and compared in context, which is what keeps the comparison current
and the schema small.

Field discipline: this module names **standard** fields only — the ones present
in every org. Every other field it reads comes from the account's resolved field
map (`fieldmap.py`), which is why there is not a single managed-package
or org-local API name below. A name invented by one customer's admin raises
INVALID_FIELD and kills the whole query rather than blanking a column, so a
hardcoded one is a run that dies at the next customer.

Every query here is therefore built in two halves: the standard fields, written
out, and `field_map.select(...)`, resolved. A client constructed without a map
still works and reads standard fields only — which is what a run with no
Salesforce configured, and every test, does.

What leaves this module is a `Contact`, never a Salesforce record. The standard
field names are as much a coupling as the custom ones — they are simply spelled
the same in every Salesforce org, which is not the same as being spelled the
same in every CRM.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import requests

from ..config import Config
from ..domains import bare_domain
from .contact import Contact
from .fieldmap import FieldMap

TIMEOUT = 60

# Field names/labels that would hint at LinkedIn connection tracking, if any org
# had it. The pattern is deliberately broad: it is used to flag candidates for a
# human to look at, not to pick a field automatically.
_LI_TRACKING_HINT = re.compile(
    r"(linkedin|connect|invite|invitation|network|sales_?nav|social|outreach|sequence)",
    re.I,
)

# The standard half of each query. Nothing is selected "in case". What was
# dropped, and what each would have earned on one real bench, is recorded in
# docs/salesforce-access.md. The resolved half — headcount, HQ,
# LinkedIn — is appended from the field map at query time.
BENCH_STANDARD = ["Name", "Title", "Email", "MobilePhone", "LastActivityDate"]

CONTACT_STANDARD = [
    "Id", "Name", "Title", "MobilePhone", "Email", "AccountId", "Account.Name"
]

# Website is a standard field in every org, so it needs no field-map entry.
# One query per company reads every contact at it, to look for one person held
# twice. The cap bounds the cost: a company with more contacts than this is
# checked on its first COMPANY_CONTACTS_LIMIT only, so a duplicate beyond that
# goes unseen rather than slowing the run.
COMPANY_CONTACTS_LIMIT = 2000

ACCOUNT_STANDARD = ["Name", "Type", "Website"]


def soql_quote(value: str) -> str:
    """Escape a value for a SOQL string literal.

    Emails and domains reach these queries from the meetings database rather
    than from user input, so interpolating them raw was never exploitable — but
    an apostrophe in an address was enough to break a run, and a pipeline that
    reads your CRM should not be one quote away from a malformed query.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Salesforce:
    """Configured or not. Every method returns an explicit 'not checked' shape
    when Salesforce is absent, so a run without it produces an artifact with
    stated gaps rather than silently missing columns."""

    def __init__(self, cfg: Config, field_map: Optional[FieldMap] = None) -> None:
        self._cfg = cfg.salesforce
        self._token: Optional[str] = None
        self._instance: Optional[str] = None
        # Empty is legitimate: standard fields only, every resolved column
        # reporting itself unavailable.
        self.fields = field_map or FieldMap()

    @property
    def configured(self) -> bool:
        return self._cfg.configured

    @property
    def left_company_available(self) -> bool:
        """Does this CRM hold a "no longer at the company" box for contacts?
        Only if the field map resolved one; without it nothing changes."""
        return self.fields.available("Contact", "left_company")

    @property
    def linkedin_available(self) -> bool:
        """Does this CRM hold the person's LinkedIn URL at all?

        A capability question, not a field-map one: the caller needs it to tell
        'nothing on file' from 'no such field here', and must not have to know
        that a field map is how this adapter answers it.
        """
        return self.fields.available("Contact", "linkedin_url")

    def _auth(self) -> tuple[Optional[str], Optional[str]]:
        if self._token:
            return self._token, self._instance

        # By-hand path: a token pasted from `sf org display`. Expires in ~2
        # hours; a 401 mid-run means the token died, not that the network
        # failed.
        if self._cfg.access_token and self._cfg.instance_url:
            self._token, self._instance = self._cfg.access_token, self._cfg.instance_url
            return self._token, self._instance

        # Deployed path: client credentials. No paste, no refresh ritual.
        if not (self._cfg.token_url and self._cfg.client_id and self._cfg.client_secret):
            return None, None
        resp = requests.post(
            self._cfg.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._instance = payload["instance_url"]
        return self._token, self._instance

    def query(self, soql: str) -> Optional[dict[str, Any]]:
        token, instance = self._auth()
        if not token:
            return None
        resp = requests.get(
            f"{instance}/services/data/{self._cfg.api_version}/query",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": soql},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401:
            raise SalesforceAuthExpired(
                "Salesforce returned 401 — the access token has expired. "
                "See docs/salesforce-access.md for the re-auth runbook, or configure "
                "SF_TOKEN_URL / SF_CLIENT_ID / SF_CLIENT_SECRET for a deployed run."
            )
        resp.raise_for_status()
        return resp.json()

    # --- reads the pipeline makes ----------------------------------------- #

    def contact_by_email(self, email: str) -> Optional[Contact]:
        """Step 3 — does this attendee exist as a Contact?"""
        soql = (
            f"SELECT {self.fields.select('Contact', CONTACT_STANDARD)} FROM Contact "
            f"WHERE Email = '{soql_quote(email)}' LIMIT 1"
        )
        records = (self.query(soql) or {}).get("records") or []
        return self._contact(records[0]) if records else None

    def domain_stats(self, domain: str, senior_terms: list[str]) -> dict:
        """Step 4 — the existing CRM bench at a domain: total, focus-senior, and
        an AccountId to hang firmographics off."""
        out = {"sf_total": 0, "sf_senior": 0, "account_id": None}
        if not self.configured or not domain:
            return out
        like = f"%@{soql_quote(domain)}"
        out["sf_total"] = _count(
            self.query(f"SELECT COUNT(Id) c FROM Contact WHERE Email LIKE '{like}'")
        )
        out["sf_senior"] = _count(
            self.query(
                f"SELECT COUNT(Id) c FROM Contact WHERE Email LIKE '{like}' "
                f"AND {senior_clause(senior_terms)}"
            )
        )
        recs = self.query(f"SELECT AccountId FROM Contact WHERE Email LIKE '{like}' LIMIT 50")
        for r in (recs or {}).get("records", []):
            if r.get("AccountId"):
                out["account_id"] = r["AccountId"]
                break
        return out

    def account_firmographics(self, account_id: Optional[str]) -> dict:
        """Step 4 — Employees / HQ / Account type for the coverage tab.

        Every non-standard field comes from the resolved map, so an org with no
        managed data package installed reads BillingCountry / BillingCity /
        BillingState without a line of code changing. Gaps are output, not
        failure.

        City, state and country are returned separately as well as joined for
        display: the ICP geography test compares whole country values, and
        substring-matching the joined string is what made it necessary to look
        for " us" with a leading space so it would not match inside "Australia".
        """
        out = {
            "name": "", "employees": "", "hq": "", "account_type": "",
            "country": "", "city": "", "state": "",
            # The account's Website, reduced to a bare domain. Some companies
            # email from one domain and are filed under another.
            "account_domain": "",
        }
        if not account_id or not self.configured:
            return out
        recs = self.query(
            f"SELECT {self.fields.select('Account', ACCOUNT_STANDARD)} FROM Account "
            f"WHERE Id = '{soql_quote(account_id)}' LIMIT 1"
        )
        r = ((recs or {}).get("records") or [{}])[0]
        out["name"] = r.get("Name") or ""
        out["account_type"] = r.get("Type") or ""
        out["account_domain"] = bare_domain(r.get("Website"))
        out["employees"] = self.fields.value(r, "Account", "employee_count")
        out["city"] = self.fields.value(r, "Account", "hq_city")
        out["state"] = self.fields.value(r, "Account", "hq_state")
        out["country"] = self.fields.value(r, "Account", "hq_country")
        out["hq"] = ", ".join(
            str(x) for x in (out["city"], out["state"], out["country"]) if x
        )
        return out

    def contacts_for_company(
        self, domain: str, account_domain: str = "", account_id: Optional[str] = None
    ) -> list[Contact]:
        """Every contact at a company, by email domain, by the account's website
        domain, or by the account itself — name, email, title and account only.
        For finding one person held as several records."""
        if not self.configured or not domain:
            return []
        clauses = [f"Email LIKE '%@{soql_quote(domain)}'"]
        if account_domain and account_domain != domain:
            clauses.append(f"Email LIKE '%@{soql_quote(account_domain)}'")
        if account_id:
            clauses.append(f"AccountId = '{soql_quote(account_id)}'")
        soql = (
            f"SELECT Name, Email, Title, AccountId FROM Contact WHERE "
            f"{' OR '.join(clauses)} LIMIT {COMPANY_CONTACTS_LIMIT}"
        )
        return [self._contact(r) for r in (self.query(soql) or {}).get("records") or []]

    def senior_bench(self, domain: str, senior_terms: list[str]) -> list[Contact]:
        """Step 5 — contacts at a domain whose Title clears the focus profile's
        seniority bar. Everything the shortlist needs, in one query."""
        if not self.configured or not domain:
            return []
        soql = (
            f"SELECT {self.fields.select('Contact', BENCH_STANDARD)} FROM Contact "
            f"WHERE Email LIKE '%@{soql_quote(domain)}' "
            f"AND {senior_clause(senior_terms)} LIMIT 200"
        )
        return [self._contact(r) for r in (self.query(soql) or {}).get("records") or []]

    def _contact(self, record: dict) -> Contact:
        """A Salesforce record as the pipeline reads it.

        The one place in the system that knows Salesforce calls a job title
        `Title` and a mobile number `MobilePhone`.
        """
        return Contact(
            name=_text(record.get("Name")),
            title=_text(record.get("Title")),
            email=_text(record.get("Email")),
            # Presence only, and reduced here rather than by the caller: the
            # number must not survive into the JSON dump either, and the dump
            # carries `provenance` verbatim.
            mobile=bool(record.get("MobilePhone")),
            linkedin=(
                self.fields.value(record, "Contact", "linkedin_url")
                if self.linkedin_available
                else None
            ),
            last_activity=_text(record.get("LastActivityDate")),
            left_company=(
                bool(self.fields.value(record, "Contact", "left_company"))
                if self.left_company_available
                else None
            ),
            provenance={**record, "MobilePhone": bool(record.get("MobilePhone"))},
        )

    # --- reads the field-map resolver makes -------------------------------- #

    def describe(self, sobject: str) -> dict:
        """The org's own account of one object. Configure time, not run time."""
        token, instance = self._auth()
        if not token:
            return {"fields": []}
        resp = requests.get(
            f"{instance}/services/data/{self._cfg.api_version}/sobjects/{sobject}/describe",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def count(self, sobject: str, where: Optional[str] = None) -> int:
        """How many rows — optionally, how many with a field populated.

        This is the measurement the field map is ordered by, so it is a real
        aggregate over the whole object rather than a sample: 'has data' is a
        claim about the org, not about the first page of it.
        """
        clause = f" WHERE {where}" if where else ""
        return _count(self.query(f"SELECT COUNT(Id) c FROM {sobject}{clause}"))

    def describe_contact(self) -> dict:
        """Zero-cost evidence, kept because it answers a question that recurs:
        does this org track LinkedIn connection STATUS anywhere on Contact, or
        only the profile URL? On the org it was first run against, across every
        field on the object: profile URLs only, no connection status."""
        if not self._auth()[0]:
            return {"checked": False, "reason": "Salesforce not configured"}
        fields = [
            {"name": f.get("name"), "label": f.get("label"), "type": f.get("type")}
            for f in self.describe("Contact").get("fields", [])
        ]
        return {
            "checked": True,
            "field_count": len(fields),
            "candidates": [
                f
                for f in fields
                if _LI_TRACKING_HINT.search(f"{f['name']} {f['label']}")
            ],
            "all_fields": fields,
        }


class SalesforceAuthExpired(RuntimeError):
    pass


def senior_clause(terms: list[str]) -> str:
    return "(" + " OR ".join(f"Title LIKE '%{soql_quote(t)}%'" for t in terms) + ")"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _count(result: Optional[dict]) -> int:
    records = (result or {}).get("records") or [{}]
    return records[0].get("c", 0) if records else 0
