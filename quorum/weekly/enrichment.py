"""The enrichment pass — a provider's second opinion, set beside the CRM's.

Runs only when an enrichment provider is configured (see `quorum/enrich/`), and
changes nothing when one is not. Serves:

  tab 1  Name, Title and LinkedIn (provider) — for people not in the CRM
  tab 2  Employees (provider), HQ (provider), Profile check
  tab 3  Still at company?, Title (provider), LinkedIn (provider), and which
         companies reach the map at all (a disputed verdict does)
  tab 4  the review queue

Three sources, and none is the truth. The CRM is what the customer has; the
provider is a second opinion that can be stale too; LinkedIn is what a person
checks, and the one that settles it. So a provider value is **never** written
over a CRM value — it is shown beside it, and a disagreement becomes a row in
the review queue for a person to settle.

Agreement is not correctness either. A provider may be one of the sources the
CRM was filled from, in which case the two agreeing says little. The queue
therefore holds disagreements and missing values; it does not certify the rest.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from ..enrich import linkedin_handle
from .coverage import meets_profile
from .people import company_mismatch, missing_from_crm
from .stakeholders import ICP_NOT_ASSESSED, NO_SENIOR_CONTACT

AGREES = "agrees"
MATCHED_ON_LINKEDIN = "(matched on LinkedIn)"


def not_found(provider) -> str:
    """The provider rule, as a cell: looked, and this provider has nothing.
    Never inferred, never blank, never filled from somewhere else."""
    return f"not found in {provider.display_name}"


class _Cached:
    """One lookup per email and per domain per run, whichever tab asks first.
    A person on tab 1 and tab 3 is one credit, not two."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self._people: dict = {}
        self._companies: dict = {}
        self._by_linkedin: dict = {}
        self.people_looked_up = 0
        self.companies_looked_up = 0
        self.linkedin_looked_up = 0
        # Optional in the provider interface; a provider without it is not asked.
        self.can_search_linkedin = hasattr(provider, "person_by_linkedin")

    def person(self, email: str):
        key = (email or "").strip().lower()
        if key not in self._people:
            self._people[key] = self.provider.person_by_email(key) if key else None
            self.people_looked_up += 1 if key else 0
        return self._people[key]

    def person_by_linkedin(self, url: str):
        handle = linkedin_handle(url)
        if not handle or not self.can_search_linkedin:
            return None
        if handle not in self._by_linkedin:
            self._by_linkedin[handle] = self.provider.person_by_linkedin(url)
            self.linkedin_looked_up += 1
        return self._by_linkedin[handle]

    def company(self, domain: str):
        key = (domain or "").strip().lower()
        if key not in self._companies:
            self._companies[key] = self.provider.company_by_domain(key) if key else None
            self.companies_looked_up += 1 if key else 0
        return self._companies[key]


def start(provider) -> Optional[_Cached]:
    return _Cached(provider) if provider else None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _handle(url: str) -> str:
    """For comparing two LinkedIn values: the handle when there is one, the
    normalised text otherwise."""
    return linkedin_handle(url) or _norm(url)


