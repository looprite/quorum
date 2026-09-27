"""End-to-end over the two legs a session can actually reach: the product
database and the artifact. Salesforce and HubSpot are deliberately absent."""

from __future__ import annotations

import json

import psycopg
import pytest
import requests
from openpyxl import load_workbook

from quorum import db
from quorum.config import Config
from quorum.gong.importer import import_range
from quorum.weekly.run import run_weekly
from quorum.weekly.stakeholders import NO_SENIOR_CONTACT

ACCOUNT = "northwind.com"


def _seed_account(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "insert into accounts (name, internal_domains) values (%s, %s) returning id",
            (ACCOUNT, ["northwind.com"]),
        ).fetchone()
    return str(row[0])


def _cfg(dsn: str, tmp_path, **overrides) -> Config:
    kwargs = dict(
        database_url=dsn,
        account=ACCOUNT,
        week_start="2026-08-17",
        output_dir=str(tmp_path),
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _seed_profile(dsn: str, account_id: str) -> None:
    """An active focus profile — run_weekly refuses to start without one."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "insert into user_focus_profiles (account_id, version_number, is_active, "
            "profile_data) values (%s, 1, true, %s)",
            (account_id, json.dumps({
                "employee_count_min": 200, "employee_count_max": 10000,
                "hq_geographies": ["North America"],
                "focus_seniority": ["c-level", "vp", "director"],
            })),
        )


def _import(dsn: str, account_id: str, gong_calls):
    from tests.conftest import FakeGong

    with psycopg.connect(dsn) as conn:
        result = import_range(
            conn, FakeGong(gong_calls), account_id, ["northwind.com"],
            "2025-10-01", "2026-08-24", log=lambda *_: None,
        )
        conn.commit()
    return result


def test_import_classifies_and_resolves(database, gong_calls):
    account_id = _seed_account(database)
    result = _import(database, account_id, gong_calls)

    assert result.meetings_upserted == 3

    with psycopg.connect(database) as conn:
        kinds = dict(
            conn.execute(
                "select coalesce(domain_kind, '<null>'), count(*) from attendees "
                "group by 1"
            ).fetchall()
        )
        # Gong's affiliation wins where present; domain classification fills the
        # rest. The party with neither name nor email is not a person.
        assert kinds["internal"] == 2
        assert kinds["personal"] == 1          # sam@gmail.com
        assert kinds["<null>"] == 1            # named, no email, no affiliation
        assert kinds["external"] == 12         # Dana x2, support@, 9 trainees

        # Internal attendees never become people.
        emails = {
            r[0]
            for r in conn.execute(
                "select email from people where email is not null"
            ).fetchall()
        }
        assert "sam@northwind.com" not in emails
        assert {"dana.reyes@acme.com", "d.reyes@acme.com"} <= emails

        # The name-only attendee gets an unmatched person of its own.
        assert conn.execute(
            "select count(*) from people where unmatched"
        ).fetchone()[0] == 1


def test_import_times_the_run_it_just_did(database, gong_calls):
    """Counts alone cannot answer 'what would a year cost?'. The elapsed time
    is measured around the real work, not assembled by the caller."""
    account_id = _seed_account(database)
    result = _import(database, account_id, gong_calls)

    assert result.elapsed_seconds > 0
    assert str(result).endswith("elapsed")


def test_reimport_is_idempotent(database, gong_calls):
    """The failure this guards: an importer that inserts person_attendees with no uniqueness
    constraint. 0002 adds one, so a second run over an overlapping range would
    raise instead of doing nothing — a backfill that looks fine, then breaks the
    overnight job the next day."""
    account_id = _seed_account(database)
    first = _import(database, account_id, gong_calls)

    def counts() -> tuple:
        with psycopg.connect(database) as conn:
            return tuple(
                conn.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ("meetings", "attendees", "people", "person_attendees",
                          "person_identifiers")
            )

    before = counts()
    second = _import(database, account_id, gong_calls)
    assert counts() == before
    assert second.calls_already_imported == 3
    assert second.attendees_created == 0
    assert first.meetings_upserted == second.meetings_upserted


def test_weekly_runs_without_a_crm(database, gong_calls, tmp_path):
    """No Salesforce, no HubSpot. The run must still produce the artifact, with
    the missing legs stated rather than silently absent."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)

    _seed_profile(database, account_id)

    cfg = _cfg(database, tmp_path)
    paths = run_weekly(cfg, log=lambda *_: None)

    wb = load_workbook(paths["xlsx"])
    assert wb.sheetnames == [
        "Summary",
        "1 - Met this week",
        "2 - Company coverage",
        "3 - Stakeholder list",
    ]

    # 11 distinct external people met in the pinned week: Dana, support@, and
    # nine trainees. The historical call and the internal attendees are out.
    # Caption rows (column A alone) are not people.
    met = [
        r for r in wb["1 - Met this week"].iter_rows(min_row=2, values_only=True)
        if any(r[1:])
    ]
    assert len(met) == 11

    # No CRM was queried, so nobody can be reported as missing from one. The
    # in-CRM column that would have said so is not rendered at all — see
    # test_tabs_omit_a_crm_that_was_never_queried.
    assert "In CRM?" not in _headers(wb["1 - Met this week"])

    dump = json.loads(open(paths["json"]).read())
    assert dump["account"] == ACCOUNT
    assert dump["focus_profile"]["employee_count_min"] == 200

    html = open(paths["html"]).read()
    assert "northwind.com" in html     # the account, not a hardcoded name
    assert "not for publishing" in html


@pytest.mark.parametrize(
    "name, email, expected",
    [
        # A role inbox is a role inbox whether or not a name came with it. The
        # meeting source labels `amer-bdr@` as "AMER BDR", and a real person's
        # name can sit on a shared `jobs@` address — treating the name as proof
        # of a person meant neither was ever flagged, and the Flag column on
        # tab 1 was empty for every row as a result.
        ("Casey Morrow", "jobs@example.com", "shared inbox — verify"),
        (None, "support@example.com", "shared inbox — verify"),
        # A person keeps their name, and a nameless one still needs enriching.
        ("Dana Reyes", "dana@example.com", ""),
        (None, "dana@example.com", "needs enrichment"),
        (None, None, "needs enrichment"),
    ],
)
def test_a_role_inbox_is_flagged_even_when_it_has_a_name(name, email, expected):
    from quorum.weekly.people import person_flag

    assert person_flag(name, email) == expected


def test_the_icp_test_states_itself_in_the_readers_numbers(
    database, gong_calls, tmp_path
):
    """Tab 3's caption used to name "your focus profile" — a term the reader of
    the file has never met — and then decline to say what was in it. Someone
    looking at a column of yes and no has no other way to learn what the test
    was."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)
    ws = load_workbook(paths["xlsx"])["2 - Company coverage"]
    caption = " ".join(
        str(r[0]) for r in ws.iter_rows(min_row=2, values_only=True)
        if r[0] and str(r[0]).startswith("Meets profile?")
    )

    # The seeded profile's own values, not a description of where they live.
    assert "200–10,000 employees" in caption
    # Written out, not the "NA" shorthand the narrow HQ column uses: the reader
    # of this caption has no column context to decode it from.
    assert "HQ in North America" in caption
    assert "focus profile" not in caption


def test_a_week_that_has_not_finished_says_so(database, gong_calls, tmp_path):
    """The failure this catches is silent, which is why it is a log line rather
    than a column: a run whose window has not closed completes cleanly and
    produces an artifact with no rows. Nothing downstream can tell that apart
    from a genuinely quiet week, and a scheduled job has nobody looking."""
    import datetime as dt

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    # The week every other test pins is long past. No warning.
    logged: list[str] = []
    run_weekly(_cfg(database, tmp_path), log=logged.append)
    assert not any("not over" in line for line in logged)

    # The current week always has time left in it, whatever day the suite runs:
    # the window ends next Monday 00:00 and now is necessarily before that.
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    logged = []
    run_weekly(
        _cfg(database, tmp_path, week_start=this_monday.isoformat()),
        log=logged.append,
    )
    warnings = [line for line in logged if "This week is not over" in line]
    assert len(warnings) == 1
    # Names where to fix it, not just that something is off.
    assert "WEEK_START" in warnings[0] and "§13" in warnings[0]

    # And it is a warning, not a refusal — the run still produced its files.
    assert (tmp_path / "last_run.json").exists()


@pytest.mark.parametrize(
    "stored, handle",
    [
        ("https://www.linkedin.com/in/gregory-sherman-b91", "gregory-sherman-b91"),
        # The shape that broke it: no scheme, so the old check ("starts with
        # http") neither linked it nor shortened it — leaving a long raw string
        # that the column's ellipsis then truncated into something a reader can
        # neither click nor copy.
        ("www.linkedin.com/in/gregory-sherman-b91", "gregory-sherman-b91"),
        ("linkedin.com/in/brigreene", "brigreene"),
        ("https://linkedin.com/in/dana/", "dana"),
        ("https://uk.linkedin.com/in/someone?trk=x", "someone"),
    ],
)
def test_a_linkedin_profile_renders_short_and_clickable(stored, handle):
    from quorum.weekly.view import cell

    _, rendered = cell("LinkedIn", stored)
    assert rendered == (
        f'<a href="https://www.linkedin.com/in/{handle}">{handle}</a>'
    )


@pytest.mark.parametrize(
    "stored, href, label",
    [
        # A member ID: LinkedIn redirects it to the real profile, so it is kept
        # as the link — but as text it is gibberish, so it is not the label.
        (
            "https://www.linkedin.com/in/ACwAAAB1c2VyLWlkLWV4YW1wbGUtMDAx",
            "https://www.linkedin.com/in/ACwAAAB1c2VyLWlkLWV4YW1wbGUtMDAx",
            "LinkedIn profile",
        ),
        (
            "linkedin.com/in/ACoAAAB1c2VyLWlkLWV4YW1wbGUtMDAy/",
            "https://www.linkedin.com/in/ACoAAAB1c2VyLWlkLWV4YW1wbGUtMDAy",
            "LinkedIn profile",
        ),
        # Sales Navigator: no public equivalent can be derived, and it opens
        # only for someone with Sales Navigator, which the label says.
        (
            "https://www.linkedin.com/sales/people/ACwAAAB1c2Vy,NAME_SEARCH,abcd",
            "https://www.linkedin.com/sales/people/ACwAAAB1c2Vy,NAME_SEARCH,abcd",
            "Sales Nav only",
        ),
        (
            "www.linkedin.com/sales/lead/ACwAAAB1c2Vy,NAME_SEARCH,abcd",
            "https://www.linkedin.com/sales/lead/ACwAAAB1c2Vy,NAME_SEARCH,abcd",
            "Sales Nav only",
        ),
        # A handle that happens to start with "ac" is still a handle.
        ("https://www.linkedin.com/in/acwaa-smith", "https://www.linkedin.com/in/acwaa-smith",
         "acwaa-smith"),
    ],
)
def test_linkedin_ids_and_sales_nav_render_as_a_short_label(stored, href, label):
    from quorum.weekly.view import cell

    _, rendered = cell("LinkedIn", stored)
    assert rendered == f'<a href="{href}">{label}</a>'


@pytest.mark.parametrize(
    "history, activity, expected",
    [
        ({"last_met": "{d}", "smallest_meeting": 26}, None, "yes — {d} (group call)"),
        ({"last_met": "{d}", "smallest_meeting": 3}, None, "yes — {d} (met)"),
        (None, "{d}", "yes — {d} (CRM activity)"),
    ],
)
def test_recent_contact_puts_the_date_first(history, activity, expected):
    """The column is narrow. With the date last, "group call, 26 attendees"
    pushed it into the ellipsis — losing the part that says how recent. Date
    first means truncation cuts the least useful part."""
    import datetime as dt
    from types import SimpleNamespace

    from quorum.weekly.stakeholders import recent_contact

    d = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    cfg = SimpleNamespace(recent_days=90, group_call_min=8)
    if history:
        history = {k: (v.format(d=d) if isinstance(v, str) else v) for k, v in history.items()}
    got = recent_contact(cfg, history, activity.format(d=d) if activity else None)

    assert got == expected.format(d=d)
    assert "attendees" not in got


def test_tab_1_states_what_was_never_asked(database, gong_calls, tmp_path):
    """With no CRM configured, tab 1's CRM-derived columns must say the
    question was not asked. They used to assert answers: `GAP` in red on every
    row, `needs title` on every named attendee, and a blank LinkedIn cell
    indistinguishable from 'we looked and found none'.

    The three CRM-derived columns are dropped, the same answer the coverage tab
    gives for a provider that was never queried — the tab survives because who
    attended comes from the meeting source, not the CRM. So is the in-CRM
    column: with no CRM there is nothing to be in."""
    from quorum.crm.fieldmap import NOT_CHECKED

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)
    ws = load_workbook(paths["xlsx"])["1 - Met this week"]

    # Enumerated, not counted: a column added or removed fails here visibly.
    assert _headers(ws) == ["Name", "Email", "Company (domain)", "Flag", "Source"]

    flag = _headers(ws).index("Flag")
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if any(r[1:])]
    assert rows, "no attendees rendered — the rest of this test proves nothing"

    # "needs title" is a finding about a CRM. None was asked, so it cannot be
    # asserted about anyone.
    assert not any("needs title" in (r[flag] or "") for r in rows)

    # The rest is asserted against reconcile() directly: the fixture has no
    # nameless attendee, so going through the workbook would leave the
    # name-check half of this proving nothing.
    from quorum.crm.hubspot import HubSpot
    from quorum.crm.salesforce import Salesforce
    from quorum.weekly.people import reconcile

    cfg = _cfg(database, tmp_path)
    sf, hs = Salesforce(cfg), HubSpot(cfg)
    assert not sf.configured and not hs.configured

    # The distinction still exists in the data even though no column shows it,
    # so nothing downstream has to re-derive which legs ran.
    named = reconcile({"email": "d@northwind.com", "attendee_name": "Dana"}, sf, hs)
    assert named["mobile_in_crm"] == NOT_CHECKED
    assert named["linkedin_in_crm"] == NOT_CHECKED
    assert "needs" not in named["flag"]

    # The name check is not CRM-derived and must survive — it comes from the
    # meeting, so it is still answerable with no CRM at all.
    nameless = reconcile({"email": "x@northwind.com", "attendee_name": ""}, sf, hs)
    assert nameless["flag"] == "needs name"


def test_manifest_names_this_runs_files(database, gong_calls, tmp_path):
    """The manifest is what a delivery step reads instead of reconstructing the
    filename pattern or scraping the run's log. Enumerated here rather than
    counted, so a change to its shape fails visibly."""
    import os

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    manifest_path = tmp_path / "last_run.json"
    assert manifest_path.exists()
    assert paths["manifest"] == str(manifest_path)

    manifest = json.loads(manifest_path.read_text())
    assert sorted(manifest) == ["html", "json", "schema", "summary", "week_start", "xlsx"]
    # `summary` was added without a bump: new keys are ignorable, so a reader
    # pinned to schema 1 keeps working.
    assert manifest["schema"] == 1
    assert manifest["week_start"] == "2026-08-17"

    for key in ("xlsx", "json", "html", "summary"):
        # Absolute, because the reader is a separate process that need not
        # share this one's working directory.
        assert os.path.isabs(manifest[key])
        assert os.path.exists(manifest[key])
        assert manifest[key] == paths[key]


def test_the_summary_is_a_tab_a_file_and_part_of_the_dump(database, gong_calls, tmp_path):
    """Three places, one set of numbers: the Summary tab a reader sees first,
    the small file a delivery step posts from, and the inputs dump retention
    keeps — which is what makes the counts comparable week to week."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    wb = load_workbook(paths["xlsx"])
    assert wb.sheetnames[0] == "Summary"
    assert _headers(wb["Summary"]) == ["Tab", "What", "Count", "Out of"]

    summary = json.loads(open(paths["summary"]).read())
    assert summary["schema"] == 1
    assert summary["week_start"] == "2026-08-17"
    stats = {s["key"]: s for s in summary["stats"]}
    # The same count as the rows on tab 1: the 11 people the no-CRM test
    # counts there.
    assert stats["people_met"]["count"] == 11
    # No CRM in this configuration, so nothing a CRM would have answered.
    assert "people_not_in_crm" not in stats

    dump = json.loads(open(paths["json"]).read())
    assert dump["summary"] == summary["stats"]

    html = open(paths["html"]).read()
    assert html.index("<h2>Summary") < html.index("<h2>Company coverage")


def test_manifest_is_written_only_after_retention_succeeds(
    database, gong_calls, tmp_path
):
    """Its presence has to mean the run finished. Retention is the last thing
    that can fail, so the manifest must come after it, not before."""
    from quorum.weekly.run import MissingRunOutputsTable

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("drop table run_outputs")

    with pytest.raises(MissingRunOutputsTable):
        run_weekly(_cfg(database, tmp_path, retain_runs=True), log=lambda *_: None)

    assert not (tmp_path / "last_run.json").exists()


def test_retention_off_writes_nothing(database, gong_calls, tmp_path):
    """RETAIN_RUNS defaults off, and off must mean off: no row anywhere."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    cfg = _cfg(database, tmp_path)
    assert cfg.retain_runs is False
    run_weekly(cfg, log=lambda *_: None)

    with psycopg.connect(database) as conn:
        count = conn.execute("select count(*) from run_outputs").fetchone()[0]
    assert count == 0


def test_retention_on_writes_three_rows(database, gong_calls, tmp_path):
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path, retain_runs=True), log=lambda *_: None)

    with psycopg.connect(database) as conn:
        rows = conn.execute(
            "select file_type, run_date, content from run_outputs order by file_type"
        ).fetchall()

    assert [r[0] for r in rows] == ["html", "json", "xlsx"]
    # Written from the run's week start, not from whenever the row landed.
    assert all(r[1].isoformat() == "2026-08-17" for r in rows)

    stored = {r[0]: bytes(r[2]) for r in rows}
    for file_type, path in [("xlsx", paths["xlsx"]), ("json", paths["json"]),
                             ("html", paths["html"])]:
        with open(path, "rb") as f:
            assert stored[file_type] == f.read()


def test_retention_rerun_appends(database, gong_calls, tmp_path):
    """A re-run is a second real run, not a replacement of the first."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    cfg = _cfg(database, tmp_path, retain_runs=True)
    run_weekly(cfg, log=lambda *_: None)
    run_weekly(cfg, log=lambda *_: None)

    with psycopg.connect(database) as conn:
        count = conn.execute("select count(*) from run_outputs").fetchone()[0]
    assert count == 6


def test_retention_partial_failure_leaves_no_rows(database, tmp_path):
    """One good file, one missing — the transaction must not keep the good one."""
    from quorum.weekly import retention

    good_xlsx = tmp_path / "run.xlsx"
    good_xlsx.write_bytes(b"fake-xlsx-bytes")
    good_json = tmp_path / "run.json"
    good_json.write_text("{}")
    missing_html = tmp_path / "missing.html"  # never written

    cfg = _cfg(database, tmp_path, retain_runs=True)
    with pytest.raises(FileNotFoundError):
        retention.store(
            cfg,
            "2026-08-17",
            {"xlsx": str(good_xlsx), "json": str(good_json), "html": str(missing_html)},
        )

    with psycopg.connect(database) as conn:
        count = conn.execute("select count(*) from run_outputs").fetchone()[0]
    assert count == 0


def test_retention_on_without_migration_stops_before_any_crm_call(
    database, gong_calls, tmp_path
):
    """RETAIN_RUNS on with 0005 unapplied must fail immediately — not on the
    last line of the run, after every Gong and CRM call."""
    from quorum.weekly.run import MissingRunOutputsTable

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("drop table run_outputs")

    logged = []
    with pytest.raises(MissingRunOutputsTable):
        run_weekly(_cfg(database, tmp_path, retain_runs=True), log=logged.append)

    # Stopped before step 1 (the attendee read) — nothing past the guards at
    # the top of the block ran, and no file was emitted.
    assert not any("attendee-rows" in line for line in logged)
    assert list(tmp_path.iterdir()) == []


def _headers(ws) -> list:
    return [h for h in next(ws.iter_rows(max_row=1, values_only=True)) if h]


def test_tabs_omit_a_crm_that_was_never_queried(database, gong_calls, tmp_path):
    """Four output surfaces, in the state the suite actually runs in.

    Nothing covered this before: the suite has always run with both CRMs off
    and only asserted tab 1, so a heading naming two vendors, two always-present
    columns, a hardcoded "hubspot/sfdc" provenance and a bare 0 contact count
    all stayed invisible. An unqueried provider must contribute no column, no
    count and no name anywhere a reader can see.
    """
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)
    wb = load_workbook(paths["xlsx"])

    # Tab 1 — no in-CRM column of any kind is offered, so "not checked" never
    # has to appear on this tab at all.
    headers = _headers(wb["1 - Met this week"])
    assert not {"In CRM?", "In HubSpot?", "In Salesforce?"} & set(headers)

    # Tab 2 — the count columns for both unqueried providers are gone. A 0
    # here would be indistinguishable from a company with genuinely no
    # contacts on file.
    assert _headers(wb["2 - Company coverage"]) == [
        "Company", "Company name", "Employees", "HQ", "Account type",
        "Meets profile?", "Met this wk",
    ]

    # No cell on any tab reports a check that never happened.
    for name in wb.sheetnames:
        for row in wb[name].iter_rows(values_only=True):
            for value in row:
                assert "not checked" != str(value).strip().lower()

    # The inputs dump carries the same distinction as null, not zero, so the
    # JSON cannot be re-read later as "we looked and found none".
    dump = json.loads(open(paths["json"]).read())
    for company in dump["coverage"]:
        assert company["hs_total"] is None
        assert company["sf_total"] is None
        assert company["sf_senior"] is None

    # The HTML view once had a heading naming both vendors. It is the file a
    # reader actually opens, and it must name neither CRM when neither ran.
    html = open(paths["html"]).read()
    assert "Met this week" in html
    assert "Not in HubSpot or Salesforce" not in html
    assert "hubspot" not in html.lower()


