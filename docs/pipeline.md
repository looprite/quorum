# The pipeline

Six steps, and one optional pass (5b, enrichment) that runs only when a
provider is configured. Each one names the read path it serves — the column, tab or filter in
the stakeholder-map artifact that would be wrong or absent without it. A step
with no read path named is a step that should not exist.

This is the pipeline as it actually runs, written down rather than redesigned —
a description of working code, not of an intention.

The code lives in `quorum/` — one module per step, named for the step:

```
quorum/bootstrap.py         quorum init — the account row and the focus profile
quorum/config.py            every environment value, in one place
quorum/geography.py         regions, countries and the HQ comparison
quorum/db.py                the product DB and the queries against it
quorum/domains.py           internal / external / personal classification
quorum/gong/                client.py · importer.py · identity.py      (step 0)
quorum/crm/                 salesforce.py · hubspot.py                 (steps 3-5)
                            fieldmap.py — the resolved field map      (step 0b)
                            contact.py — what an adapter hands back
quorum/enrich/              one module per enrichment provider, found by
                            the package rather than imported by name  (5b)
quorum/weekly/              people.py (1-3) · coverage.py (4) ·
                            stakeholders.py (5) · enrichment.py (5b) ·
                            workbook.py + view.py (6) ·
                            run.py — the sequence, and nothing else
quorum/cli.py               quorum init · resolve-fields · import · weekly
```

Sources are named per step: **DB** = your own Postgres (schema in
`migrations/`), **SF** = Salesforce, **HS** = HubSpot, **Gong** = the call
source, via the importer that fills DB.

---

## Step 0a — Init (once per deployment)

**Reads:** the command line, and `ACCOUNT_DOMAIN`.
**Writes:** DB `accounts`, `user_focus_profiles`.

The migrations create empty tables. `quorum init` creates the two rows that
everything else needs: the account (its name and internal domains) and an
active focus profile (employee band, HQ geographies, seniority levels). Without
the account, `quorum import` stops. Without the profile, `quorum weekly`
refuses to start — see step 4.

```bash
quorum init --internal-domains acme.com,acme.io \
            --employee-min 200 --employee-max 10000 \
            --geographies "North America" \
            --seniority c-level,vp,director
```

Re-running it with the same arguments writes nothing. Re-running it with a
different profile is refused: the profile decides which companies appear on the
map, so changing it is `--replace`, which supersedes the active version and
keeps the old one, inactive. A domain added later needs no `--replace`.

A seniority level the CRM matching does not know is warned about at this point
rather than discovered in an artifact — it would match nobody. A geography the
ICP test cannot act on is *refused* here rather than warned about
(`quorum/geography.py`); it used to be a warning, and a profile naming a region
the test did not know applied no geography filter at all.

---

## Step 0b — Resolve the CRM field map (once, then when the org changes)

**Reads:** the CRM's own description of `Account` and `Contact`, and one row
count per candidate field.
**Writes:** DB `crm_field_maps`.

