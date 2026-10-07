"""Step 6 — emit. A summary and three tabs, and a fourth tab — the review
queue — when an enrichment provider is configured.

Nothing is written back to any system. The workbook and the JSON dump are the
only outputs, and the dump redacts MobilePhone to a boolean: sensitive contact
fields pass through to the CRM, never into a Quorum store.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..config import Config
from ..crm.fieldmap import NOT_AVAILABLE, NOT_CHECKED
from .coverage import seniority_prose
from .people import in_any_crm, missing_from_crm
from .stakeholders import NO_SENIOR_CONTACT

# A CRM column for someone with no CRM record. Not "no" — that asserts a fact
# about a record that does not exist — and not blank, which on LinkedIn (CRM) means
# "the CRM has the field and nothing in it". The in-CRM column beside it says
# why, and the caption says what the dash means.
NO_RECORD = "—"

HEADER_FILL = "2F5B7C"


def _sheet(wb: Workbook, title: str, headers: list[str]):
    ws = wb.create_sheet(title)
    ws.append(headers)
    font = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor=HEADER_FILL)
    for cell in ws[1]:
        cell.font = font
        cell.fill = fill
    ws.freeze_panes = "A2"
    return ws


def _linkedin_cell(value, url: str = "") -> str:
    """The CRM's LinkedIn URL, with the same three other answers as before.
    None is not False, and neither is NOT_CHECKED. A CRM with no LinkedIn
    field says so in the cell, a run with no CRM says *that*, and only a real
    False renders blank — so an empty column cannot be read as 'nobody has
    one'."""
    if value == NOT_CHECKED:
        return NOT_CHECKED
    if value is None:
        return NOT_AVAILABLE
    return (url or "yes") if value else ""


def _mobile_cell(value) -> str:
    """A plain yes/no about what the CRM holds, plus "not checked" when no CRM
    was asked at all. Not an emphasised gap: a missing mobile number is a fact
    about a contact record, not a defect in one.
    """
    if value == NOT_CHECKED:
        return NOT_CHECKED
    return "yes" if value else "no"


def _checked_against(cfg: Config) -> str:
    """Met this week's caption line naming the one CRM it was checked against,
    or "".

    A run-wide fact belongs in the caption rather than in a column repeating it
    on every row. With both CRMs configured the "In …?" columns already say it
    per row, and with none there is nothing to explain. Naming the vendor is
    right in a line that reports provenance; naming one that was not consulted
    is not.
    """
    names = []
    if cfg.hubspot.configured:
        names.append("HubSpot")
    if cfg.salesforce.configured:
        names.append("Salesforce")
    return f"Checked against {names[0]}." if len(names) == 1 else ""


def _band(profile: dict) -> str:
    lo = profile.get("employee_count_min")
    hi = profile.get("employee_count_max")
    if lo and hi:
        return f"{lo:,}–{hi:,} employees"
    if lo:
        return f"{lo:,}+ employees"
    if hi:
        return f"up to {hi:,} employees"
    return "any size"


def _profile_sentence(profile: dict, geo_label: str) -> str:
    """The ICP test in the reader's words, using their own numbers.

    A reader looking at a column of yes and no needs to know what the test was,
    and nothing else on the page tells them — so it states the values rather
    than naming the profile they came from.
    """
    return f"Meets profile? = {_band(profile)}, HQ in {geo_label}."


