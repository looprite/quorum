-- Quorum v1 — 0002_identity: people, person_identifiers, person_attendees
--
-- ⚠ These three tables are WRITTEN by the importer and READ BY NOTHING in the
-- stakeholder-map artifact today. That is a deliberate, named exception to the
-- repo's "no tool ships without a customer-visible read path" rule, decided
-- 2026-08-24. The reason it is worth the exception:
--
--   The stakeholder list joins all-time meeting history to the CRM bench on a
--   lowercased email string. Someone who changed employer or email address is
--   two people to that join, and their history splits — which shows up in the
--   artifact as a wrong 'Recent contact?' value, silently. person_identifiers
--   is what collapses the aliases back to one person.
--
--   It is not hypothetical at real scale: on a real org a substantial share of
--   people carry more than one identifier.
--
-- The honest statement of the debt: MET_HISTORY_SQL does not use these tables
-- yet — it still groups by email. Making the history query resolve through
-- person_identifiers is the read path that retires this exception, and until
-- that happens these tables are carried, not consumed.
--
-- The write surface is quorum/gong/identity.py — the only code that touches
-- all three tables.

create table people (
  id         uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,

  -- Written by the identity resolver. Null for unmatched people (below), which
  -- the unique constraint permits — Postgres treats nulls as distinct.
  email      text,
  name       text,
  -- True when the attendee had no email at all, so no identifier exists to
  -- deduplicate on and each sighting gets its own record. These are the
  -- name-only rows the artifact reports as explicit gaps.
  unmatched  boolean     not null default false,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  unique (account_id, email)
);

-- `people` deliberately has no first_name, last_name, company, title,
-- linkedin_url, company_domain or company_id. Columns like those only get
-- populated by an enrichment or brief-generation path, which this pipeline
-- does not have, and none of them is read by the artifact — titles and
-- LinkedIn URLs come from Salesforce at read time. Left out rather than
-- carried: a column that arrives empty and stays empty reads as coverage that
-- does not exist. Add them in a later migration if and when a read path needs
-- them.

create table person_identifiers (
  -- Every email a person has ever been seen with, all pointing at one person
  -- record. This is the deduplication lookup key.
  id         uuid primary key default gen_random_uuid(),
  person_id  uuid not null references people(id) on delete cascade,
  account_id uuid not null references accounts(id) on delete cascade,
  email      text not null,
  created_at timestamptz not null default now(),

  unique (account_id, email)
);

create index person_identifiers_person_idx on person_identifiers (person_id);

create table person_attendees (
  -- One row per (person, attendance). This is what preserves attendance vs.
  -- invitation — the signal the README names as the reason meeting data matters
  -- here at all.
  id          uuid primary key default gen_random_uuid(),
  person_id   uuid not null references people(id) on delete cascade,
  attendee_id uuid not null references attendees(id) on delete cascade,
  created_at  timestamptz not null default now(),

  unique (person_id, attendee_id)
);

create index person_attendees_attendee_idx on person_attendees (attendee_id);
