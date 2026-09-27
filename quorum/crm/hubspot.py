"""HubSpot — marketing contacts, read-only, at read time.

Salesforce is the source of truth for accounts, contacts and titles. HubSpot
answers two narrower questions: is this attendee known to marketing at all, and
how large is the marketing pool at a domain.

Like the Salesforce adapter, what leaves here is a `Contact` — nothing under
`weekly/` should know that HubSpot spells a job title `jobtitle` and splits a
name across `firstname` and `lastname`.
"""

from __future__ import annotations

import time
from typing import Optional

import requests

from ..config import Config
from .contact import Contact

TIMEOUT = 30
SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"

# Both reads below hit the CRM *search* endpoint, which HubSpot throttles on a
# short interval rather than against the daily allowance. A weekly run makes
# far fewer calls than a day's allowance on any plan we have seen, so a 429
# here is a burst limit rather than a quota. HubSpot did
# not return the per-interval headers on this endpoint, so the interval is
# unknown and the retry deliberately does not depend on knowing it.
#
# The cap is small on purpose: a genuinely throttled account should fail loudly
# rather than hang. Worst case is four waits, each bounded by MAX_BACKOFF.
MAX_ATTEMPTS = 5
BACKOFF_BASE = 2.0
MAX_BACKOFF = 60.0

CONTACT_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "jobtitle",
    "mobilephone",
    "company",
    "associatedcompanyid",
]


class HubSpot:
    def __init__(self, cfg: Config) -> None:
        self._key = cfg.hubspot.api_key

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _search(self, payload: dict) -> dict:
        """One POST to the contacts search endpoint, waiting out a 429.

        Any other error status raises as before — only throttling is retried.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = requests.post(
                SEARCH_URL, headers=self._headers, json=payload, timeout=TIMEOUT
            )
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            if attempt < MAX_ATTEMPTS:
                time.sleep(_retry_delay(resp, attempt))
        raise HubSpotRateLimited(
            f"HubSpot returned 429 on {MAX_ATTEMPTS} consecutive attempts to "
            f"{SEARCH_URL}. This endpoint is burst-limited; if it persists the "
            "run is going too fast for the account rather than out of quota."
        )

    def contact_by_email(self, email: str) -> Optional[Contact]:
        """Step 3 — does this attendee exist as a marketing contact?"""
        if not self.configured or not email:
            return None
        body = self._search(
            {
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": CONTACT_PROPERTIES,
                "limit": 1,
            }
        )
        results = body.get("results", [])
        return self._contact(results[0]) if results else None

    def _contact(self, record: dict) -> Contact:
        """A HubSpot record as the pipeline reads it.

        `linkedin` stays None: HubSpot holds no LinkedIn URL for a contact in
        the properties this adapter asks for, and None is how the type says
        "this CRM has no such field" rather than "this person has nothing in
        it". `last_activity` is likewise absent — Salesforce's rollup is the
        only activity source the artifact uses.
        """
        props = record.get("properties") or {}
        name = " ".join(
            part for part in (props.get("firstname"), props.get("lastname")) if part
        ).strip()
        return Contact(
            name=name,
            title=(props.get("jobtitle") or "").strip(),
            email=(props.get("email") or "").strip(),
            mobile=bool(props.get("mobilephone")),
            provenance={
                **record,
                "properties": {**props, "mobilephone": bool(props.get("mobilephone"))},
            },
        )

    def count_domain(self, domain: str) -> int:
        """Step 4 — the marketing reach pool at a domain, for the coverage tab."""
        if not self.configured or not domain:
            return 0
        body = self._search(
            {
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "hs_email_domain",
                                "operator": "EQ",
                                "value": domain,
                            }
                        ]
                    }
                ],
                "properties": ["email"],
                "limit": 1,
            }
        )
        return body.get("total", 0)


class HubSpotRateLimited(RuntimeError):
    pass


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt.

    HubSpot's `Retry-After` is honoured when present and readable, capped so a
    large value cannot stall the run past the attempt budget. Otherwise back off
    exponentially from BACKOFF_BASE.
    """
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return min(max(float(header), 0.0), MAX_BACKOFF)
        except (TypeError, ValueError):
            pass  # not a number of seconds — fall through to backoff
    return min(BACKOFF_BASE * (2 ** (attempt - 1)), MAX_BACKOFF)
