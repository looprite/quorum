-- Quorum v1 — 0001_core: accounts, meetings, attendees
--
-- Designed backwards from what the pipeline reads. Every column below is either
-- read by a column of the stakeholder-map artifact or written by the Gong
-- importer that produces it; nothing is here speculatively. When a read path
-- later needs a column, a later migration adds it.
--
-- The write surface is quorum/gong/importer.py. The read surface is
-- WEEK_ATTENDEES_SQL and MET_HISTORY_SQL in quorum/db.py — the two queries the
-- weekly run makes against these tables.
--
-- The design decisions worth knowing about, each with its reason:
--
--   * No row-level security, and no dependency on a hosting platform's auth
--     schema. Quorum deploys single-tenant inside your own environment, with
--     account-scoped keys read from the environment rather than per-user
--     sessions — so row-level isolation has no user to key off, and policies
--     written against a managed platform's auth functions would not run on the
--     plain Postgres this is designed for.
--
--   * No credential columns. `accounts` holds no OAuth tokens, no enrichment
--     provider API key, no billing id, and no per-account Gong keys. The
--     README's rule is that secrets are read from the environment and never
--     committed — so Gong, Salesforce, HubSpot and provider credentials are env
--     config, not rows.
--
--   * `provider` and `domain_kind` are text + CHECK, not enums. Extending a
--     Postgres enum costs an `alter type ... add value` migration per value,
--     and comparing one in a query means casting `domain_kind::text` on every
--     read. A CHECK constraint extends with a plain ALTER and drops the cast.
--
--   * `accounts` keeps its id, and the account_id foreign keys stay, even though
--     a single-tenant deploy has exactly one row. Keeping the column is what
--     lets the same schema and the same queries ship to every deployment —
--     differentiation by config, never code.

create table accounts (
  id               uuid primary key default gen_random_uuid(),
  -- Matched by name against the ACCOUNT_DOMAIN env value ('acme.com').
  -- Read by: every query in the pipeline, as the account scope.
  name             text        not null unique,
  -- Read by: the Gong importer, to classify an attendee domain as internal
  -- vs external. `attendees.domain_kind` is derived from it at import time.
  internal_domains text[]      not null default '{}',
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create table meetings (
  id          uuid primary key default gen_random_uuid(),
  account_id  uuid not null references accounts(id) on delete cascade,

  -- Written by: the importer. 'gong' is the only value it produces today;
  -- the others are carried so a second source does not need a migration.
  provider    text not null
                check (provider in ('gong', 'zoom', 'google_meet', 'teams', 'other')),
  -- The provider's own call id. With account_id and provider it is what makes
  -- the importer's upsert idempotent, so a re-run of an overlapping date range
  -- does not duplicate meetings.
  provider_id text not null,

  -- Read by: WEEK_ATTENDEES_SQL (the meeting titles aggregated per person on
  -- tab 1).
  title       text,
  -- Read by: WEEK_ATTENDEES_SQL (the week window) and MET_HISTORY_SQL
  -- (first_met / last_met, and the recency test behind 'Recent contact?').
  start_time  timestamptz,

  -- Written by the importer, read by nothing yet. Kept because they arrive free
  -- in the same upsert and dropping them would mean re-importing to get them
  -- back; called out here so they are not mistaken for a live read path.
  end_time         timestamptz,
  duration_seconds integer,

  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  unique (account_id, provider, provider_id)
);

-- WEEK_ATTENDEES_SQL filters one week by start_time within an account;
-- MET_HISTORY_SQL scans all of an account's meetings, which grows without
-- bound as history is imported.
create index meetings_account_start_idx on meetings (account_id, start_time);

create table attendees (
  id           uuid primary key default gen_random_uuid(),
  meeting_id   uuid not null references meetings(id) on delete cascade,
  account_id   uuid not null references accounts(id) on delete cascade,

  -- Read by: 'Name' on tab 1, and the name-only gap rows. Nullable on
  -- purpose — an attendee with no name is a gap to report, not a row to drop.
  name         text,
  -- Read by: every reconciliation lookup (Salesforce and HubSpot are both
  -- queried by email) and the join key of MET_HISTORY_SQL. Lowercased at read
  -- time by the pipeline, not stored normalised.
  email        text,
  -- Read by: company grouping (company coverage and the stakeholder list are
  -- keyed on domain) and the
  -- Salesforce/HubSpot domain queries.
  domain       text,
  -- Read by: the external-attendee filter that both SQL queries open with.
  -- Derived at import from accounts.internal_domains.
  domain_kind  text
                 check (domain_kind in ('internal', 'external', 'personal')),

  -- Written by the importer as the Gong party id; the importer also uses it to
  -- tell a re-import of the same call from a new attendee.
  provider     text not null
                 check (provider in ('gong', 'zoom', 'google_meet', 'teams', 'other')),
  provider_uid text,

  created_at   timestamptz not null default now()
);

-- Both queries filter external attendees within an account before anything else.
create index attendees_account_kind_idx on attendees (account_id, domain_kind);
-- MET_HISTORY_SQL groups by (domain, email) across all of an account's history.
create index attendees_account_domain_idx on attendees (account_id, domain);
create index attendees_meeting_idx        on attendees (meeting_id);
create index attendees_email_idx          on attendees (account_id, lower(email));
