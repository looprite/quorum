-- Quorum v1 — 0003_focus_profile: the per-account ICP configuration
--
-- The focus profile drives the whole product: it decides which companies are
-- ICP fits (employee band + HQ geography) and which titles clear the seniority
-- bar for the stakeholder list. It lives in your own database, read over the
-- same connection as the meeting data — so pointing the pipeline at a
-- different deployment is a connection-string change, not a code change.
--
-- This is per-account ICP configuration: which companies count, and which
-- titles are senior enough to list. It is not per-contact scoring — there is
-- no such feature here, and the profile is not a ranking input.
--
-- Read by FOCUS_PROFILE_SQL in quorum/db.py. The ICP test is applied in
-- quorum/weekly/coverage.py; the seniority terms it carries are used by
-- quorum/weekly/stakeholders.py.
--
-- The profile is scoped to the ACCOUNT, not to a user. A per-user profile
-- would need a `users` table and a hosted auth system to key off, and this
-- deploys single-tenant with neither; the pipeline already selects on
-- (account, is_active, max version) without reference to a user, so a user
-- column would be a foreign key to nothing, guarding a distinction the read
-- path never makes. In practice a deployment has exactly one active profile.

create table user_focus_profiles (
  id             uuid primary key default gen_random_uuid(),
  account_id     uuid not null references accounts(id) on delete cascade,

  -- Versioned rather than updated in place: a profile change alters which
  -- companies appear on the map, so the run that produced a given artifact
  -- needs to stay reconstructable.
  version_number integer     not null default 1,
  is_active      boolean     not null default true,

  -- Read by the pipeline as:
  --   employee_count_min / employee_count_max  -> the ICP employee band
  --   hq_geographies                           -> the HQ geography test
  --   focus_seniority                          -> the Salesforce Title LIKE clause
  --                                               behind the senior CRM bench
  -- Left as jsonb: the profile's shape is configuration, and pinning it into
  -- columns would put per-deployment differentiation in the schema.
  profile_data   jsonb       not null,

  note           text,
  created_at     timestamptz not null default now(),

  unique (account_id, version_number)
);

-- The pipeline reads the active profile for an account. One active row at a
-- time, enforced rather than assumed — two would make the run's ICP filter
-- depend on row order.
create unique index user_focus_profiles_one_active
  on user_focus_profiles (account_id)
  where is_active;

-- The 'user_' prefix is historical: the table is account-scoped, not per-user,
-- and nothing reads it per-user. The name is kept so that an existing data
-- load and the pipeline's query stay unchanged; renaming it is a cosmetic
-- change, better done once a load is finished than in the middle of one.
