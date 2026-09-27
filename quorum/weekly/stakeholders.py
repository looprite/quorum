"""Step 5 — the stakeholder list. The map: which specific people at the companies
that passed triage.

Serves all six columns of tab 3: Company, Name, Title, Recent contact?, LinkedIn,
Mobile in CRM?.

Ordering is two rules and no weighting: most senior first, recent contact
breaking ties between equals. Capped per company — the cap is a feature.

No action is suggested per person. Real outreach starts with a connection, may
become a message, and may or may not become a meeting request; a column
declaring one action per row is wrong at the first step and asserts a decision
that has not been made. The list says who is worth considering and stops.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from ..config import Config
from ..crm.fieldmap import NOT_AVAILABLE
from ..crm.salesforce import Salesforce

# Order matters: the VP test runs before the C-level test so "Vice President"
# never matches on the word 'president'.
_VP_STRING = re.compile(r"\b(vp|svp|evp|vice\s+president)\b", re.I)
_C_LEVEL = re.compile(r"\b(chief|ceo|cro|cfo|cto|coo|cmo|president|founder|owner)\b", re.I)
_C_LEVEL_EXCLUDE = re.compile(r"(chief\s+of\s+staff|deputy\s+chief|assistant\s+to\s+the)", re.I)
_DIRECTOR = re.compile(r"\b(director|head\s+of)\b", re.I)


def seniority_rank(title: Optional[str]) -> int:
    """3 C-level, 2 VP, 1 everything else. ORDERING ONLY — no outreach action is
    implied; see the module docstring."""
    t = (title or "").strip()
    if not t:
        return 0
    if _C_LEVEL_EXCLUDE.search(t):  # "Chief of Staff" is not C-level here
        return 2
    if _VP_STRING.search(t):
        return 2
    if _C_LEVEL.search(t):
        return 3
    if _DIRECTOR.search(t):
        return 1
    return 1


def clean_title(title: Optional[str]) -> str:
    """LinkedIn headlines get pasted into Title — "Chief Revenue Officer (cro) |
    Cybersecurity | Cloud | Saas". Keep the role, drop the billboard. Seniority
    ordering still reads the raw value; this is display only."""
    t = (title or "").replace("\n", " ").strip()
    return t.split("|")[0].strip() if "|" in t else t


def _as_date(value) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except (ValueError, TypeError):
        return None


def recent_contact(cfg: Config, history: Optional[dict], last_activity) -> str:
    """One question, one answer.

    Contact is a meeting (from the product DB) or anything logged in the CRM
    (LastActivityDate, a Task+Event rollup that covers emails and calls without
    saying which). Recent means inside cfg.recent_days. The kind of contact is
    stated rather than judged, so a reader can discount a training session
    themselves instead of the code deciding for them.

    Date first, kind second: "yes — 2026-08-18 (group call)". The column is
    narrow, and with the date last a long kind pushed it into the ellipsis —
    losing the part that answers "how recent". The attendee count is dropped
    for the same reason; the label alone is what stops a webinar reading as a
    relationship.
    """
    cutoff = dt.date.today() - dt.timedelta(days=cfg.recent_days)
    met = _as_date((history or {}).get("last_met"))
    activity = _as_date(last_activity)

    if met and met >= cutoff:
        smallest = int((history or {}).get("smallest_meeting") or 0)
        kind = "group call" if smallest > cfg.group_call_min else "met"
        return f"yes — {met.isoformat()} ({kind})"
    if activity and activity >= cutoff:
        return f"yes — {activity.isoformat()} (CRM activity)"

    last = max([d for d in (met, activity) if d], default=None)
    return f"no — last contact {last.isoformat()}" if last else "no — none on record"


NO_SENIOR_CONTACT = "— no senior contact in Salesforce —"

# Tab 3 when the ICP test could not run at all. An empty tab reads as "no
# targets this week", which is a finding a reader would act on; this says the
# test never ran. Same shape as the row above, for the same reason: the reader
# needs to see the difference between looking and finding nothing, and not
# looking.
ICP_NOT_ASSESSED = "— ICP not assessed: no CRM configured —"


def companies_for_map(coverage: list[dict]) -> list[dict]:
    """The companies tab 3 shows: confirmed targets, every company whose ICP
    test could not run, and every company whose verdict an enrichment provider
    disputes.

    The second half is the whole point. `is_target` is False for an unassessed
    company exactly as it is for a rejected one, so filtering on it alone drops
    a company out of the map because data nobody fetched did not clear a bar.

    The third is the same argument with a second source. A wrong "no" is
    invisible — the company never reaches the map and nobody looks — so a
    company the CRM rejects and the provider would accept goes on, marked as
    disputed, for a person to settle. An existing customer stays off either way.
    `disputed` is only ever set by the enrichment pass.
    """
    return [
        c for c in coverage
        if c.get("is_target")
        or not c.get("assessed", True)
        or (c.get("disputed") and not c.get("is_customer"))
    ]


def build(
    cfg: Config,
    coverage: list[dict],
    terms: list[str],
    history: dict[str, dict],
    sf: Salesforce,
) -> tuple[list[dict], list[dict]]:
    """-> (rows for tab 3, the raw bench for the JSON dump)."""
    rows: list[dict] = []
    raw: list[dict] = []

    for company in sorted(companies_for_map(coverage), key=lambda x: -x.get("met", 0)):
        if not company.get("assessed", True):
            # The ICP test never ran for this company, so there is no verdict to
            # act on and no bench worth querying — the CRM the bench comes from
            # is the one that is not configured. State that on the row; leaving
            # the tab empty would read as "nobody worth considering this week".
            rows.append(
                {
                    "domain": company["domain"],
                    "company": company.get("name") or company["domain"],
                    "disputed": bool(company.get("disputed")),
                    "name": ICP_NOT_ASSESSED,
                    "title": "",
                    "contact": "",
                    "linkedin": "",
                    "mobile": "",
                }
            )
            continue

        bench = sf.senior_bench(company["domain"], terms)

        # The phone NUMBER is redacted out of the dump. Sensitive contact fields
        # pass through to the CRM and are never persisted here; presence is the
        # only thing the artifact needs.
        raw.append(
            {
                "domain": company["domain"],
                "bench_size": len(bench),
                # What the CRM actually returned, sensitive fields already
                # reduced by the adapter. Carried through opaquely so the dump
                # still lets the ranking be re-tuned without re-querying —
                # reading a field name out of it would put the coupling back.
                "bench": [c.provenance for c in bench],
                # Overwritten below once the bench is scored; an empty bench
                # has nobody to have contacted.
                "any_recent_contact": False,
            }
        )

        if not bench:
            # A company with no senior CRM contact is a stated gap, not an
            # omission — the reader needs to see that we looked.
            rows.append(
                {
                    "domain": company["domain"],
                    "company": company.get("name") or company["domain"],
                    "disputed": bool(company.get("disputed")),
                    "name": NO_SENIOR_CONTACT,
                    "title": "",
                    "contact": "",
                    "linkedin": "",
                    "mobile": "",
                }
            )
            continue

        scored = []
        for person in bench:
            email = person.email.strip().lower()
            contact = recent_contact(cfg, history.get(email), person.last_activity)
            scored.append(
                {
                    "domain": company["domain"],
                    "company": company.get("name") or company["domain"],
                    "disputed": bool(company.get("disputed")),
                    "name": person.name,
                    "title": clean_title(person.title),
                    "contact": contact,
                    # None is the CRM having no LinkedIn field at all, which the
                    # column states; "" is this person having nothing in it.
                    "linkedin": (
                        NOT_AVAILABLE if person.linkedin is None else person.linkedin
                    ),
                    # "no", not "GAP". The column is a plain yes/no about what
                    # the CRM holds, and a missing mobile number is a fact about
                    # a contact record rather than something wrong with it.
                    # "GAP" read as a defect, in red, on rows where nothing was
                    # defective.
                    "mobile": "yes" if person.mobile else "no",
                    "_seniority": seniority_rank(person.title),
                    "_recent": contact.startswith("yes"),
                    "_email": email,
                }
            )

        # Across the whole bench, not the capped list: the summary asks whether
        # anyone senior at the company was contacted recently, and the answer
        # must not depend on SHORTLIST_SIZE.
        raw[-1]["any_recent_contact"] = any(x["_recent"] for x in scored)

        scored.sort(key=lambda x: (-x["_seniority"], not x["_recent"]))
        rows.extend(scored[: cfg.shortlist_size])

    return rows, raw
