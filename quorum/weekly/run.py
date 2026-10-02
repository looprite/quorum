"""The weekly run — steps 1 through 6 in order.

Each step is in its own module and names the read path it serves; this file is
only the sequence. docs/pipeline.md is the prose version of exactly this.
"""

from __future__ import annotations

import json
import os

from .. import db, enrich, geography
from ..config import Config
from ..crm import fieldmap as fieldmap_mod
from ..crm.fieldmap import FieldMap
from ..crm.hubspot import HubSpot
from ..crm.salesforce import Salesforce
from . import coverage as coverage_mod
from . import enrichment as enrichment_mod
from . import people as people_mod
from . import retention as retention_mod
from . import stakeholders as stakeholders_mod
from . import summary as summary_mod
from . import view as view_mod
from . import workbook as workbook_mod


class MissingFocusProfile(RuntimeError):
    """No active focus profile for the account.

    Fatal rather than a warning, because the failure is invisible in the
    output: `meets_profile` treats an absent profile as "everything fits", so
    the run completes, the workbook has the usual shape, and every company met
    that week is reported as an ICP target. A reader cannot tell that from a
    correct run. `quorum init` creates one.
    """


class MissingFieldMap(RuntimeError):
    """Salesforce is configured but no field map has been resolved for it.

    Fatal for the same reason: the map is where the non-standard field names
    come from, so a run without one reads standard fields only — no headcount
    from a package field, no HQ, no LinkedIn — and the ICP test then judges
    every company on data it did not fetch. `quorum resolve-fields` writes one.
    """


class MissingRunOutputsTable(RuntimeError):
    """RETAIN_RUNS is on but run_outputs does not exist.

    Fatal for the same reason: without this check, the failure surfaces on the
    last line of the run, after every Gong and CRM call, as a raw "relation
    run_outputs does not exist" — instead of before any of them. Apply
    `migrations/0005_run_outputs.sql` and grant the pipeline's role INSERT and
    SELECT on it; see `docs/setup.md` §14.
    """


