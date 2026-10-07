# Reading your first run

`setup.md` §12 lists what the tabs contain. This file is about what to make
of them — what to check first, what looks wrong and isn't, and what to actually
do about each kind of gap.

**It is written for whoever receives the file, not for whoever deployed it.**
Those are often different people. If you set the deployment up, this is the
thing to send along with the first run.

## Before anything else: three things that tell you the run worked

A run can complete, produce every tab, and still have done almost nothing —
because the legs it needs were not configured, not because anything failed.
That is deliberate: gaps are output, not errors. So the first pass is checking
which legs actually ran.

1. **Is tab 1 populated, and are the people on it external?** Colleagues should
   not appear. If they do, your account's internal domains are wrong. If the tab
   is empty, the week window found no meetings — check the window before
   concluding anything about the data.
2. **Does tab 2 have employee counts and HQs?** If those columns are populated,
   your CRM leg ran. If `Meets profile?` reads `not assessed — no CRM
   configured` on every row, it did not, and nothing on tab 3 means what it
   looks like it means.
3. **Does tab 3 have people on it?** If it carries a single explanatory row
   instead, no company passed the filter. That is a real answer sometimes — but
   check point 2 first, because an unassessed company cannot pass a filter it
   was never tested against.

**The Summary tab, first in the file, is the same check in numbers.** A line
missing from it is a source that was not configured — there is no "not in your
CRM" count without a CRM — which is different from a line reading 0. Every
count is out of something; read the two together. Each is a count of rows on
the tab it names, so a number that looks wrong can be checked there. Under
the counts, the tab says in words whether an enrichment provider ran: an
absent provider adds no columns, so this line is the only place a run that
should have been enriched, and was not, shows up.

**An absent column, a `—`, and a `no` mean three different things**, and the
difference is the same one everywhere in this output:

- **A column that is not there at all** is a source your deployment did not
  configure. The run reports nothing about a system it never asked, rather than
  reporting a 0 or a blank that reads like an answer. This is a fact about your
  configuration, not about your data.
- **`—`, or `not assessed — no CRM configured`,** means the question was asked
  and there was nothing to read: no CRM record for that person, no
  firmographics to test that company against.
- **`no`** means the run looked and the answer was no. Only this one is a
  finding. An empty `LinkedIn (CRM)` cell says the same thing more quietly: your CRM
  has the field, and this person has nothing in it.

So a column you expected and cannot find is a configuration problem, and a
column full of `no` is something to act on.

## What looks alarming and is not

**A lot of people missing from the CRM.** This is the most common first
reaction, and it is the product working. The gap between who your team meets
and who reaches the CRM is the thing this exists to measure. A first run over
real history usually surfaces more than people expect. That is a finding about
your process, not a fault in the run.

**Titles that are missing, wrong or years out of date.** The output reports
what the CRM holds. Where it is stale, the map shows it as stale rather than
quietly improving it.

**Companies you do not recognise.** Recruiters, vendors, candidates and
partners all end up in a meeting recorder. The run does not know which meetings
were commercial.

**Tab 3 much shorter than tab 2.** Expected. Tab 2 is every company met; tab 3
is the ones that fit your profile, capped at `SHORTLIST_SIZE` people each, and
only people already in your CRM. The caption at the top of tab 3 states the
rule in your profile's own terms.

**Dashes in the CRM columns on tab 1.** A person with no CRM record has no
title, LinkedIn or mobile *in the CRM* to report, so those cells read `—`
rather than `no`. They are listed first on tab 1 — that is the not-in-CRM list.

**`— no senior contact in Salesforce —` rows.** A company that fits your ICP
where the CRM holds nobody senior. That is a stated gap rather than an omission,
and it is usually the most useful line in the file.

**Meeting bots and notetakers at the foot of tab 1.** Attendees with neither an
email nor a domain are suppressed there visibly rather than dropped, so you can
see what was removed.

## What actually indicates something is wrong

- **Every person on tab 1 showing as not in the CRM.** Your CRM leg is not
  reaching your data. If no CRM were configured the in-CRM column would not be
  there at all, so `NO` on every row means one was configured and is
  answering with nothing. The same goes for every person who *is* in the CRM
  reading `no` under `Mobile in CRM?` and blank under `LinkedIn (CRM)`.
- **Companies you know are large showing no employee count.** A company with no
  count is excluded from the map entirely rather than given the benefit of the
  doubt, so this silently shrinks tab 3.