@pytest.mark.parametrize(
    "hs_on, sf_on, crm_columns, checked",
    [
        (True, True, ["In HubSpot?", "In Salesforce?"], None),
        (True, False, ["In CRM?"], "Checked against HubSpot."),
        (False, True, ["In CRM?"], "Checked against Salesforce."),
        (False, False, [], None),
    ],
)
def test_workbook_columns_follow_the_crms_configured(
    tmp_path, hs_on, sf_on, crm_columns, checked
):
    """Met this week says, per person, whether they are in the CRM.

    With both CRMs on, the pair "In HubSpot?" / "In Salesforce?" — "in HubSpot
    but not Salesforce" is the actionable answer, and one merged column would
    lose it. With one, a single "In CRM?", and the caption names which CRM. With
    none, no column: nobody can be in a CRM that was never asked.

    This tab absorbed the old Missing-from-CRM tab, which was a filtered copy of
    it. Its Source column held which CRMs were checked — the same value on every
    row — and that fact is still a caption line.

    A person with no CRM record gets "—" in the CRM columns, never "no": a "no"
    in Mobile in CRM? asserted a fact about a record that does not exist.

    A unit test rather than an end-to-end one: configuring a CRM leg there would
    make the run reach for the real API. The config is a stub, not Config(),
    which reads the environment.
    """
    from types import SimpleNamespace

    from quorum.weekly.workbook import NO_RECORD, build_workbook

    cfg = SimpleNamespace(
        hubspot=SimpleNamespace(configured=hs_on),
        salesforce=SimpleNamespace(configured=sf_on),
        recent_days=90,
        shortlist_size=3,
    )
    crm_on = hs_on or sf_on

    reconciled = [
        {
            # In the CRM — listed second.
            "attendee_name": "Sam Fox", "email": "sam@acme.com",
            "domain": "acme.com", "flag": "", "title": "VP Sales",
            "mobile_in_crm": False, "linkedin_in_crm": False,
            "in_hubspot": True if hs_on else None,
            "in_salesforce": True if sf_on else None,
        },
        {
            # Not in the CRM — listed first. False is a real answer from a CRM
            # that was called; None is what reconcile() writes for one that
            # was not.
            "attendee_name": "Dana Reyes", "email": "dana@acme.com",
            "domain": "acme.com", "flag": "", "title": "",
            "mobile_in_crm": False, "linkedin_in_crm": False,
            "in_hubspot": False if hs_on else None,
            "in_salesforce": False if sf_on else None,
        },
    ]
    coverage = [
        {
            "domain": "acme.com", "name": "Acme", "employees": 500, "hq": "US",
            "account_type": "", "meets": "yes", "met": 1, "is_target": True,
            "sf_total": 4 if sf_on else None,
            "sf_senior": 1 if sf_on else None,
            "hs_total": 7 if hs_on else None,
        }
    ]

    out = str(tmp_path / "wb.xlsx")
    build_workbook(
        cfg, reconciled, coverage, [], [], out,
        profile={"employee_count_min": 50, "employee_count_max": 500},
        geo_label="United States/Canada",
    )
    wb = load_workbook(out)
    # One list of people, not two: everyone met, in-CRM column and all.
    assert wb.sheetnames[0] == "1 - Met this week"

    ws1 = wb["1 - Met this week"]
    assert _headers(ws1) == (
        ["Name", "Email", "Company (domain)"] + crm_columns
        + (["Title (CRM)", "LinkedIn?", "Mobile in CRM?"] if crm_on else [])
        + ["Flag", "Source"]
    )

    ws3 = wb["2 - Company coverage"]
    assert _headers(ws3) == (
        ["Company", "Company name", "Employees", "HQ", "Account type",
         "Meets profile?", "Met this wk"]
        + (["SF contacts", "SF focus-senior"] if sf_on else [])
        + (["HubSpot contacts"] if hs_on else [])
    )

    all_rows = list(ws1.iter_rows(min_row=2, values_only=True))
    captions = [r[0] for r in all_rows if r[0] and not any(r[1:])]
    rows = [dict(zip(_headers(ws1), r)) for r in all_rows if any(r[1:])]
    assert (checked in captions) if checked else not any(
        str(c).startswith("Checked against") for c in captions
    )

    if not crm_on:
        # Nobody is missing from a CRM that was never asked: order unchanged.
        assert [r["Name"] for r in rows] == ["Sam Fox", "Dana Reyes"]
        return
    # Not in the CRM first.
    assert [r["Name"] for r in rows] == ["Dana Reyes", "Sam Fox"]
    dana, sam = rows
    for col in crm_columns:
        assert dana[col] == "NO" and sam[col] == "yes"
    # No record: a dash, never "no".
    assert (dana["Title (CRM)"], dana["LinkedIn?"], dana["Mobile in CRM?"]) == (NO_RECORD,) * 3
    # A record with no mobile on it: a real "no".
    assert sam["Mobile in CRM?"] == "no"
    # No vendor is named on a row: which CRM was checked is the caption or the
    # column headers.
    for r in rows:
        assert "hubspot" not in " ".join(str(v) for v in r.values()).lower()
        assert "salesforce" not in " ".join(str(v) for v in r.values()).lower()