def build_workbook(
    cfg: Config,
    reconciled: list[dict],
    coverage: list[dict],
    suppressed: list[str],
    stakeholders: list[dict],
    out_path: str,
    profile: dict,
    geo_label: str,
    enrichment: Optional[str] = None,
    queue: Optional[list[dict]] = None,
    summary: Optional[list[dict]] = None,
) -> None:
    """`enrichment` is the configured provider's display name, or None.

    None is the default and changes nothing: no provider column, no tab 4. The
    same rule as a CRM that is not configured — an absent source contributes no
    column, rather than a column saying it was not asked.
    """
    other = enrichment
    wb = Workbook()
    wb.remove(wb.active)

    # Summary — first, unnumbered: it is not a list to work through but the
    # counts of the tabs that are. Built by weekly/summary.py.
    if summary is not None:
        ws_summary = _sheet(wb, "Summary", ["Tab", "What", "Count", "Out of"])
        for st in summary:
            ws_summary.append([st["area"], st["what"], st["count"], st["out_of"]])
        ws_summary.append([])
        # Said in words either way. An absent provider contributes no column
        # and no count, which is right — but it also means a run that was meant
        # to be enriched and was not looks exactly like one that never was.
        # This line is the only place the difference shows.
        ws_summary.append(
            [
                f"Enrichment: {other} — its findings are on the Review queue tab."
                if other
                else "Enrichment: not configured — no provider lookups and no "
                "review queue this run."
            ]
        )
        ws_summary.append(
            [
                "Each count is a count of rows on the tab named, so any of them can be "
                "checked there. The one exception is recent contact, which reads every "
                "senior CRM contact at the company, not only the people listed."
            ]
        )

    # Tab 1 — Met this week: everyone met, one row per person, and whether each
    # is in the CRM.
    #
    # One list rather than two: the people not in the CRM sort to the top, which
    # is what a second tab of them used to be for.
    #
    # The in-CRM column follows the cross-CRM rule: with both CRMs configured it
    # is the pair "In HubSpot?" / "In Salesforce?" — "in HubSpot but not
    # Salesforce" is the actionable answer, and a merged column would lose it.
    # With one, a single "In CRM?", and the caption names which. With none,
    # nothing: no CRM was asked. The CRM-derived columns are dropped with no CRM
    # for the same reason, rather than filled with "not checked".
    #
    # A person with no CRM record gets a dash in the CRM columns, never "no": a
    # "no" there asserts a fact about a record that does not exist.
    hs_on = cfg.hubspot.configured
    sf_on = cfg.salesforce.configured
    both_crms = hs_on and sf_on
    crm_on = sf_on or hs_on
    ws_met = _sheet(
        wb,
        "1 - Met this week",
        ["Name", "Email", "Company (domain)"]
        + (["In HubSpot?", "In Salesforce?"] if both_crms else ["In CRM?"] if crm_on else [])
        # "Title (CRM)", not "Title (SF)": the value comes from either CRM —
        # Salesforce wins, HubSpot is the fallback.
        + (["Title (CRM)", "LinkedIn (CRM)", "Mobile in CRM?"] if crm_on else [])
        + ([f"Name ({other})", f"Title ({other})", f"LinkedIn ({other})"] if other else [])
        + ["Flag", "Source"],
    )
    # Not in the CRM first — the most directly actionable rows. Stable, so the
    # order within each group is unchanged.
    for r in sorted(reconciled, key=lambda r: not missing_from_crm(r)):
        row = [r.get("attendee_name"), r.get("email", ""), r.get("domain")]
        if both_crms:
            row += ["yes" if r.get("in_hubspot") else "NO",
                    "yes" if r.get("in_salesforce") else "NO"]
        elif crm_on:
            row.append("yes" if in_any_crm(r) else "NO")
        if crm_on:
            if in_any_crm(r):
                row += [
                    r.get("title", ""),
                    _linkedin_cell(r.get("linkedin_in_crm"), r.get("linkedin_url_in_crm", "")),
                    _mobile_cell(r.get("mobile_in_crm")),
                ]
            else:
                row += [NO_RECORD, NO_RECORD, NO_RECORD]
        if other:
            # Filled for people not in the CRM, and for people in it with no
            # title; for everyone else the CRM already holds what the map needs,
            # and the stakeholder list is where the provider is set beside it.
            row += [r.get("other_name", ""), r.get("other_title", ""),
                    r.get("other_linkedin", "")]
        ws_met.append(row + [r.get("flag", ""), "gong"])
    notes = []
    checked = _checked_against(cfg)
    if checked:
        notes.append(checked)
    if crm_on:
        notes.append(
            "People not in the CRM are listed first. — in a CRM column means there "
            "is no CRM record to read."
            + (f" The {other} columns are enrichment from {other}, not your CRM, and are"
               " filled for people not in the CRM and for people in it with no title."
               if other else "")
        )
    if suppressed:
        notes.append(
            "Suppressed as non-contacts (no email/domain — likely meeting bots): "
            + ", ".join(suppressed)
        )
    if notes:
        ws_met.append([])
        for n in notes:
            ws_met.append([n])

    # Tab 2 — Company coverage (triage)
    #
    # Same rule as the CRM columns on tab 1, applied to counts: a provider that was not queried
    # contributes no column. A count column has to stay numeric to be sortable
    # and summable — writing "not checked" into it would turn the whole column
    # to text and quietly break sorting on the tab whose job is triage — so the
    # absence is expressed by dropping the column rather than by a value in it.
    ws_coverage = _sheet(
        wb,
        "2 - Company coverage",
        ["Company", "Company name", "Employees", "HQ", "Account type",
         "Meets profile?", "Met this wk"]
        + (["SF contacts", "SF focus-senior"] if sf_on else [])
        + (["HubSpot contacts"] if hs_on else [])
        # Numeric, like the count columns: a company the provider does not know
        # is blank here and says so in Profile check, rather than putting text
        # into a column that has to stay sortable.
        + ([f"Employees ({other})", f"HQ ({other})", "Profile check"] if other else []),
    )
    for c in sorted(coverage, key=lambda x: (not x.get("is_target"), -x.get("met", 0))):
        row = [
            c["domain"], c.get("name", ""), c.get("employees", ""), c.get("hq", ""),
            c.get("account_type", "") or "(blank)", c.get("meets", ""), c.get("met", 0),
        ]
        if sf_on:
            row += [c.get("sf_total", 0), c.get("sf_senior", 0)]
        if hs_on:
            row.append(c.get("hs_total", 0))
        if other:
            row += [c.get("other_employees"), c.get("other_hq", ""), c.get("verdict_check", "")]
        ws_coverage.append(row)
    ws_coverage.append([])
    ws_coverage.append(
        [
            _profile_sentence(profile, geo_label)
            + " A company with no employee count on file is excluded rather than"
            " given the benefit of the doubt. Account type is shown for context"
            " and is not used to filter."
        ]
    )
    if other:
        ws_coverage.append(
            [
                f"The {other} columns are a second opinion, not a correction — {other} can "
                "be out of date too. Profile check runs the same test on its numbers; "
                "a disputed company is on the stakeholder list, marked, and in the review "
                "queue."
            ]
        )

    # Tab 3 — Stakeholder list (the map)
    ws_map = _sheet(
        wb,
        "3 - Stakeholder list",
        ["Company", "Name", "Title", "Recent contact?", "LinkedIn", "Mobile in CRM?"]
        + (["Still at company?", f"Title ({other})", f"LinkedIn ({other})"] if other else []),
    )
    for r in stakeholders:
        company = r.get("company", "")
        if r.get("disputed"):
            company = f"{company} (profile disputed)"
        row = [company, r.get("name", ""), r.get("title", ""),
               r.get("contact", ""), r.get("linkedin", ""), r.get("mobile", "")]
        if other:
            row += [r.get("still_at", ""), r.get("other_title", ""), r.get("other_linkedin", "")]
        ws_map.append(row)
    ws_map.append([])
    # How the list is built, in the reader's own values — which companies, which
    # people, how many, in what order. A list of names with no stated rule reads
    # as a recommendation from nowhere. Only what a reader needs: design
    # rationale and ticket references belong in the repo and in Linear.
    also = []
    if any(c.get("disputed") for c in coverage):
        also.append("any whose profile fit is disputed")
    if any(not c.get("assessed", True) for c in coverage):
        also.append("any that could not be assessed")
    ws_map.append(
        [
            f"Companies met this week that meet your profile ({_band(profile)}, HQ in "
            f"{geo_label})"
            + (", plus " + " and ".join(also) if also else "")
            + f". For each, up to {cfg.shortlist_size} people from your CRM at "
            f"{seniority_prose(profile)} level, most senior first, then most recently "
            "contacted. People not in your CRM cannot appear here — they are on Met "
            "this week."
        ]
    )
    ws_map.append(
        [
            f"Recent contact = a meeting, or activity logged in the CRM, in the last "
            f"{cfg.recent_days} days. Titles come from the CRM and may be out of date."
        ]
    )
    if other:
        ws_map.append(
            [
                f"Still at company? is {other}'s view of where each person works now. "
                f"The {other} title and LinkedIn are filled only where they differ from "
                "the CRM's. Neither source settles it — LinkedIn does; see the review queue."
            ]
        )

    # Tab 4 — Review queue. Only with a provider: it holds the disagreements
    # between the CRM and that provider, and there are none to hold without one.
    if other:
        ws_queue = _sheet(
            wb,
            "4 - Review queue",
            ["What", "Company", "Person", "LinkedIn", "CRM says", f"{other} says", "Check"],
        )
        for q in queue or []:
            ws_queue.append(
                [q["kind"], q["company"], q["who"], q.get("linkedin", ""), q["crm"],
                 q["other"], q["check"]]
            )
        ws_queue.append([])
        ws_queue.append(
            [
                "Things for a person to settle, most consequential first. Nothing here "
                "has been changed anywhere — the CRM is updated by whoever works "
                "through this list."
            ]
        )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    wb.save(out_path)


def dump_inputs(path: str, payload: dict) -> None:
    """Every input the run used, so the ordering can be re-tuned without going
    back to Salesforce."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)


def count_gaps(stakeholders: list[dict]) -> int:
    return sum(1 for r in stakeholders if r.get("name") == NO_SENIOR_CONTACT)