- **`HQ unknown` where you know the HQ.** `HQ unknown` and `HQ not in region`
  are different rows: the first is missing data, the second is a decision your
  profile made.
- **People appearing twice under different addresses.** Someone who changed
  email address can split into two people, which also makes a recent contact
  read as "no".
- **Another company's people listed under an account.** With Salesforce, the map
  reaches a CRM account through the company's contacts: the account a contact
  at that email domain is filed under. One contact filed under the wrong
  account can pull that account's details in for the whole company, and
  contacts split across two accounts can land on either. (HubSpot gives no
  account, only a contact count.) Nothing in the run can fix that; someone
  correcting the contact's or the account's record in the CRM can.

## What to do with each kind of gap

**Nothing in this list happens automatically.** Quorum reads your CRM and
writes nothing back to it — not to Salesforce, not to HubSpot, not to Gong. The
output is a list of things for a person to decide about, and every action below
happens in your own systems.

| What you see | What it means | What a person does |
|---|---|---|
| Met, not in CRM (top of tab 1) | Somebody your team spoke to has no record | Decide whether they belong there, then add them. This is the most directly actionable thing in the file. |
| Flag `marked as left in CRM` (tab 1) | You met them this week, but your CRM has them ticked as no longer at the company. Only with a CRM box for it. | One of the two is wrong: check, and correct the CRM if they have not left |
| In CRM, no title | The record exists but is thin, and without a title the person can never reach the stakeholder list | Fill it in at the source, so next week's run reads it. With a provider configured, a `Title missing in CRM` row on tab 4 gives you a title to start from. |
| Company met, no employee count | Excluded from the map entirely | Fill the firmographics if the company matters to you |
| Fits profile, no senior contact | The stakeholder gap | Find who the senior people there are. This is where a data provider or a browser extension earns its place, and it is a person's step, not a pipeline's. |
| Fits profile, senior contacts present | The map did its job | Decide who is worth approaching |
| A row on tab 4, the review queue | Your CRM and the enrichment provider disagree, or one is missing a value | Check the place the row names — usually LinkedIn — and correct whichever source is wrong. Only with a provider configured. |

**No action is suggested per person, deliberately.** The output says who is
worth considering and stops; the outreach sequence is not decided for you.

**Nobody is checked for still being at the company** unless an enrichment
provider is configured — CRM contacts go stale, and on its own the run cannot
tell.

## If an enrichment provider is configured

The provider's values sit **beside** the CRM's on tabs 1, 2 and 3, and a fourth
tab lists where they disagree. `docs/enrichment.md` has the detail; what a
reader needs:

- **Neither source is the truth.** The provider can be out of date as easily as
  the CRM. LinkedIn settles a disagreement. The two agreeing is not proof either.
- **`(profile disputed)` beside a company on tab 3** means the CRM and the
  provider give different answers to your ICP test. It is on the list so the
  question gets asked — a company wrongly rejected would otherwise never appear.
- **`Still at company?`** is the provider's view of where each person works now.
  `no — now at …` is a reason to check, not a conclusion. The row stays on the
  list either way. `(matched on LinkedIn)` means the person was found through
  the LinkedIn URL in your CRM rather than their email — worth the same check.
- **A provider title or LinkedIn column that is mostly blank is normal.** On
  tab 3 those columns are filled only where the provider differs from the CRM;
  on tab 1, only for people not in the CRM — which is what gives them a name,
  title and LinkedIn to act on.
- **`not found in <provider>`** means it was looked up and the provider has no
  record. It is not a verdict about the person or company.
- **Tab 4 is the work.** Every row says what to check and where. Nothing in it
  has been changed anywhere — the CRM is updated by whoever works through it.

## Why the second run is worth more than the first

One run answers who you met. A series answers the questions that are actually
worth asking — who is new at this company, whose title changed, which coverage
gap closed, which company went quiet. None of that can be read from a single
file, and none of it can be recovered later from runs you did not keep. That is
what §14's retention requirement is for.

## If something looks wrong that is not listed here

`docs/supported-configuration.md` is the honest account of what this does and
does not handle. Read it before concluding anything is broken — several of the
things that look like defects are documented decisions.

Mistakes in companies, people and titles are expected. The value is not that
the output is perfect; it is that this reconciliation does not otherwise happen
at all.
