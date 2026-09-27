"""The run in a handful of counts — the Summary tab, and summary_<week>.json.

Every count is a count of rows the other tabs already show, so a reader can
check any number against the tab it names. Nothing is computed here that a tab
does not carry, with one exception: whether anyone senior at a company was
contacted recently is asked of the whole CRM bench, not the capped stakeholder
list, so the answer does not change with SHORTLIST_SIZE.

A count comes with what it is out of wherever there is a denominator. "12 not
in your CRM" means nothing on its own; "12 of 40 people met" is a finding.

Same rule as every column: a source that was not configured contributes no
count. With no CRM there is no "not in your CRM" line, rather than a zero that
reads as "everyone is in it".
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..config import Config
from ..crm.fieldmap import NOT_AVAILABLE, NOT_CHECKED
from .coverage import seniority_prose
from .people import in_any_crm, missing_from_crm
from .stakeholders import ICP_NOT_ASSESSED, NO_SENIOR_CONTACT

# Bumped only if a key changes meaning or disappears. A new key is not a bump:
# a reader that does not know it can ignore it.
SCHEMA = 1


def _stat(area: str, key: str, what: str, count: int, out_of: Optional[int] = None) -> dict:
    return {"area": area, "key": key, "what": what, "count": count, "out_of": out_of}


def build(
    cfg: Config,
    reconciled: list[dict],
    coverage: list[dict],
    stakeholders: list[dict],
    bench_raw: list[dict],
    profile: dict,
    enrichment: Optional[str] = None,
    queue: Optional[list[dict]] = None,
    queue_kinds: tuple[str, ...] = (),
) -> list[dict]:
    """-> the stats, in the order the Summary tab shows them."""
    crm_on = cfg.salesforce.configured or cfg.hubspot.configured
    other = enrichment
    stats: list[dict] = []

    # --- Company coverage ------------------------------------------------ #
    area = "Company coverage"
    met = len(coverage)
    stats.append(_stat(area, "companies_met", "Companies met", met))
    assessed = [c for c in coverage if c.get("assessed", True)]
    unassessed = met - len(assessed)
    targets = [c for c in coverage if c.get("is_target")]
    if assessed:
        stats.append(
            _stat(area, "companies_fit", "Meet your profile", len(targets), met)
        )
    if unassessed:
        stats.append(
            _stat(area, "companies_not_assessed", "Could not be assessed", unassessed, met)
        )
    # The bench is read from Salesforce; without it there is no one to have
    # contacted, and a zero would read as a finding.
    if assessed and cfg.salesforce.configured:
        target_domains = {c["domain"] for c in targets}
        recent = sum(
            1 for b in bench_raw
            if b.get("domain") in target_domains and b.get("any_recent_contact")
        )
        stats.append(
            _stat(
                area,
                "companies_fit_recent_senior",
                f"Meet your profile, and someone there at {seniority_prose(profile)} "
                f"level was contacted in the last {cfg.recent_days} days",
                recent,
                len(targets),
            )
        )

    # --- Met this week --------------------------------------------------- #
    area = "Met this week"
    people = len(reconciled)
    stats.append(_stat(area, "people_met", "People met", people))
    if crm_on:
        missing = [r for r in reconciled if missing_from_crm(r)]
        stats.append(
            _stat(area, "people_not_in_crm", "Not in your CRM", len(missing), people)
        )
        if other:
            looked_up = [r for r in missing if "other_found" in r]
            stats.append(
                _stat(
                    area,
                    "people_not_in_crm_found",
                    f"Not in your CRM, and found by {other} (shared inboxes not looked up)",
                    sum(1 for r in looked_up if r["other_found"]),
                    len(looked_up),
                )
            )
        held = [r for r in reconciled if in_any_crm(r)]
        stats.append(_stat(area, "people_in_crm", "In your CRM", len(held), people))
        stats.append(
            _stat(
                area, "people_in_crm_no_title", "In your CRM, no title",
                sum(1 for r in held if not r.get("title")), len(held),
            )
        )
        # Only where the CRM has a LinkedIn field to be empty.
        answerable = [
            r for r in held if r.get("linkedin_in_crm") not in (None, NOT_CHECKED)
        ]
        if answerable:
            stats.append(
                _stat(
                    area, "people_in_crm_no_linkedin", "In your CRM, no LinkedIn",
                    sum(1 for r in answerable if r.get("linkedin_in_crm") is False),
                    len(answerable),
                )
            )
        stats.append(
            _stat(
                area, "people_in_crm_no_mobile", "In your CRM, no mobile",
                sum(1 for r in held if r.get("mobile_in_crm") is False), len(held),
            )
        )

    # --- Stakeholder list ------------------------------------------------ #
    area = "Stakeholder list"
    listed = [
        r for r in stakeholders if r.get("name") not in (NO_SENIOR_CONTACT, ICP_NOT_ASSESSED)
    ]
    if assessed:
        stats.append(_stat(area, "stakeholders", "People on the list", len(listed)))
        stats.append(
            _stat(
                area, "stakeholders_no_title", "No title in the CRM",
                sum(1 for r in listed if not r.get("title")), len(listed),
            )
        )
        answerable = [r for r in listed if r.get("linkedin") != NOT_AVAILABLE]
        if answerable:
            stats.append(
                _stat(
                    area, "stakeholders_no_linkedin", "No LinkedIn in the CRM",
                    sum(1 for r in answerable if not r.get("linkedin")), len(answerable),
                )
            )
        stats.append(
            _stat(
                area, "stakeholders_no_mobile", "No mobile in the CRM",
                sum(1 for r in listed if r.get("mobile") == "no"), len(listed),
            )
        )

    # --- Review queue ---------------------------------------------------- #
    if other:
        area = "Review queue"
        counts: dict[str, int] = {k: 0 for k in queue_kinds}
        for q in queue or []:
            counts[q["kind"]] = counts.get(q["kind"], 0) + 1
        # Zeros stay: week to week, a kind going to zero is the thing to see.
        for kind, n in counts.items():
            stats.append(_stat(area, "queue:" + kind, kind, n))

    return stats


def write(path: str, week: str, stats: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "week_start": week, "stats": stats}, fh, indent=2)
        fh.write("\n")
