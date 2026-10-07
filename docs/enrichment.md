# Enrichment — a second opinion beside your CRM

Optional. With no provider configured, a run and its output are exactly what
`setup.md` describes: a summary and three tabs, nothing called, nothing spent. This file
covers what changes when one is configured.

**The one provider implemented is LeadIQ**, through its GraphQL API. Nothing
else in the repository names it — the pipeline asks for "the configured
provider" — so a second provider is one new module in `quorum/enrich/`.

---

## What it is for

The map reports what your CRM holds, errors included: a company recorded at 10
employees that has 150, a senior contact who left two years ago, a LinkedIn URL
attached to the wrong person. Enrichment looks each of those up with the
provider and shows what it says **beside** the CRM's value, so the
disagreements are visible and a person can settle them.

**Three sources, and none of them is the truth.**

- **Your CRM** is what you have.
- **The provider** is a second opinion. It can be out of date too.
- **LinkedIn** is what a person checks by hand, and the one that settles it.

So a provider value **never replaces** a CRM value. Quorum still writes nothing
to your CRM. The output is a list of disagreements — tab 4, the review queue —
for someone to work through and apply in the CRM themselves. That someone does
not need CRM access to do the checking, which is deliberate.

**Agreement is not confirmation.** If the provider is one of the sources your
CRM was filled from, the two agreeing tells you little. The review queue holds
disagreements and missing values; it does not certify everything else.

---

## Turning it on

Set one environment variable, in `.env` locally or your secret store in
production:

```
LEADIQ_API_KEY=your-api-key
```

That is the whole switch. Unset or empty, enrichment is off. Configure one
provider at a time: with two, the run refuses to start rather than choosing
between them, because a person one provider cannot find is reported as not
found in *it* — never filled from another.

**Use the plain secret key, not the Base64 one.** LeadIQ's settings page offers
both. The client sends the key as HTTP Basic auth — the key as the username, an
empty password — and does the Base64 encoding itself. Given the Base64 form, it
encodes it a second time, and every call returns 401.

**The first thing a run does with it** is one free account query, before the
database is read or a CRM is called. A key that does not work fails there, not
after everything else has been paid for. The log line names the plan and the
credits available:

```
[i] Enrichment: LeadIQ — <plan name> (Active), <n> credits available
```

**Set it where the scheduled job runs, then check a scheduled run.** The key
has to be in the environment of whatever runs `quorum weekly` on a schedule —
on a PaaS, the app's own environment variables — not only in a shell someone
tried it from. A trial with the key exported by hand proves the key works; it
says nothing about whether the schedule has it. With no key, the run does not
fail: it produces a normal-looking artifact with no provider columns. So read
the first scheduled run for one of three things — the log line above, the
Summary tab's footnote naming the provider, or `enrichment_provider` in
`summary_<week>.json`. If it says "not configured", the key is not reaching the
job.

---

## What it looks up

| Where | Looked up by | Compared |
|---|---|---|
| Tab 2 — Company coverage | domain | employee count and HQ country, through your ICP test |
| Tab 3 — Stakeholder list | email; then the CRM's LinkedIn URL, if the email finds nothing | current employer, title, LinkedIn URL |
| Tab 1 — Met this week, people not in the CRM | email | name, title and LinkedIn URL |

Each person and each company is looked up once per run, whichever tab asks
first. Shared inboxes (`support@`, `info@` …) are never looked up — a role
inbox is not a person, and a credit spent on it buys an answer that cannot be
right.

**Only the fields the map reads are requested.** A person lookup asks for name,
current and past positions (title, employer, work emails), LinkedIn URL and when
the record was last updated. **It never asks for a phone number or a personal
email.** The map does not use them, and a phone number is the expensive field.

### A result is accepted only if it is the person asked about

A lookup by email can return a record for **someone else** — a different person
at a different company, at the provider's highest confidence score, with none of
the record's emails matching the one searched for. This was seen on a real call.
Taken at face value it would report a stakeholder as having moved to a company
they never worked at.

So a person is accepted only when the email searched for is on the record, in a
current or past position. A past position counts: the address in your CRM is
often the one they left behind, and the record's current employer is then the
finding. Companies are held to the same rule on domain.

### When the email finds nothing, the CRM's LinkedIn URL is tried

An email address goes stale exactly when someone changes jobs — the event this
check exists to catch — and the provider does not always keep the old address
on its record. A LinkedIn profile URL usually survives the move. So when the
email lookup finds nothing and the CRM holds a LinkedIn URL for the person, the
run searches by that URL. The result is used only if **both**:

