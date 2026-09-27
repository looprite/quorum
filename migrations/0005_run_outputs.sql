-- Quorum v1 — 0005_run_outputs: the three files a weekly run emits, kept as
-- rows, for a deployment that has opted into database retention.
--
-- A weekly run cannot be reproduced: company coverage, the ICP verdicts and
-- the stakeholder list are computed against Salesforce and HubSpot as they
-- stood that week, and none of it is written back to the database
-- (quorum/weekly/run.py). A run that is not kept is gone. This table is
-- where it is kept, when RETAIN_RUNS is on (quorum/config.py). See
-- docs/setup.md §14.
--
-- Written by quorum/weekly/retention.py, in one transaction per run — a
-- partial failure must leave no rows, not one file and the appearance of a
-- stored run.
--
-- run_date is written by the caller from the run's week start, and never
-- inferred from now(): storage can happen well after the run it belongs to —
-- a backfill, a retry the next morning, a file copied in by hand — and a row
-- dated by when it was stored is a row that cannot be lined up against
-- anything.
--
-- No account_id, unlike the pipeline's own tables: one deployment serves one
-- account, so there is nothing here to scope against.
--
-- Optional, unlike 0001-0004: this migration is applied only by a deployment
-- that has chosen retention (docs/setup.md §14), not as part of the base
-- schema every deployment needs to run at all.

create table run_outputs (
  id         bigserial   primary key,
  run_date   date        not null,
  file_type  text        not null check (file_type in ('xlsx', 'json', 'html')),
  content    bytea       not null,
  created_at timestamptz not null default now()
);
