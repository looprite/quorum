"""Domain classification.

An attendee is internal (an employee), personal (a consumer mailbox), or
external. Only external attendees reach the artifact, so this function decides
what the whole pipeline can see.
"""

from __future__ import annotations

from typing import Iterable, Optional

PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "hotmail.com",
        "hotmail.co.uk",
        "hotmail.fr",
        "outlook.com",
        "live.com",
        "live.co.uk",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "protonmail.com",
        "pm.me",
        "proton.me",
        "msn.com",
    }
)


def classify_domain(domain: str, internal_domains: Iterable[str]) -> str:
    d = domain.lower()
    if d in {x.lower() for x in internal_domains}:
        return "internal"
    if d in PERSONAL_DOMAINS:
        return "personal"
    return "external"


def domain_of(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    part = email.rsplit("@", 1)[1].strip().lower()
    return part or None