def test_the_map_filter_keeps_a_company_it_could_not_assess():
    """The filter half. `is_target` is False for a company that failed the ICP
    test AND for one the test could not run on, so filtering on it alone drops
    the second kind off tab 3 — a company disappearing from the map because
    data nobody fetched did not clear a bar."""
    from quorum.weekly.stakeholders import companies_for_map

    coverage = [
        {"domain": "target.com", "assessed": True, "is_target": True},
        {"domain": "rejected.com", "assessed": True, "is_target": False},
        {"domain": "unassessed.com", "assessed": False, "is_target": False},
    ]

    kept = [c["domain"] for c in companies_for_map(coverage)]

    assert kept == ["target.com", "unassessed.com"]
    # A company the test rejected is a decision, and stays off the map.
    assert "rejected.com" not in kept


def test_no_crm_does_not_silently_empty_the_stakeholder_map(
    database, gong_calls, tmp_path
):
    """With no CRM, the ICP test cannot run — and must say so rather than
    reporting a verdict.

    Before this, empty firmographics made every company fail on "no size", and
    the same verdict is the filter feeding tab 3, so the map came out empty.
    Nothing errored and the workbook had its usual shape: an empty tab 3 reads
    as "nobody worth considering this week", which is a finding a reader would
    act on, rather than "the test never ran".
    """
    from quorum.weekly.coverage import NOT_ASSESSED
    from quorum.weekly.stakeholders import ICP_NOT_ASSESSED

    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)
    _seed_profile(database, account_id)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)
    wb = load_workbook(paths["xlsx"])

    # Tab 2 — the verdict column states that no verdict was reached. Not "no
    # size", which is a finding about the company.
    ws3 = wb["2 - Company coverage"]
    verdict = _headers(ws3).index("Meets profile?")
    companies = [r for r in ws3.iter_rows(min_row=2, values_only=True) if r[1] or r[6]]
    assert companies, "the company met this week must still appear on tab 2"
    for row in companies:
        assert row[verdict] == NOT_ASSESSED

    # Tab 3 — not empty. Every company that reached the map is on it, saying
    # why there are no people rather than being absent.
    stakeholder_rows = [
        r for r in wb["3 - Stakeholder list"].iter_rows(min_row=2, values_only=True)
        if r[1]
    ]
    assert stakeholder_rows, "tab 3 must not be empty when the ICP test could not run"
    assert {r[1] for r in stakeholder_rows} == {ICP_NOT_ASSESSED}
    assert {r[0] for r in stakeholder_rows} == {"acme.com"}
    # Not the "we looked and found nobody" row — nothing was looked at.
    assert all(r[1] != NO_SENIOR_CONTACT for r in stakeholder_rows)

    # Tab 1 — no header names a system this run never called, and a column
    # whose source was not configured is dropped rather than renamed: with no
    # CRM the title is empty on every row, so there is nothing to head.
    assert "Title (SF)" not in _headers(wb["1 - Met this week"])
    assert "Title (CRM)" not in _headers(wb["1 - Met this week"])

    # The dump carries the third state as its own field, so "could not assess"
    # cannot later be re-read as "assessed and rejected".
    dump = json.loads(open(paths["json"]).read())
    for company in dump["coverage"]:
        assert company["assessed"] is False
        assert company["is_target"] is False
        assert company["meets"] == NOT_ASSESSED
    # Nothing was queried, so no bench is claimed for it — not even an empty one.
    assert dump["sf_bench"] == []

    # The HTML must not colour a test that never ran as a rejection.
    html = open(paths["html"]).read()
    assert "not assessed" in html
    assert f'class="na">{NOT_ASSESSED}' in html
    assert f'class="rej">{NOT_ASSESSED}' not in html


