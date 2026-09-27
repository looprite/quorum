"""Step 4 — company coverage. Triage: which companies warrant a map.

Serves every column of tab 2, and the ICP filter that decides which companies
reach tab 3's stakeholder list.
"""

from __future__ import annotations

from typing import Optional

from .. import geography
from ..config import Config
from ..crm.hubspot import HubSpot
from ..crm.salesforce import Salesforce

# Focus-profile seniority levels expanded into the title fragments a CRM
# actually contains. The profile says "vp"; Salesforce says "SVP", "Vice
# President". These began as a tier rule that assigned an outreach action per
# person; that was removed 2026-08-24. The matching survives because the
# ordering still needs it.
SENIORITY_KEYWORDS = {
    "cro": ["CRO", "Chief Revenue"],
    "cxo": ["Chief", "CEO", "CFO", "CTO", "COO", "CMO"],
    "c-level": ["Chief", "CEO", "CFO", "CTO", "COO", "CMO"],
    "vp": ["VP", "Vice President", "SVP", "EVP"],
    "director": ["Director", "Head of"],
    "founder": ["Founder", "Owner"],
    "manager": ["Manager"],
}

# How each focus-profile level reads in a sentence, for the stakeholder list's
# caption. Anything not listed is shown as the profile holds it.
_LEVEL_PROSE = {
    "cro": "CRO", "cxo": "C-suite", "c-level": "C-suite", "vp": "VP",
    "director": "Director", "founder": "Founder", "manager": "Manager",
}


def seniority_prose(profile: dict) -> str:
    """The profile's seniority levels as a reader would say them: "CRO, VP or
    Director". With none set, the default the bench query uses."""
    levels = [str(x).strip() for x in (profile or {}).get("focus_seniority") or [] if str(x).strip()]
    words = [_LEVEL_PROSE.get(x.lower(), x) for x in levels] or ["C-suite", "VP", "Director"]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def seniority_terms(profile: dict) -> list[str]:
    terms: list[str] = []
    for level in profile.get("focus_seniority") or []:
        terms += SENIORITY_KEYWORDS.get(str(level).strip().lower(), [str(level)])
    return terms or ["VP", "Vice President", "Chief", "Director", "Head of"]


# The ICP verdict when the firmographics behind it were never fetched. A
# company that was never measured has not failed the test — it has not taken
# it. Phrased as "verdict — reason", the shape the artifact already uses for
# recent contact ("no — none on record") and for the target column.
NOT_ASSESSED = "not assessed — no CRM configured"


def meets_profile(
    profile: dict, employees, country: str, *, firmographics_fetched: bool = True
) -> tuple[Optional[bool], str]:
    """The ICP test: employee band and HQ geography. Returns (ok, why-not).

    **`ok` has three values, not two.** True and False are verdicts. None means
    the test did not run, because the firmographics it reads were never
    fetched — the CRM they come from is not configured.

    Without that third state, an unconfigured CRM returns empty strings here
    and every company fails on "no size": a judgement asserted about data
    nobody looked up. It is not cosmetic, because this test is also the filter
    feeding tab 3, so the same absence silently empties the stakeholder list —
    the mirror image of the MissingFocusProfile failure in `weekly/run.py`,
    where an absent profile makes the test pass everything instead.

    None is the idiom the rest of the pipeline already uses for "not checked":
    `in_salesforce`, `linkedin_in_crm` and tab 2's contact counts all use it,
    for exactly this reason.

    `country` is the HQ country on its own, not the joined display string. The
    test that preceded this one substring-matched a country list against
    "city, state, country", which is why the list held " us" with a leading
    space — to stop it matching inside "Australia".

    Two failures, kept apart: a country outside the selection is a decision the
    profile made, and no country at all is missing CRM data. They read the same
    before this, and a reader could not tell which they were looking at.
    """
    # First, because every reason below would otherwise be an artefact of the
    # absence rather than a finding about the company.
    if not firmographics_fetched:
        return None, NOT_ASSESSED
    # An absent profile passing everything is why run_weekly refuses to start
    # without one (MissingFocusProfile). Kept as the answer for a caller that
    # has no profile to give, not as a mode the weekly run can reach.
    if not profile:
        return True, ""
    reasons: list[str] = []
    try:
        emp = int(employees)
    except (TypeError, ValueError):
        emp = None

    emin, emax = profile.get("employee_count_min"), profile.get("employee_count_max")
    if emp is None:
        reasons.append("no size")
    else:
        if emin and emp < emin:
            reasons.append(f"<{emin} emp")
        if emax and emp > emax:
            reasons.append(f">{emax} emp")

    selections = geography.parse_selections(profile.get("hq_geographies"))
    if selections:
        if not str(country or "").strip():
            reasons.append("HQ unknown")
        elif not geography.matches(selections, country):
            reasons.append(f"HQ not {geography.label(selections)}")

    return (not reasons), "; ".join(reasons)


