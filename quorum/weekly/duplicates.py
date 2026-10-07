"""One person held as several CRM contacts.

Different emails, different domains, sometimes different accounts. The meeting
matches whichever record has the email it saw, which is often the one without a
title, and the person can take two places on the stakeholder list. Matching that
guessed which record was real would be wrong as often as right, so this asks: a
review-queue row per person, for someone to merge in the CRM if they are the
same.

Salesforce only. HubSpot is not asked, and with only HubSpot nothing here runs.
"""

from __future__ import annotations

from .names import name_key

NO_DOMAIN = "(no-domain)"


def find(sf, coverage: list[dict]) -> tuple[dict, int]:
    """-> ({domain: {name key: [contacts]}}, queries made).

    One query per company, not per person. Only names held twice or more at a
    company are returned.
    """
    if not sf.configured:
        return {}, 0
    found: dict = {}
    queries = 0
    for c in coverage:
        domain = c.get("domain")
        if not domain or domain == NO_DOMAIN:
            continue
        contacts = sf.contacts_for_company(
            domain, c.get("account_domain") or "", c.get("account_id") or ""
        )
        queries += 1
        by_name: dict = {}
        for contact in contacts:
            key = name_key(contact.name)
            if key:
                by_name.setdefault(key, []).append(contact)
        sets = {k: v for k, v in by_name.items() if len(v) > 1}
        if sets:
            found[domain] = sets
    return found, queries


def records(contacts) -> str:
    """The records of one person, as the queue shows them."""
    return "; ".join(
        f"{c.email} — {c.title.strip() or '(no title)'}"
        for c in sorted(contacts, key=lambda c: c.email.lower())
    )