Standard fields are portable; custom fields are not, and custom fields are where
the signal lives. A field that does not exist raises `INVALID_FIELD` and kills
the whole query rather than blanking a column, so a hardcoded custom name is a
run that dies at the next customer — and it is per-customer differentiation
living in code. **The repository holds patterns, never names** (README, "CRM
field map").

`quorum init` resolves the map; `quorum resolve-fields` re-resolves it when an
admin adds a field or a package is installed, superseding the active version and
keeping the old one so last week's artifact stays explicable.

Five logical fields, each named for the column it serves: `employee_count`,
`hq_country`, `hq_city`, `hq_state` on `Account`, and `linkedin_url` on
`Contact`. For each, the resolver matches every field's API name and label
against an include pattern, drops the ones an exclude pattern or the field's
type rules out, counts how many rows actually have each survivor populated, and
stores them **in count order**.

**A list per logical field, not a winner.** The first populated value wins at
read time. On one real org, a handful of the companies met in a single week had
an empty package country and a populated `BillingCountry`; a single-name map
would blank their HQ and drop at least one of them out of the ICP set.

**Counting alone is not enough.** On the same org the best-populated field
matching `/linkedin/` on `Contact` is the *company's* page, beating the
person's profile URL. Exclusions are as much a part of a pattern as
inclusions, and every rejection is stored with the rule that made it, so the
choice can be argued with rather than trusted.

Unresolved **optional** fields degrade their column to "not available in this
CRM" — only `linkedin_url` has no standard equivalent. An unresolved
**required** field stops the resolution: without a headcount field the ICP
employee band cannot be applied, and the run would report every company as a
fit.

Salesforce unconfigured means no map and no error: that run reads no CRM fields
at all. Salesforce configured with no map is fatal — every query would silently
fall back to standard fields only.

---

## Step 0 — Import (prerequisite, not part of a run)

**Reads:** Gong API (per-account credentials from the environment).
**Writes:** DB `meetings`, `attendees`, and — through the dedupe step —
`people`, `person_identifiers`, `person_attendees`.

`quorum import` with no arguments imports the last `RECENT_DAYS` days —
the same window `Recent contact?` is answered against (step 5). It is derived
rather than fixed so the two cannot drift: importing less than the recency
window would put "no" against people who were met inside it, and a deployment
should not have to pick a number to get started. `--from … --to …` extends the
history beyond it; `quorum import --yesterday` is the overnight run.

The counts it prints carry the elapsed time, because the question they get
asked is whether to extend the range — calls-in-range and the time they took
are what a year's backfill can be extrapolated from.

**No transcripts are stored.** The importer reads Gong's call and party
endpoints only; `meetings` has no transcript column and nothing downstream reads
one.

**Read path served:** everything downstream. It is the only step that writes.

**Why it persists rather than paging Gong live:** the stakeholder list reads
all-time history, not the week. A year of one organisation's calls runs to a
couple of thousand meetings and several thousand external attendee rows —
re-paging that on every weekly run is the wrong shape, and it would also discard
the attendance-vs-invitation record between runs.

### Idempotency, and why it matters

A backfill and an overnight run **will** overlap. `migrations/0002` puts
`unique (person_id, attendee_id)` on `person_attendees`, which turns a silent
double-write into an error.

That error does not appear on the run that creates the problem. The first
import succeeds. The second one, over any range that overlaps it, raises — so a
backfill looks clean on Friday and the overnight job dies on Saturday, with
nothing in between to suggest why.

The port therefore guards every write, all in `quorum/gong/`:

| Write | Guard |
|---|---|
| `meetings` | `on conflict (account_id, provider, provider_id) do update` — re-importing refreshes title and timing rather than duplicating the call |
| `attendees` | a call whose meeting already has attendee rows is skipped whole |
| `people`, `person_identifiers` | `on conflict … do nothing` on the account+email keys |
| `person_attendees` | `on conflict (person_id, attendee_id) do nothing` |

`tests/test_import_and_weekly.py::test_reimport_is_idempotent` runs the same
range twice and asserts every table count is unchanged. It is there to fail if
any of these guards is ever removed.

---

## Step 1 — Pull the week's external attendees

**Reads:** DB `meetings` ⋈ `attendees` ⋈ `accounts`, filtered to one week and
to `domain_kind = 'external'`.
**Cost:** none.

**Read path served:** determines which rows exist on **tab 1 (Met this week)**
at all. Supplies `Name`, `Email`, `Company (domain)`, and the meeting titles.

Attendees with neither email nor domain are suppressed as non-contacts (meeting
bots) and listed by name at the foot of tab 1 — suppressed visibly, not dropped.

---

## Step 2 — Dedupe to distinct people, group by domain

**Reads:** step 1's rows. No I/O.
**Cost:** none.

**Read path served:** one row per person on tab 1 rather than one per meeting
attended; and the company keys that tabs 2 and 3 are built on.

Deduplication is on lowercased email. Rows with no email stay distinct — they
cannot be merged safely by name, and each is a gap worth reporting. Each gets a
follow-up flag: `needs enrichment` for a real inbox, `shared inbox — verify`
for a role address.

**Known limitation, and where it gets fixed:** email-keyed grouping splits a
person who has changed address. Demonstrated on a fixture 2026-08-24: one
person seen as `dana.reyes@` and `d.reyes@` becomes two rows with
`last_met` 2026-08-19 and 2025-10-21 — so the `Recent contact?` column reads
`no` for someone met last week. Resolving the key through `person_identifiers`
collapses them to one row with the correct date. That query is written and
verified; wiring it in is the read path that justifies the identity tables
(see `migrations/0002_identity.sql`).

---

## Step 3 — Reconcile each attendee against the CRM

**Reads:** HS contacts by email; SF `Contact` by email — the standard fields
(`Id, Name, Title, MobilePhone, Email, AccountId, Account.Name`) plus whatever
the field map resolved for `Contact.linkedin_url` (step 0b).

Both adapters hand back a `Contact` — `{name, title, email, mobile, linkedin,
last_activity}` — never a CRM record. Those API names appear in this document
and in `crm/`, and nowhere under `weekly/`: a standard field name is as much a
coupling as a custom one, it is just spelled the same in every Salesforce org.
`mobile` is presence only, reduced inside the adapter so the number cannot reach
the JSON dump. `linkedin` is three-valued — a URL, `""` for nothing on file, and
`None` for a CRM with no such field, which is what tab 1 and tab 3 render as
"not available in this CRM".
**Cost:** none — customer-owned data in both systems.

**Read path served:**
- tab 1 — whether each person is in the CRM, and the order of the rows:
  people not in the CRM come first. With both CRMs configured that is the pair
  `In HubSpot?` / `In Salesforce?`, because "in HubSpot but not Salesforce" is
  the answer they exist for; with one, a single `In CRM?`, and the tab's
  caption names the CRM ("Checked against Salesforce."); with none, no column —
  nobody can be missing from a CRM that was not asked.
- tab 1 — `Title (CRM)`, `LinkedIn?`, `Mobile in CRM?`, `Flag`. The first three
  report what a CRM record holds. With no CRM configured they are dropped
  rather than filled; for a person with no record they read `—`, never `no`,
  because there is no record to have or lack anything. `Flag`'s `needs title`
  appears only for a record that exists without one.

Tab 1 used to be two tabs — everyone met, and a second tab of the people not in
the CRM. The second was a filtered copy of the first, so they were merged.

Salesforce is the source of truth for `Title`; HubSpot is the fallback. With
both configured, a disagreement between them is itself a flag (`title differs`,
`title only in …`). With one, there is nothing to compare, so neither flag
appears.
Mobile is presence only — the number is never read into the artifact and is
redacted to a boolean in the JSON dump.

No CRM sync and no CRM tables. Contacts are fetched at read time and compared in
context, which is what keeps the comparison current and the schema small.

---

## Step 4 — Company coverage (triage)

**Reads:** DB `user_focus_profiles` (the active profile) and `crm_field_maps`
(the resolved one); SF contact counts per email domain, plus the senior-title
count and one `AccountId`; SF `Account` firmographics — the standard
`Name, Type` plus every field the map resolved for `employee_count`, `hq_city`,
`hq_state` and `hq_country`; HS contact count per domain.
**Cost:** none.

**Read path served:** every column of **tab 2 (Company coverage)** —
`Company` (the domain met), `Company name`, `Employees`, `HQ`, `Account type`,
`Meets profile?`,
`Met this wk`, `SF contacts`, `SF focus-senior`, `HubSpot contacts` — and the
ICP filter that decides which companies reach tab 3. The three count columns
appear only for a CRM that was configured: a count of contacts nobody counted
is a 0 that reads exactly like a company with none on file.

`Meets profile?` is the employee band and HQ geography from the focus profile,
and it has **three** answers rather than two. With no CRM configured the
firmographics it reads were never fetched, so it reports `not assessed — no CRM
configured` instead of a verdict, and the companies stay on tab 3 as
`— ICP not assessed: no CRM configured —` rather than dropping off it. Without
that third state an unconfigured CRM fails every company on "no size" — a
judgement about data nobody looked up — and because this column is also the
filter feeding tab 3, it empties the stakeholder map while the workbook keeps
its usual shape.

**A run without an active profile stops here — before step 1, in fact.** An
absent profile makes the ICP test pass everything, so the run would complete,
the workbook would have its usual shape, and every company met that week would
be reported as a target; a reader could not tell that from a correct run. The
profile is read and required at the top of `run_weekly` so the failure costs a
second rather than every CRM call above it. `quorum init` creates one.
`Account type` is captured and displayed but is **not** a filter. In one org
every value present on a week's companies was a customer or lifecycle label, so
gating on it removed nearly the whole ICP-fit set. `Type` is a per-org picklist
and commonly not maintained, so the gate stays in the code behind
`CUSTOMER_ACCOUNT_TYPES`, off by default. A run dumps the values it observed, so
you can see what yours holds before turning it on.

The firmographics query names no non-standard field: every one comes from the
resolved map (step 0b), so an org with no managed data package installed reads
`NumberOfEmployees` + `Billing*` without a line of code changing. That is
`quorum/crm/salesforce.py` contains no `__c` name, and a test asserts it.

`Meets profile?`'s geography half compares the HQ **country** as a whole value
against the profile's selections (`quorum/geography.py`). The test it replaced
substring-matched a country list against the joined "city, state, country"
display string, which is why that list carried `" us"` with a leading space — to
stop `us` matching inside `Australia`. Selections are `{level, value}` at region
or country level, three regions (North America, EMEA, APAC), and a bare string
reads as a region so profiles written before levels keep working. A region or
country the test does not know is refused at `quorum init` and again before a
run starts: it used to be accepted, apply no geography filter at all, and pass
every company met.

Two geography failures, kept apart on the row: `HQ not NA` is a country outside
the selection — a decision the profile made — and `HQ unknown` is no country in
the CRM at all. Both read `HQ not NA` before, and a reader could not tell which.

---

## Step 5 — Stakeholder list (the map)

**Reads:** DB all-time external meeting history for the target domains, with the
external-attendee count of each meeting; SF senior bench per domain
(`Name, Title, Email, MobilePhone, LastActivityDate` plus the resolved
`Contact.linkedin_url` candidates, title filtered by the focus profile's
seniority terms).
**Cost:** none.

**Read path served:** all six columns of **tab 3 (Stakeholder list)** —
`Company`, `Name`, `Title`, `Recent contact?`, `LinkedIn`, `Mobile in CRM?` —
and its caption, which states the rule in the reader's own values: which
companies, which seniority levels, how many per company, in what order.

Ordering is two rules, no weighting: most senior first, recent contact breaking
ties between equals. Capped at `SHORTLIST_SIZE` (3) per company — the cap is a
feature. A company with no senior CRM contact gets an explicit
`— no senior contact in Salesforce —` row rather than being omitted.

`Recent contact?` is one yes/no question. Contact is a meeting (from DB) or
anything logged in the CRM (`LastActivityDate`); recent is `RECENT_DAYS` (90).
A meeting with more than `GROUP_CALL_MIN` (8) external attendees is labelled a
group call on the row, so the reader discounts a training session rather than
the code doing it for them.

**No action is suggested per person.** The outreach sequence — connect, maybe
message, maybe meeting request — is undecided, so the artifact says who is worth
considering and stops.

**Not run here:** net-new discovery — finding senior people at a company who are
not in the CRM. The enrichment pass (5b) checks the people already on the map;
it does not find new ones.

**Not checked here:** whether each person is still at the company. Without an
enrichment provider the column is absent rather than saying `not checked` on
every row; with one, step 5b answers it.

---

## Step 5b — Enrichment (optional)

Runs only when an enrichment provider is configured; with none, nothing in this
section happens and the output is as the steps above describe. Full detail,
including the provider's name, its variable and its cost, in
`docs/enrichment.md`.

**Reads:** the provider — a company lookup per domain on tab 2, and a person
lookup per email on tab 3 and for the people not in the CRM on tab 1 (shared
inboxes excepted). For a tab 3
person the email finds nothing for, a second lookup by the CRM's LinkedIn URL,
where it holds one. One lookup per email, per LinkedIn URL and per domain per
run. Never a phone number or a personal email.
**Cost:** the provider's credits, per `docs/enrichment.md`.

**Read path served:** tab 1 `Name (…)`, `Title (…)` and `LinkedIn (…)` for
people not in the CRM; tab 2 `Employees (…)`, `HQ (…)` and `Profile check`;
tab 3 `Still at company?`, `Title (…)` and `LinkedIn (…)`; **tab 4 (Review
queue)**; and which companies reach tab 3 — a
company whose ICP verdict the provider disputes goes on, marked
`(profile disputed)`, because a wrong "no" otherwise removes a company from the
map without trace. The `(…)` is the provider's display name.

**Order within the run.** The provider's free account check runs first of all,
before the database or a CRM is touched. The company half runs after step 4 and
before the map's companies are chosen, since a dispute changes that choice. The
person half runs after step 5.

Three rules shape the output. **A provider value is never written over a CRM
value** — it is shown beside it, and a disagreement becomes a review-queue row.
**A result is accepted only if it is the person or company asked about** — the
searched email on the record (and, on the stakeholder list, the CRM
contact's name on it), the searched domain on the company, the searched
LinkedIn handle on the profile *and* the CRM contact's name on it — because a
lookup can return someone else at full confidence, and a CRM's LinkedIn URL can
point at someone else. **A miss is stated**, as
`not found in <provider>`: never inferred, never blank, never filled from
another source.

---

## Step 6 — Emit

**Writes:** five files, and nothing anywhere else — a local `.xlsx` (a Summary
tab and three numbered tabs; a fourth, the review queue, with an enrichment
provider), `weekly_view_<week>.html` rendered from it, `summary_<week>.json`
with the Summary tab's counts, `last_run.json` naming all of them, and a JSON
dump of every input — focus
profile, seniority terms, observed `Account.Type` values, coverage, meeting
history, the SF bench, the shortlist, the `Contact` describe, and with a
provider its values on each coverage and shortlist row, its name, and the
review queue, plus the summary.

**The summary** counts rows the other tabs already carry, each with its
denominator: companies met and how many fit the profile; of those, how many had
someone at the profile's seniority contacted in the last `RECENT_DAYS`; people
met, how many are not in the CRM and — with a provider — how many it found;
CRM records among people met, and people on the stakeholder list, lacking a
title, LinkedIn or mobile; and review-queue rows by kind, zeros included. A
count whose source was not configured is absent, not zero. The one count not
read off a tab is recent senior contact: it asks of the whole CRM bench, not
the capped list, so it does not move with `SHORTLIST_SIZE`. Under the counts,
one line says which enrichment provider ran, or that none was configured; the
summary file carries the same as `enrichment_provider`, with `recent_days`
beside it.

**Read path served:** the artifact itself; the summary is what a reader sees
first and what a delivery step posts; and the JSON is what lets the ranking be
re-tuned without re-running Salesforce, and — because retention keeps it — what
makes the counts comparable week to week.

Nothing is written back to any system. `MobilePhone` is reduced to a boolean in
the dump — sensitive contact fields pass through to the CRM, never into a
Quorum store.

---

## Running it

```bash
pip install -e .
cp .env.example .env          # then fill it in
psql "$DATABASE_URL" -f migrations/0001_core.sql  # …and 0002, 0003, 0004

quorum init --internal-domains acme.com --employee-min 200 \
            --employee-max 10000 --geographies "North America" \
            --seniority c-level,vp,director        # once

quorum import                                    # the last RECENT_DAYS days
quorum import --from 2025-10-01 --to 2026-08-24   # a longer history
quorum import --yesterday                          # the overnight run
WEEK_START=2026-08-17 quorum weekly                # the artifact
```

`quorum weekly` writes into `OUTPUT_DIR`: the workbook, the JSON dump of every
input, the summary's counts, the single-page HTML view, and `last_run.json` —
the manifest naming those four and the week they belong to, so a delivery or archival step can find
them without rebuilding the filename pattern or parsing the run's log. It writes
nothing anywhere else.

**What the tests cover, and what they cannot.** `pytest` runs the importer and
the whole weekly sequence against a real Postgres with the real migrations and
a stubbed Gong, with **Salesforce and HubSpot deliberately unconfigured** —
which is how the unconfigured-provider path stays honest rather than
degrading into "NO" or into a count of 0. The one-CRM configurations
(Salesforce only, HubSpot only) and the both-configured one run separately,
from reconciliation to the HTML view, against stubbed CRM adapters: they assert
that nothing comparing two CRMs appears unless both are configured. Enrichment
is covered the same way, off and on, against a stubbed provider — including a
lookup that returns someone other than the person asked about. The CRM
legs themselves are typically unreachable from an agent session and have to be
verified on a machine that can reach them, by diffing a workbook against a
known-good run for the same week. Point `QUORUM_TEST_DSN` at a Postgres a test
may create databases on; without it the database tests skip rather than fail.

## Where it runs

Inside your own environment. Salesforce and HubSpot are typically unreachable
from an agent session — an egress proxy refuses CONNECT, and no token changes
that. Your database is reachable from wherever the pipeline runs, which is the
same environment.

Testing by hand uses a pasted Salesforce token that expires in ~2 hours
(`docs/salesforce-access.md` runbook). A deployed run uses the client-credentials
flow (`SF_TOKEN_URL` / `SF_CLIENT_ID` / `SF_CLIENT_SECRET`) — no paste, no
refresh ritual.

## What the pipeline does not do

No MCP tools. No web UI. No briefs, no deliverables, no Linear publishing, no
per-contact scoring, no transcript moments, no outbound sequencing. The read
surface is one file. Tools are earned by a demonstrated interactive read, and
none has been demonstrated yet.
