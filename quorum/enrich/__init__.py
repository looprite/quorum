"""Enrichment providers — a second opinion on what the CRM holds.

A provider looks people up by email and companies by domain, and the weekly run
shows what it says *beside* the CRM's value. It never replaces a CRM value: the
CRM is what the customer has, a provider can be stale too, and the person who
settles a disagreement checks LinkedIn. See docs/enrichment.md.

Each provider is one module in this package exposing `PROVIDER`, a class with:

  display_name   how the provider is named in the output ("not found in …")
  env_vars       the environment variables it reads, so tests can clear them
  from_env()     an instance if its credentials are set, otherwise None
  check()        a free call that proves the credentials work, before any work
  person_by_email(email)     -> Person or None
  company_by_domain(domain)  -> Company or None

and optionally:

  person_by_linkedin(url)    -> Person or None, accepted only when the record's
                                LinkedIn handle is the one searched for

The weekly run tries LinkedIn only when an email lookup finds nothing and the
CRM holds a LinkedIn URL for the person — an email address goes stale exactly
when someone changes jobs, and a profile URL usually does not. A provider
without the method is simply not asked.

Providers are discovered from this package rather than imported by name, so
nothing outside a provider's own module needs to spell it. That is also how a
second provider is added: a new module, nothing else.

**Off unless configured.** With no provider configured, the run and its output
are exactly what they were before this package existed — no column, no tab, no
"not checked".
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Person:
    """What a provider says about one person — only the fields the map reads.

    Phone numbers and personal emails are deliberately absent: the map does not
    use them, and asking for a phone number is the expensive field.
    """

    name: str = ""
    title: str = ""
    employer_name: str = ""
    employer_domain: str = ""
    linkedin: str = ""
    # When the provider last refreshed the record, as it reports it. Shown so a
    # reader can weigh a disagreement against how old the provider's view is.
    updated: str = ""
    # Every current position as (domain, employer name, title). The fields above
    # are the first of them. A person can hold several at once — a full-time
    # role and advisory seats — so "is this person still at the company" asks
    # whether the company is among them, not whether it is listed first.
    current_jobs: tuple = ()

    def job_at(self, domain: str):
        """The current position at this domain, or None."""
        want = (domain or "").strip().lower()
        for job in self.current_jobs:
            if job[0] and job[0] == want:
                return job
        return None


@dataclass(frozen=True)
class Company:
    name: str = ""
    domain: str = ""
    employees: Optional[int] = None
    country: str = ""


_HANDLE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.I)


def linkedin_handle(url: str) -> str:
    """The profile handle in a LinkedIn /in/ URL, lowercased, or "".

    The handle is what identifies a profile; scheme, subdomain, trailing slash
    and query string are not. A Sales Navigator URL has no public handle and
    returns "" — it cannot be searched by, or compared with, a profile URL.
    """
    m = _HANDLE.search(url or "")
    return m.group(1).strip().lower().rstrip("/") if m else ""


class MoreThanOneProvider(RuntimeError):
    """Two providers configured at once.

    Refused rather than resolved by order: the provider rule is that a person one
    provider cannot find is shown as not found in *that* provider, never filled
    from another. Choosing between two silently is the substitution the rule
    forbids.
    """


def _modules():
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(module, "PROVIDER"):
            yield module.PROVIDER


def env_vars() -> tuple[str, ...]:
    """Every environment variable any provider reads."""
    names: list[str] = []
    for provider in _modules():
        names.extend(provider.env_vars)
    return tuple(names)


def configured(environ=None):
    """The one configured provider, or None.

    Raises MoreThanOneProvider if more than one has credentials set.
    """
    found = [p for p in (cls.from_env(environ) for cls in _modules()) if p is not None]
    if len(found) > 1:
        raise MoreThanOneProvider(
            "More than one enrichment provider is configured ("
            + ", ".join(p.display_name for p in found)
            + "). Configure one: a person one provider cannot find is reported "
            "as not found in it, never filled from another."
        )
    return found[0] if found else None