def run_weekly(cfg: Config, log=print) -> dict:
    start, end = cfg.week_bounds()
    log(f"[*] Week window: {start} .. {end}  account={cfg.account}")

    # Checked here, before anything is read or spent, because the failure it
    # catches is silent: a run whose window has not closed completes cleanly and
    # produces an artifact with no rows in it. Nothing downstream can tell that
    # apart from a genuinely quiet week, and a scheduled job has nobody reading
    # the output closely enough to ask.
    #
    # A warning rather than a refusal — a deliberate mid-week look is a
    # legitimate thing to want, and this is the run saying what it is about to
    # do, not stopping you doing it.
    remaining = cfg.week_days_remaining()
    if remaining > 0:
        log(
            f"[!] This week is not over — {7 - remaining:.1f} of 7 days have "
            f"elapsed, {remaining:.1f} still ahead. The artifact will cover only "
            f"the part that has happened, and run early enough it will have no "
            f"rows at all. Deliberate mid-week run: expected. Scheduled: set "
            f"WEEK_START, or move the schedule later in the week — "
            f"docs/setup.md §13."
        )

    sf = Salesforce(cfg)
    hs = HubSpot(cfg)
    if not sf.configured:
        log(
            "[i] Salesforce not configured — its columns are omitted, and the ICP "
            "test cannot run: tab 2 will read 'not assessed' rather than a verdict."
        )
    elif sf._cfg.uses_client_credentials:
        log("[i] Salesforce: client-credentials flow.")
    else:
        log("[i] Salesforce: pasted token (expires ~2h — see docs/salesforce-access.md).")
    if not hs.configured:
        log("[i] HubSpot not configured — its columns are omitted from tabs 1 and 2.")

    # The enrichment provider's one free call, made here — before the database
    # is read or a CRM is called — for the reason every other guard in this
    # function is at the top: a key that does not work would otherwise surface
    # after all of that had been paid for.
    provider = enrich.configured()
    if provider is None:
        log("[i] No enrichment provider configured — no provider columns, no review queue.")
    else:
        log(f"[i] Enrichment: {provider.display_name} — {provider.check()}")
    enriching = enrichment_mod.start(provider)

    with db.connect(cfg) as conn:
        # Read first, before any work: the profile carries the ICP test and the
        # seniority bar, so a run without one is wrong from step 4 onwards.
        # Failing here costs a second; failing where it is used costs every
        # Salesforce and HubSpot call made in between.
        profile = db.focus_profile(conn, cfg)
        if not profile:
            raise MissingFocusProfile(
                f"No active focus profile for account {cfg.account!r}. It carries "
                "the ICP test (employee band, HQ geography) and the seniority bar, "
                "and without one every company met would be reported as an ICP "
                "target — an artifact that looks normal and is wrong. "
                "Create one with `quorum init`."
            )
        # Validated here, before any work, rather than per company inside the
        # ICP test: a geography the test cannot act on is a broken profile, and
        # it used to mean no geography filter was applied at all.
        selections = geography.parse_selections(profile.get("hq_geographies"))
        log(
            f"[*] Focus profile: emp {profile.get('employee_count_min')}-"
            f"{profile.get('employee_count_max')}, geo {geography.label(selections)}, "
            f"seniority {profile.get('focus_seniority')}"
        )

        # The resolved CRM field map, for the same reason and at the same point:
        # it decides which fields every Salesforce query below asks for.
        field_map = db.crm_field_map(conn, cfg)
        if sf.configured and not field_map:
            raise MissingFieldMap(
                f"Salesforce is configured but account {cfg.account!r} has no "
                "resolved CRM field map. Every query would fall back to standard "
                "fields only — no headcount, HQ or LinkedIn from this org's own "
                "fields — and the ICP test would judge companies on data that was "
                "never fetched. Resolve one with `quorum resolve-fields`."
            )
        sf.fields = FieldMap(field_map)
        for line in fieldmap_mod.describe_lines(field_map):
            log(f"[i] Field map: {line}")

        # Retention, checked here for the same reason as the two guards above:
        # it is only used at the very end, after emitting the three files, so
        # an unapplied migration would otherwise surface there — after every
        # Gong and CRM call this run makes.
        if cfg.retain_runs:
            with conn.cursor() as cur:
                cur.execute("select to_regclass('run_outputs')")
                exists = cur.fetchone()[0]
            if exists is None:
                raise MissingRunOutputsTable(
                    "RETAIN_RUNS is on but the run_outputs table does not exist. "
                    "Apply migrations/0005_run_outputs.sql and grant the "
                    "pipeline's role INSERT and SELECT on it — see "
                    "docs/setup.md §14."
                )

        # Step 1 — the week's external attendees
        rows = db.week_attendees(conn, cfg)
        rows, suppressed = people_mod.suppress_non_contacts(rows)
        if suppressed:
            log(f"[i] Suppressed {len(suppressed)} non-contact(s): {', '.join(suppressed)}")

        # Step 2 — distinct people, grouped by company domain
        people = people_mod.dedupe_people(rows)
        companies = people_mod.group_companies(people)
        log(
            f"[*] {len(rows)} attendee-rows → {len(people)} distinct people, "
            f"{len(companies)} companies"
        )

        # Step 3 — reconcile against the CRM
        reconciled = [people_mod.reconcile(p, sf, hs) for p in people]
        total = len(reconciled)
        with_mobile = sum(1 for r in reconciled if r.get("mobile_in_crm"))
        with_linkedin = sum(1 for r in reconciled if r.get("linkedin_in_crm"))
        log(f"[*] Mobile in CRM: {with_mobile}/{total} — {total - with_mobile} gaps")
        log(f"[*] LinkedIn in CRM: {with_linkedin}/{total}")

        # Step 4 — company coverage (triage). The profile it filters on was
        # read and required at the top of this block.
        log(f"[*] Building company coverage for {len(companies)} companies...")
        coverage = coverage_mod.build_coverage(cfg, companies, profile, sf, hs)

        # Before the map is chosen: a company whose verdict the provider
        # disputes goes onto the stakeholder list, so this has to have run
        # before companies_for_map() is asked.
        if enriching:
            enrichment_mod.companies(enriching, coverage, profile)
            disputed = sum(1 for c in coverage if c.get("disputed"))
            log(
                f"[*] {provider.display_name}: {enriching.companies_looked_up} companies "
                f"looked up, {disputed} ICP verdict(s) disputed"
            )

        type_counts = coverage_mod.observed_account_types(coverage)
        log(f"[*] Account.Type values observed: {type_counts}")

        targets = [c for c in coverage if c["is_target"]]
        # Not targets and not rejections: companies the ICP test could not run
        # on at all. Counted separately, because folding them into either
        # number is the misreport this whole path exists to prevent.
        undetermined = [c for c in coverage if not c.get("assessed", True)]
        if undetermined:
            log(
                f"[!] ICP test did not run for {len(undetermined)} of {len(coverage)} "
                "companies — no CRM configured, so no firmographics were fetched. "
                "They are reported as 'not assessed' on tabs 2 and 3, not dropped."
            )
        if cfg.customer_account_types:
            fits = sum(1 for c in coverage if c["meets"] == "yes")
            gated = [c["domain"] for c in coverage if c["meets"] == "yes" and c["is_customer"]]
            log(
                f"[*] ICP fit: {fits} meet the profile; {len(targets)} after the "
                f"Account.Type gate {list(cfg.customer_account_types)}"
            )
            if gated:
                log(f"[i] Gated as existing customers: {', '.join(gated)}")
        else:
            log(
                f"[*] ICP fit: {len(targets)} companies (employee band + HQ geography). "
                "Account.Type captured for context, not used as a filter."
            )

        # Step 5 — the stakeholder list
        terms = coverage_mod.seniority_terms(profile)
        # The same set tab 3 will render, so history is fetched for every company
        # that reaches the map rather than for confirmed targets alone.
        mapped = stakeholders_mod.companies_for_map(coverage)
        history = db.met_history(conn, cfg, [c["domain"] for c in mapped])
        log(
            f"[*] Meeting history: {len(history)} distinct people ever met across "
            f"{len(mapped)} companies on the map"
        )
        stakeholders, bench_raw = stakeholders_mod.build(cfg, coverage, terms, history, sf)
        gaps = workbook_mod.count_gaps(stakeholders)
        unassessed = sum(
            1 for r in stakeholders if r.get("name") == stakeholders_mod.ICP_NOT_ASSESSED
        )
        log(
            f"[*] Stakeholder list: {len(stakeholders) - gaps - unassessed} people "
            f"across {len(targets)} target companies ({gaps} with no senior CRM "
            f"contact" + (f", {len(undetermined)} not assessed" if unassessed else "") + ")"
        )

        queue: list[dict] = []
        if enriching:
            enrichment_mod.stakeholders(enriching, stakeholders)
            enrichment_mod.not_in_crm(enriching, reconciled)
            enrichment_mod.check_withheld(enriching)
            queue = enrichment_mod.review_queue(enriching, coverage, stakeholders)
            moved = sum(1 for r in stakeholders if str(r.get("still_at", "")).startswith("no —"))
            on_email = sum(1 for r in stakeholders if r.get("matched_on") == "email")
            on_linkedin = sum(1 for r in stakeholders if r.get("matched_on") == "LinkedIn")
            log(
                f"[*] {provider.display_name}: {enriching.people_looked_up} people looked "
                f"up by email and {enriching.linkedin_looked_up} by LinkedIn URL; "
                f"stakeholders matched on email {on_email}, on LinkedIn {on_linkedin}; "
                f"{moved} may have left, {len(queue)} item(s) in the review queue"
                + (
                    f"; {enriching.withheld} lookup(s) withheld by {provider.display_name}"
                    + (f" (codes: {', '.join(sorted(enriching.withheld_codes))})"
                       if enriching.withheld_codes else "")
                    if enriching.withheld else ""
                )
            )

        describe = sf.describe_contact() if sf.configured else {"checked": False}
        if describe.get("checked"):
            log(f"[*] Contact.describe: {describe['field_count']} fields")

    # Step 6 — emit
    week = start[:10]
    summary = summary_mod.build(
        cfg, reconciled, coverage, stakeholders, bench_raw, profile,
        enrichment=provider.display_name if provider else None,
        queue=queue,
        queue_kinds=enrichment_mod.QUEUE_ORDER,
    )
    xlsx_path = os.path.join(cfg.output_dir, f"weekly_stakeholder_map_{week}.xlsx")
    workbook_mod.build_workbook(
        cfg, reconciled, coverage, suppressed, stakeholders, xlsx_path,
        # The ICP test states itself on tab 2 in the reader's own numbers, so
        # the profile and the geography label it was parsed into both have to
        # reach the workbook rather than being described in the abstract.
        profile=profile,
        geo_label=geography.prose_label(selections),
        enrichment=provider.display_name if provider else None,
        queue=queue,
        summary=summary,
    )
    log(f"[✓] Wrote {xlsx_path}")

    json_path = os.path.join(cfg.output_dir, f"stakeholder_inputs_{week}.json")
    workbook_mod.dump_inputs(
        json_path,
        {
            "week_start": week,
            "account": cfg.account,
            "focus_profile": profile,
            "crm_field_map": field_map,
            "seniority_terms": terms,
            "account_type_values_observed": type_counts,
            "customer_gate_patterns": list(cfg.customer_account_types),
            "coverage": coverage,
            "met_history": {
                k: {kk: str(vv) for kk, vv in v.items()} for k, v in history.items()
            },
            "sf_bench": bench_raw,
            "stakeholders": stakeholders,
            "contact_describe": describe,
            # Which provider's view sits beside the CRM's in coverage and
            # stakeholders above, or null. Null means none was asked.
            "enrichment_provider": provider.display_name if provider else None,
            "review_queue": queue,
            # Also here, not only in its own file: this dump is what retention
            # keeps, and counts over time are what the summary is for.
            "summary": summary,
        },
    )
    log(f"[✓] Wrote {json_path}")

    # Its own small file, named by the manifest, so a delivery step can post
    # the counts without opening the workbook or parsing the log.
    summary_path = os.path.join(cfg.output_dir, f"summary_{week}.json")
    summary_mod.write(
        summary_path, week, summary,
        enrichment=provider.display_name if provider else None,
        recent_days=cfg.recent_days,
    )
    log(f"[✓] Wrote {summary_path}")

    html_path = view_mod.render(xlsx_path, cfg.account)
    log(f"[✓] Wrote {html_path}")

    if cfg.retain_runs:
        retention_mod.store(
            cfg, week, {"xlsx": xlsx_path, "json": json_path, "html": html_path}
        )
        log(f"[✓] Retained run {week} in run_outputs (xlsx, json, html)")
    else:
        log("[i] RETAIN_RUNS is off — this run's files are not stored in the database.")

    # The manifest is the contract anything downstream reads to find this run's
    # files — a delivery step, an archival step, a notification. Without it the
    # only ways to locate them are reconstructing the filename pattern or
    # scraping the "[✓] Wrote …" lines above, and neither is a thing this
    # project versions: a rename or a log tweak would break every deployment's
    # delivery step silently, at the end of a run, after every Gong read and
    # every CRM call have already been paid for.
    #
    # Written last, so its presence means the run finished — retention included,
    # since that raises rather than returning. Paths are absolute because the
    # reader is a separate process that need not share this one's cwd.
    #
    # Nothing here needs a precondition check at the start of the run: the three
    # writes above have already proven output_dir is writable.
    manifest = {
        "schema": 1,
        "week_start": week,
        "xlsx": os.path.abspath(xlsx_path),
        "json": os.path.abspath(json_path),
        "html": os.path.abspath(html_path),
        # Added after the manifest first shipped, without a schema bump: a key
        # a reader does not know is one it can ignore. A reader that wants it
        # has to allow for its absence on a run from an older version.
        "summary": os.path.abspath(summary_path),
    }
    manifest_path = os.path.join(cfg.output_dir, "last_run.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    log(f"[✓] Wrote {manifest_path}")

    return {
        "xlsx": manifest["xlsx"],
        "json": manifest["json"],
        "html": manifest["html"],
        "summary": manifest["summary"],
        "week_start": week,
        "manifest": manifest_path,
    }