- the record's profile handle (the part after `/in/`) is the one searched, and
- its first and last name agree with the CRM contact's, ignoring case, accents,
  punctuation and middle names.

The second check is there because a CRM's LinkedIn URL can point at someone
else. A handle match under a different name is not used; it becomes a review
queue row instead — **CRM LinkedIn may be someone else** — for a person to
open. Rows found this way say so: `… (matched on LinkedIn)`.

The email lookup always runs first and its rule does not change. Where the email
is accepted, a LinkedIn search would return the same record, so it is not made.

**Why the fallback is there at all**: on the week it was built against, most
of the stakeholders the email missed had a LinkedIn URL in the CRM, and most of
those matched on handle and name. Whether that holds for your CRM depends on
how well the LinkedIn field is filled, so the run log states each week how many
stakeholders were matched on email and how many on LinkedIn — measure yours
rather than borrowing these. Anything still unmatched reads
**"not found in LeadIQ"** — never inferred, never filled from elsewhere.

### A person the provider withholds

LeadIQ can hold a record it will not return — a person whose details are
withheld for privacy compliance. The lookup then answers with an error rather
than a result. That person reads **"not found in LeadIQ"** like any other miss,
the run carries on, and the log line that counts lookups adds how many were
withheld, with LeadIQ's code for why where it gives one.

It is recognised from the shape of the error, not its wording: an error the
API raises while answering a person search names that search in its `path`, as
the GraphQL specification requires. Every other error still stops the run — an
error with no path, one outside a person search, one on a company lookup or the
account check, and any HTTP failure. So does a run in which **every** person
lookup came back withheld: that is the provider not answering, and reporting
everyone as not found would hide it.

---

## What changes in the output

**Tab 1 — Met this week** gains `Name (LeadIQ)`, `Title (LeadIQ)` and
`LinkedIn (LeadIQ)`, filled for the people not in your CRM: who the provider
says each of them is, and the profile to connect with. `not found in LeadIQ`
where it has no record; `not looked up — shared inbox` for a role address.
People already in your CRM are not looked up here — the stakeholder list is
where the provider is set beside a CRM record.

**Tab 2 — Company coverage** gains `Employees (LeadIQ)`, `HQ (LeadIQ)` and
`Profile check`. The comparison is the **verdict**, not the number: your ICP
test runs on each source's own values, and only a different answer matters.
250 against 275 employees is inside a 50–500 band either way; 480 against 520
flips it.

| Profile check | Means |
|---|---|
| `agrees` | Both sources give the same ICP answer |
| `disputed — LeadIQ says yes` / `… says no (<reason>)` | They give different answers — see below |
| `not found in LeadIQ` | The provider has no record for the domain |
| `CRM not assessed — LeadIQ says …` | No CRM is configured, so there is no CRM verdict to compare |

**A disputed company goes onto the stakeholder list, marked.** A wrong "yes" is
visible — the company appears on the map and someone looks at it. A wrong "no"
is not: the company never reaches the map and nobody knows to look. So a
company your CRM rejects and the provider would accept is put on tab 3 as
`<company> (profile disputed)` for a person to settle. An existing customer
(`CUSTOMER_ACCOUNT_TYPES`) stays off either way.

**Tab 3 — Stakeholder list** gains three columns:

| Column | Values |
|---|---|
| `Still at company?` | `yes` · `no — now at <company>, as <title>` (just `no — now at <company>` when the provider has no title) · `unclear — no current employer in LeadIQ` · `not found in LeadIQ`, with `(matched on LinkedIn)` added where the match came from the CRM's LinkedIn URL |
| `Title (LeadIQ)` | Filled **only where it differs** from the CRM's title — and left blank for someone who has moved, whose provider title is for a different job |
| `LinkedIn (LeadIQ)` | Filled **only where it differs** from the CRM's URL |

**`yes` means the company is among the person's current positions**, not that
it is listed first. Someone with a full-time role plus advisory seats is still
at each of them; `no — now at …` means the company is in none of their current
positions. The provider title shown is the one for this company.

A detected move flags the row; it never removes it. The CRM record is still
what you have, and a name vanishing without explanation is worse than a name
marked stale.

**Tab 4 — Review queue** exists only with a provider configured. One row per
thing a person should settle, most consequential first, each saying what to
check and where:

