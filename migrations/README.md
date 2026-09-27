# Migrations

The v1 schema. Applied once per deployment, against a database in your own
environment. There is no Quorum-owned database.

Apply in filename order:

```
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_core.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0002_identity.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0003_focus_profile.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0004_crm_field_map.sql
```

They create empty tables. `quorum init` writes the rows a deployment cannot
start without — the account, its active focus profile, and the resolved CRM
field map — and is the next step after the files above (`docs/pipeline.md`,
steps 0a and 0b).

Verified 2026-08-24 on PostgreSQL 16.13: all four of the files above apply
clean from empty, and
the three queries the pipeline makes — `MEETINGS_SQL`, `MET_HISTORY_SQL` and the
active-focus-profile select — run verbatim against the result.

Separately verified against a full real dataset: every constraint these
migrations add — the two CHECKs, the meeting uniqueness key, the
`(person_id, attendee_id)` uniqueness, and the one-active-profile index — is
already satisfied by real rows.

## Assumptions

- **PostgreSQL.** `gen_random_uuid()` (core since 13), `text[]`, `jsonb` and a
  partial unique index. The README says the engine is your choice and not
  necessarily a managed one; it does say Postgres, and this schema assumes
  that. A non-Postgres engine is a porting exercise, not a config change.
- **Single tenant per deployment.** No row-level security, no `auth.uid()`, no
  `users` table. `account_id` is kept on every table so the same queries ship
  everywhere, but one deployment holds one account.
- **No credentials in the database.** Gong, Salesforce and HubSpot keys are
  read from the environment, never stored as account columns.

## Shape

Eight tables, plus `run_outputs` if you apply the optional migration below.
Five are read by the artifact, three are written by the importer
and not yet read — a deliberate, documented exception in `0002_identity.sql`,
not an oversight.

```
accounts ─┬─ meetings ── attendees ── person_attendees ── people ── person_identifiers
          ├─ user_focus_profiles
          └─ crm_field_maps
```

Every column carries the read path that justifies it, in a comment. A column
with no reader named should not be added.

## Retention (optional)

`0005_run_outputs.sql` is not part of the block above and not something every
deployment needs to run. It adds one table, `run_outputs`, for a deployment
that has chosen to keep every weekly run in the database — `docs/setup.md`
§14, Option A. Apply it there, once you have made that choice, and grant the
pipeline's own role `INSERT` and `SELECT` on it and nothing else.

## Adding one

Next file is `0006_`. Migrations are append-only once a deployment has run
them — the database is yours, holds your live data, and cannot be rebuilt from
scratch to accommodate an edit.
