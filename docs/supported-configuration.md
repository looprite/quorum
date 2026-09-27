# What this supports today

Read this before deploying. It states what has been built and tested, not what
the design allows. Where something is not supported, the entry says what it
would take — so you can judge the work rather than discover it.

Checked against the code on 2026-09-04, after the field-map, geography and
ICP-test work, and again on 2026-09-21 when the enrichment provider was added.

| | Supported | Notes |
|---|---|---|
| **Meeting source** | Gong | Swappable — one importer, see below |
| **CRM** | Salesforce, any org | Field names are resolved per org, not hardcoded |
| **Marketing contacts** | HubSpot | Optional; absent means no HubSpot column at all — never a "no", never a 0 |
| **ICP: company size** | Employee band | |
| **ICP: geography** | Region or country | North America, EMEA, APAC — no state or city level |
| **ICP: account type** | Optional substring gate | Off by default |
| **Seniority** | Title matching | C-level, VP, director, founder, manager |
| **Database** | PostgreSQL 13+ | `gen_random_uuid()` is built in from 13 |
| **Delivery** | Local files | A workbook, a JSON dump, a summary JSON and an HTML page |
| **Scheduling** | None | The repo defines no packaging or scheduling |
| **Enrichment providers** | One, optional | Off unless its key is set, and then the output is unchanged apart from the provider's columns and tab 4. Never replaces a CRM value — `docs/enrichment.md` |

---

## Salesforce: no managed package required

The pipeline holds no Salesforce custom field name. At setup, `quorum init`
describes the `Account` and `Contact` objects, matches every field's name and
label against include and exclude patterns, discards those whose type rules them
out, counts how many records have each survivor populated, and stores the
candidates in population order. Every query is then built from that map.
`quorum resolve-fields` re-runs it later and keeps the superseded version.

Five things are resolved: employee count, HQ country, HQ city, HQ state, and the
person's LinkedIn URL. Each records which column it feeds and why each rejected
candidate was rejected.

It keeps a list per logical field rather than a winner, and weighs exclusions
as heavily as inclusions — both forced by real data, and both explained in
`docs/pipeline.md`, step 0b.

If nothing resolves for a logical field, its column reads **"not available in
this CRM"** and the run continues. Gaps are output, not failure.

**Salesforce is still effectively required**, though. The stakeholder list is
built entirely from Salesforce contacts, so without it you get the meeting
reconciliation and the company coverage — with the ICP verdict on that coverage
reading `not assessed — no CRM configured`, because the firmographics it judges
were never fetched — and a stakeholder tab that states that rather than listing
people.

---

## ICP geography

A focus profile selects geographies at two levels: `region` or `country`. Three
regions ship — North America (3 countries), EMEA (83) and APAC (25) — each with
common aliases, so `United States`, `USA` and `US` all resolve to the same place.
A bare string is read as a region name, so `["North America"]` still works.

A company's HQ country is compared as a whole value, not as a substring of a
joined address, so `US` cannot match inside `Australia`.

**Unknown is refused, not ignored.** A profile naming a region or country this
module does not recognise is rejected at `quorum init` and again before a run
starts. Previously it silently applied no geography filter at all and reported
success.

**No state or city level.** Adding one is a matching rule rather than new data —
city and state are already fetched — but it needs name normalisation first:
`CA` versus `California`, Washington the state versus Washington the city,
Georgia the state versus Georgia the country. Those collisions are the common
case at that granularity and the failure is silent, so it waits for a real ICP
that needs it.

---

## What swaps cheaply, and what does not

**The meeting source is genuinely swappable.** Everything downstream reads the
product database, not Gong. `quorum/gong/` is the only Gong-shaped code and all
it does is fill `meetings` and `attendees`. Supporting Fathom, Fireflies, Chorus
or anything else means writing one importer that fills those two tables.

**The CRM is not.** Salesforce is read mid-pipeline — attendee reconciliation,
company firmographics, and the senior contact bench — across three modules. The
field map makes it portable across Salesforce *orgs*; it does not make another
CRM work. A customer whose primary CRM is HubSpot needs an interface that has
not been written.

---

## Behaviour worth knowing before you see the output

**Companies with no employee count are excluded**, failing the ICP test with
"no size" rather than passing on the benefit of the doubt. Deliberate — a map
that quietly includes companies it could not assess is worse than one that says
what it skipped — but it means CRM data quality limits how many companies reach
the map.

**A missing HQ country now reads "HQ unknown"**, distinct from being outside
your selected regions. One is missing data, the other is a decision.

**"Recent contact?" has two sources.** A meeting from the imported history, or
activity logged in the CRM (`LastActivityDate`, a rollup of Tasks and Events).
The CRM rollup cannot say what the touch was, so a bulk email and an hour-long
call look identical. Meeting history is what distinguishes them, and what
supplies the group-call label that stops a training webinar reading as a
relationship.

**Import at least `RECENT_DAYS` of history.** That setting decides whether the
contact column says yes or no, so importing less produces rows saying "no" for
people who were met. `quorum import` with no arguments does exactly that window.

**Nobody is checked for still being at the company** unless an enrichment
provider is configured. CRM contacts go stale as people move, and this list
suggests people to approach. Without a provider the column is absent rather than
saying "not checked" on every row; with one, `Still at company?` gives the
provider's view, which can be out of date too.

**No outreach action is suggested per person.** Real outreach is a sequence —
connect, perhaps message, perhaps request a meeting — and that sequence is not
decided here. The output says who is worth considering and stops.

---

## What it costs to run

Nothing, unless you configure the optional enrichment provider. Without it,
every field in the output comes from Gong, Salesforce or HubSpot — data you
already own, and no provider is called. With it, each run spends that
provider's credits on the lookups `docs/enrichment.md` lists, with an estimate
per week. Either way, mobile numbers are reported as present or absent in the
CRM, never revealed or stored, and never requested from the provider.