def _name_tokens(name: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    # An apostrophe joins ("O'Neil" is "oneil"); any other punctuation separates.
    plain = re.sub(r"['’]", "", plain.lower())
    return re.sub(r"[^a-z\s]", " ", plain).split()


def names_agree(a: str, b: str) -> bool:
    """First and last name both agree, ignoring case, accents, punctuation and
    anything in between. "Dana M. Reyes" agrees with "Dana Reyes"; "Dana Reyes"
    does not agree with "Dana Rivera"."""
    x, y = _name_tokens(a), _name_tokens(b)
    return bool(x and y) and x[0] == y[0] and x[-1] == y[-1]


# --- tab 2: companies ------------------------------------------------------ #


def companies(pass_: _Cached, coverage: list[dict], profile: dict) -> None:
    """Look up every company met, and compare ICP verdicts rather than numbers.

    A difference matters only if it changes the verdict: 250 against 275 is
    inside a 50–500 band either way, 480 against 520 flips it. So what is
    compared is the ICP test run on each source's own numbers.

    A disputed company goes onto the stakeholder list, marked. A wrong "no" is
    the invisible error — the company never reaches the map and nobody looks —
    so a dispute is resolved by a person, not by leaving it off.
    """
    name = pass_.provider.display_name
    for c in coverage:
        found = pass_.company(c.get("domain"))
        c["disputed"] = False
        if found is None:
            c["other_employees"] = None
            c["other_hq"] = ""
            c["verdict_check"] = not_found(pass_.provider)
            c["other_missing"] = True
            continue

        c["other_employees"] = found.employees
        c["other_hq"] = found.country
        ok, why = meets_profile(profile, found.employees, found.country)
        c["other_missing"] = found.employees is None or not found.country
        said = "yes" if ok else f"no ({why})"

        if not c.get("assessed", True):
            c["verdict_check"] = f"CRM not assessed — {name} says {said}"
        elif (c.get("meets") == "yes") == bool(ok):
            c["verdict_check"] = AGREES
        else:
            c["verdict_check"] = f"disputed — {name} says {said}"
            c["disputed"] = True


# --- tab 3: stakeholders --------------------------------------------------- #


def stakeholders(pass_: _Cached, rows: list[dict]) -> None:
    """Is each person still where the CRM says, and does the provider agree on
    title and LinkedIn?

    A detected move flags the row; it never removes it. The CRM record is what
    the customer has, and a name vanishing without explanation is worse than a
    name marked stale. The provider's title and LinkedIn are filled only where
    they differ from the CRM's — the caption says so.
    """
    name = pass_.provider.display_name
    for r in rows:
        r["matched_on"] = ""
        if r.get("name") in (NO_SENIOR_CONTACT, ICP_NOT_ASSESSED) or not r.get("_email"):
            r["still_at"] = r["other_title"] = r["other_linkedin"] = ""
            continue
        p = pass_.person(r["_email"])
        suffix = ""
        if p is not None:
            r["matched_on"] = "email"
        else:
            # The email found nothing. An email goes stale exactly when someone
            # changes jobs; a profile URL usually does not — so try the CRM's
            # LinkedIn URL, where it holds one. The provider only accepts its
            # own handle match; whether that profile is *this* person is
            # checked here, by name, because a CRM URL can point at someone
            # else. A handle match under another name is a question for a
            # person, not a finding about this one.
            q = pass_.person_by_linkedin(r.get("linkedin") or "")
            if q is not None and names_agree(r.get("name"), q.name):
                p, suffix = q, f" {MATCHED_ON_LINKEDIN}"
                r["matched_on"] = "LinkedIn"
            elif q is not None:
                r["linkedin_other_person"] = q.name
        if p is None:
            r["still_at"] = not_found(pass_.provider)
            r["other_title"] = r["other_linkedin"] = ""
            continue

        domain = (r.get("domain") or "").lower()
        jobs = p.current_jobs or ((p.employer_domain, p.employer_name, p.title),)
        here = p.job_at(domain) if p.current_jobs else (jobs[0] if jobs[0][0] == domain else None)
        if here:
            # Among their current positions, even if not the first listed: a
            # full-time role elsewhere plus a seat here is still "here".
            r["still_at"] = "yes" + suffix
            title_here = here[2]
        elif not any(j[0] for j in jobs):
            r["still_at"] = f"unclear — no current employer in {name}" + suffix
            title_here = ""
        else:
            first = next(j for j in jobs if j[0])
            r["still_at"] = f"no — now at {first[1] or first[0]}" + suffix
            # Someone who has moved holds a title somewhere else. Setting it
            # beside the CRM's would read as a disagreement about this job; the
            # move is the finding, and it is already stated.
            title_here = ""
        r["other_updated"] = p.updated
        r["other_title"] = (
            title_here if title_here and _norm(title_here) != _norm(r.get("title")) else ""
        )
        crm_linkedin = r.get("linkedin") or ""
        r["other_linkedin"] = (
            p.linkedin if p.linkedin and _handle(p.linkedin) != _handle(crm_linkedin) else ""
        )


# --- tab 1: people not in the CRM ----------------------------------------- #


def not_in_crm(pass_: _Cached, reconciled: list[dict]) -> None:
    """A name, title and LinkedIn URL for people the CRM does not hold — what a
    person needs to add them, or to connect with them."""
    for r in reconciled:
        if not missing_from_crm(r):
            continue
        if "shared inbox" in (r.get("flag") or ""):
            # A role inbox is not a person. Looking it up spends a credit on an
            # answer that cannot be right.
            r["other_name"], r["other_title"] = "not looked up — shared inbox", ""
            r["other_linkedin"] = ""
            continue
        p = pass_.person(r.get("email"))
        # Read by the summary's count, so it does not have to parse the cell.
        r["other_found"] = p is not None
        if p is None:
            r["other_name"], r["other_title"], r["other_linkedin"] = not_found(pass_.provider), "", ""
        else:
            r["other_name"], r["other_title"], r["other_linkedin"] = p.name, p.title, p.linkedin


# --- tab 4: the review queue ---------------------------------------------- #

QUEUE_ORDER = (
    "Profile fit disputed",
    "Headcount or HQ missing",
    "May have left",
    "CRM LinkedIn may be someone else",
    "Account may be linked to the wrong company",
    "Title differs",
    "LinkedIn differs",
)


def review_queue(pass_: _Cached, coverage: list[dict], rows: list[dict]) -> list[dict]:
    """Every item a person should settle, and nothing else.

    Written as a work queue rather than as CRM updates: Quorum writes nothing to
    the CRM, and whoever works through this list may not have CRM access at all.
    Each row says what to check and where.
    """
    queue: list[dict] = []

    def add(kind, company, who, crm, other, check):
        queue.append(
            {"kind": kind, "company": company, "who": who, "crm": crm, "other": other,
             "check": check}
        )

    for c in coverage:
        label = c.get("name") or c.get("domain")
        crm_side = f"{c.get('employees') or '?'} employees, HQ {c.get('hq') or '?'} — {c.get('meets')}"
        other_side = (
            f"{c.get('other_employees') if c.get('other_employees') is not None else '?'} "
            f"employees, HQ {c.get('other_hq') or '?'}"
        )
        if c.get("disputed"):
            add("Profile fit disputed", label, "", crm_side,
                f"{other_side} — {c['verdict_check'].split(' says ', 1)[-1]}",
                "Company LinkedIn page: headcount, HQ")
        elif c.get("assessed", True) and (
            c.get("other_missing") or "no size" in str(c.get("meets")) or "HQ unknown" in str(c.get("meets"))
        ):
            add("Headcount or HQ missing", label, "", crm_side,
                other_side if c.get("verdict_check") != not_found(pass_.provider) else not_found(pass_.provider),
                "Company LinkedIn page; fill in the CRM")
        if c.get("assessed", True) and c.get("name") and company_mismatch(c.get("domain"), c.get("name")):
            add("Account may be linked to the wrong company", label, "",
                f"account '{c.get('name')}' reached through {c.get('domain')}", "",
                "The account's Website field in the CRM")

    for r in rows:
        if r.get("linkedin_other_person"):
            add("CRM LinkedIn may be someone else", r.get("company", ""), r.get("name", ""),
                r.get("linkedin") or "", f"profile at that URL is {r['linkedin_other_person']}",
                "Open the CRM's LinkedIn URL")
        if not r.get("still_at") or r.get("still_at") == not_found(pass_.provider):
            continue
        who = r.get("name", "")
        company = r.get("company", "")
        if r["still_at"].startswith("no —"):
            add("May have left", company, who, f"at {company}",
                r["still_at"][len("no — "):] + (f" (updated {r['other_updated']})" if r.get("other_updated") else ""),
                "Their LinkedIn profile")
        if r.get("other_title"):
            add("Title differs", company, who, r.get("title") or "(none)", r["other_title"],
                "Their LinkedIn profile")
        if r.get("other_linkedin"):
            add("LinkedIn differs", company, who, r.get("linkedin") or "(none)", r["other_linkedin"],
                "Open both — one may be someone else")

    rank = {k: i for i, k in enumerate(QUEUE_ORDER)}
    queue.sort(key=lambda q: (rank.get(q["kind"], len(rank)), str(q["company"]).lower()))
    return queue