def test_history_splits_on_a_changed_address(database, gong_calls):
    """The defect that justifies the identity tables, as a test rather than a
    claim: Dana attended under two addresses, so the email-keyed history query
    reports her as two people with two different last_met dates."""
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)

    cfg = Config(database_url=database, account=ACCOUNT, week_start="2026-08-17")
    with psycopg.connect(database) as conn:
        history = db.met_history(conn, cfg, ["acme.com"])

        assert history["dana.reyes@acme.com"]["last_met"].isoformat() == "2026-08-18"
        assert history["d.reyes@acme.com"]["last_met"].isoformat() == "2025-10-21"

        # person_identifiers already holds what would collapse them.
        rows = conn.execute(
            "select count(distinct person_id) from person_identifiers "
            "where email in ('dana.reyes@acme.com', 'd.reyes@acme.com')"
        ).fetchone()[0]
    # Two records today because the addresses were never linked; the read path
    # that fixes this is the one described in migrations/0002_identity.sql.
    assert rows == 2


def test_group_call_is_labelled_not_judged(database, gong_calls):
    account_id = _seed_account(database)
    _import(database, account_id, gong_calls)

    cfg = Config(database_url=database, account=ACCOUNT, week_start="2026-08-17")
    with psycopg.connect(database) as conn:
        history = db.met_history(conn, cfg, ["acme.com"])

    from quorum.weekly.stakeholders import recent_contact

    trainee = history["trainee1@acme.com"]
    assert trainee["smallest_meeting"] == 9      # above GROUP_CALL_MIN of 8
    # Recency is computed against today, so assert the label rather than the date.
    label = recent_contact(cfg, trainee, None)
    assert label.endswith("(group call)") or label.startswith("no —")