**Its `Company` column is the CRM account's name**, falling back to the domain
only where no account matched — unlike Company coverage, whose `Company` column
is the domain met. A script filtering this tab by domain will report a row
missing that is present under the account's name. Match on both, or read the
domain from Company coverage.

A person's row carries the CRM's LinkedIn URL in its own `LinkedIn` column, so
the profile is one click from the row; **LinkedIn differs** keeps both URLs in
its CRM and provider columns, and company rows leave it blank. The workbook tab
is one table in the order below. The HTML page shows the same rows under one
sub-heading per kind, with its count.

| What | When |
|---|---|
| Profile fit disputed | The two sources give different ICP answers |
| Company not found in `<provider>` | Your CRM has both the employee count and the HQ, and the provider has no record of the company, so those numbers get no second opinion |
| Headcount or HQ missing | Either source lacks the employee count or HQ the ICP test needs |
| May have left | The provider places the person at a different company |
| CRM LinkedIn may be someone else | The CRM's LinkedIn URL leads to a profile under a different name |
| Account may be linked to the wrong company | The CRM account's name does not resemble the domain it was reached through |
| Title differs | The provider's title differs from the CRM's |
| LinkedIn differs | The provider's LinkedIn URL differs from the CRM's |

**Account linking** is the one no lookup fixes. With Salesforce, the map
reaches a CRM account through the company's contacts: the account a contact at
that email domain is filed under (the first one found; the order is not
defined). One contact filed under the wrong account can pull that account's
details in for the whole company, and contacts split across two accounts can
land on either. HubSpot is not asked for an account at all: it contributes a
count of contacts at the domain and nothing about the company. Only someone
correcting the contact's or the account's record can fix this; the queue says
where to look.

**The Summary tab** gains two things: how many of the people not in your CRM
the provider found, out of those it looked up (shared inboxes are not), and
one row per review-queue kind with its count — zeros included, so a kind
dropping to zero week on week is visible. Its footnote names the provider;
with none configured, it says that instead, which is the one place a run that
was meant to be enriched and was not can be told from one that never was.

The JSON dump carries the same: each company and stakeholder row holds the
provider's values, `enrichment_provider` names the provider (null when none was
asked), and `review_queue` is the tab as data. `summary_<week>.json` carries
`enrichment_provider` too, for a delivery step.

---

## What it costs

Credits, on LeadIQ's rate card at the time of writing: **1 per person record,
3 per company record, 10 per mobile phone.** Check your own plan — the `account`
query the run makes at the start reports it.

Measured on one account, September 2026, reading the account's credit balance
before and after:

| Call | Credits used |
|---|---|
| Person lookup that also selected a phone number (an ad-hoc query, not this module's) | 11 — consistent with 1 for the record and 10 for the phone |
| Person lookup as this module makes it, matched | 0 |
| Person lookup with no match | 0 |
| Company lookup by domain (five domains) | 0 |

So in practice lookups have cost less than the rate card says. Why is unknown —
records the account has already unlocked may be free — so do not rely on it.
This module never selects a phone number, so a person lookup costs at most the
record.

**A week's spend at rate-card prices — work it out from your own counts.**
The run makes one company lookup per company met; one person lookup per person
on the stakeholder list and per person met who is not in your CRM, minus shared
inboxes, which are never looked up; and a LinkedIn lookup only for the people
the email missed. Each of those is one record. So:

```
credits ≈ (people looked up × 1) + (companies met × 3)
```

For a week with 30 companies met and 50 people looked up — round numbers to
show the arithmetic, not a measurement — that is about 50 credits for person
records plus about 90 for company records: **under 150 for the week**, and
possibly far less, since the lookups measured above cost nothing at all. An
estimate, not a measurement. The Summary tab carries the counts your own
arithmetic needs.

**Measuring it yourself: the balance may be shared.** The `account` query
reports the plan's balance, and on a plan shared by a whole company everyone
else's usage moves it too. A before-and-after comparison over a full run —
minutes, not seconds — can include other people's spending; on one real trial
it did not line up with the run's own lookups. Compare over a short window, or
ask the provider for per-key usage.

---

## What it does not do

- **Write anything.** Not to your CRM, not to the provider. The review queue is
  for a person.
- **Discover people you have not met.** It checks the people already on the map.
  Finding new senior contacts at a company is a different job and is not built.
- **Look up phone numbers.** Never requested.
- **Decide.** Where the sources disagree, the map shows both and the queue asks
  a person. LinkedIn settles it.