def is_customer(cfg: Config, account_type: Optional[str]) -> bool:
    """The optional Account.Type gate, OFF unless configured.

    Type is a per-org picklist, so this matches substrings rather than
    hardcoding labels, and a run dumps the values it actually observed before
    the gate is applied. Off by default because Type is commonly not maintained
    as a lifecycle field — in one org, gating on it removed nearly the whole
    ICP-fit set for the week.
    """
    t = (account_type or "").strip().lower()
    if not t or not cfg.customer_account_types:
        return False
    return any(p in t for p in cfg.customer_account_types)


def build_coverage(
    cfg: Config,
    companies: dict[str, dict],
    profile: dict,
    sf: Salesforce,
    hs: HubSpot,
) -> list[dict]:
    terms = seniority_terms(profile)
    coverage: list[dict] = []

    for domain in companies:
        stats = sf.domain_stats(domain, terms)
        firmo = sf.account_firmographics(stats.get("account_id"))
        ok, why = meets_profile(
            profile,
            firmo.get("employees"),
            firmo.get("country"),
            firmographics_fetched=sf.configured,
        )
        assessed = ok is not None
        account_type = firmo.get("account_type", "")
        customer = is_customer(cfg, account_type)

        if not assessed:
            target = NOT_ASSESSED
        elif ok and not customer:
            target = "yes"
        elif customer:
            target = f"no — customer (Type: {account_type})"
        else:
            target = f"no — {why or 'profile'}"

        coverage.append(
            {
                "domain": domain,
                "name": firmo.get("name", ""),
                "employees": firmo.get("employees", ""),
                "hq": firmo.get("hq", ""),
                "account_type": account_type,
                "meets": "yes" if ok else (why or "no"),
                # The customer gate applies to the ICP-fit count, not only to the
                # shortlist — otherwise the triage number overcounts fit.
                "target": target,
                # Three states, kept apart. `is_target` is a CONFIRMED target,
                # so it is False both for a company that failed the test and for
                # one the test could not run on — which is why filtering on it
                # alone drops the second kind out of tab 3 entirely. `assessed`
                # is what tells them apart; see stakeholders.companies_for_map.
                "assessed": assessed,
                "is_target": bool(ok) and not customer,
                "is_customer": customer,
                "met": len(companies[domain]["people"]),
                # None, not 0, when the provider was never queried. A bare 0
                # in a count column is a measurement that was never taken, and
                # it reads identically to a company with genuinely no contacts.
                # The workbook drops the column entirely; the JSON dump carries
                # the null so the same distinction survives into the inputs.
                "sf_total": stats.get("sf_total", 0) if sf.configured else None,
                "sf_senior": stats.get("sf_senior", 0) if sf.configured else None,
                "hs_total": hs.count_domain(domain) if hs.configured else None,
            }
        )

    return coverage


def observed_account_types(coverage: list[dict]) -> dict[str, int]:
    """The distinct Account.Type values AS OBSERVED, counted before any gate is
    applied — so the gate can be configured from a customer's real picklist
    rather than assumed labels."""
    counts: dict[str, int] = {}
    for c in coverage:
        key = c.get("account_type") or "(blank)"
        counts[key] = counts.get(key, 0) + 1
    return counts