@pytest.mark.parametrize(
    "title, rank",
    [
        ("Chief Revenue Officer", 3),
        ("Vice President, Sales", 2),   # must not match on 'president'
        ("SVP Marketing", 2),
        ("Chief of Staff", 2),          # not C-level here
        ("Director of RevOps", 1),
        ("", 0),
    ],
)
def test_seniority_ordering(title, rank):
    from quorum.weekly.stakeholders import seniority_rank

    assert seniority_rank(title) == rank


def test_soql_quoting():
    from quorum.crm.salesforce import soql_quote

    assert soql_quote("o'brien@acme.com") == "o\\'brien@acme.com"
    assert soql_quote("a\\b") == "a\\\\b"


# --- HubSpot 429 handling ---------------------------------------------------- #
#
# The search endpoint is burst-limited, so a weekly run has to wait one out
# rather than die. No network: requests.post is replaced with a scripted
# sequence of responses, and the sleep is recorded instead of taken.


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture
def hubspot_calls(monkeypatch):
    """Script HubSpot's replies; return the recorded sleeps and request count."""
    from quorum.crm import hubspot as hs_mod

    state = {"sent": [], "slept": []}

    def install(responses):
        queue = list(responses)

        def fake_post(url, **kwargs):
            state["sent"].append(kwargs.get("json"))
            return queue.pop(0)

        monkeypatch.setattr(hs_mod.requests, "post", fake_post)
        monkeypatch.setattr(hs_mod.time, "sleep", lambda s: state["slept"].append(s))
        return state

    return install


def _client():
    """A client with a fake key, built without touching Config().

    Config() calls load_dotenv(), so constructing one here would pull the real
    HubSpot key out of .env and put it in any failure output.
    """
    from types import SimpleNamespace

    from quorum.config import HubSpotConfig
    from quorum.crm.hubspot import HubSpot

    return HubSpot(SimpleNamespace(hubspot=HubSpotConfig(api_key="test-key")))


def test_hubspot_retries_a_429_then_succeeds(hubspot_calls):
    hit = {"results": [{"id": "1", "properties": {"email": "a@acme.com"}}]}
    state = hubspot_calls([_Resp(429), _Resp(200, hit)])

    got = _client().contact_by_email("a@acme.com")

    assert got.email == "a@acme.com"             # the run continues
    assert len(state["sent"]) == 2               # it retried exactly once
    assert state["slept"] == [2.0]               # backed off before retrying


def test_hubspot_honours_retry_after(hubspot_calls):
    state = hubspot_calls(
        [_Resp(429, headers={"Retry-After": "7"}), _Resp(200, {"total": 4})]
    )

    assert _client().count_domain("acme.com") == 4
    assert state["slept"] == [7.0]               # HubSpot's number, not ours


def test_hubspot_caps_a_huge_retry_after(hubspot_calls):
    from quorum.crm.hubspot import MAX_BACKOFF

    state = hubspot_calls(
        [_Resp(429, headers={"Retry-After": "86400"}), _Resp(200, {"total": 0})]
    )

    _client().count_domain("acme.com")
    assert state["slept"] == [MAX_BACKOFF]        # bounded, so it cannot hang


def test_hubspot_gives_up_loudly_when_throttling_persists(hubspot_calls):
    from quorum.crm.hubspot import HubSpotRateLimited, MAX_ATTEMPTS

    state = hubspot_calls([_Resp(429)] * MAX_ATTEMPTS)

    with pytest.raises(HubSpotRateLimited):
        _client().contact_by_email("a@acme.com")

    assert len(state["sent"]) == MAX_ATTEMPTS     # capped, not unbounded
    assert len(state["slept"]) == MAX_ATTEMPTS - 1  # no sleep after the last try


def test_hubspot_does_not_retry_other_errors(hubspot_calls):
    state = hubspot_calls([_Resp(500)])

    with pytest.raises(requests.HTTPError):
        _client().contact_by_email("a@acme.com")

    assert len(state["sent"]) == 1                # 500 still fails immediately
