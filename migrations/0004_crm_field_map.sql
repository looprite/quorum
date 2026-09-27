-- Quorum v1 — 0004_crm_field_map: the resolved API names of every non-standard
-- field the pipeline reads.
--
-- The README's rule (CRM field map): the pipeline never hardcodes a
-- non-standard field name. A field that does not exist raises INVALID_FIELD and
-- kills the whole query rather than blanking a column, so a hardcoded custom
-- name is a run that dies at the next deployment — and it is per-deployment
-- differentiation living in code, which forces the fork this repository bans.
-- The repository holds the patterns; the resolved names live here.
--
-- Provenance: README 'CRM field map'. Resolution is
-- `quorum/crm/fieldmap.py`, run by `quorum init` and re-run by
-- `quorum resolve-fields`.
--
-- Why this is a table of its own rather than more keys in the focus profile:
-- the two are different kinds of configuration with different authors. The
-- focus profile is business intent, typed by a person who knows the market —
-- employee band, geographies, seniority. The field map is technical resolution,
-- discovered by the software against one org's schema, and no person should be
-- typing a managed-package country field's API name into anything. They also
-- change for unrelated reasons: an ICP shifts when the go-to-market shifts, a
-- field map shifts when an admin adds a field or a package is installed.
--
-- Departure worth stating: there is no `crm` column. Salesforce is the only CRM
-- whose custom fields v1 reads — HubSpot is queried by email and domain and
-- touches no org-local field. A column naming a system that has only one value
-- and no reader is the kind of speculation `migrations/README.md` rules out;
-- when a second CRM needs a map, it is added then.

create table crm_field_maps (
  id             uuid primary key default gen_random_uuid(),
  account_id     uuid not null references accounts(id) on delete cascade,

  -- Versioned for the same reason the focus profile is: which field the
  -- artifact read decides what it said, so last week's numbers have to stay
  -- explicable after an admin adds a better-populated field and someone
  -- re-resolves.
  version_number integer     not null default 1,
  is_active      boolean     not null default true,

  -- Read by every Salesforce query the pipeline builds:
  --   {"Account": {"employee_count": ["NumberOfEmployees", "<Pkg>__Size__c"],
  --                "hq_country": [...], "hq_city": [...], "hq_state": [...]},
  --    "Contact": {"linkedin_url": [...]}}
  -- A LIST per logical field, best-populated first, not a single name: the
  -- first populated value wins at read time. On one real org some companies
  -- met in a week have an empty package country and a populated
  -- BillingCountry, and a single-name map would blank their HQ and drop them
  -- out of the ICP set.
  field_map      jsonb       not null,

  -- Read by a human, and by the JSON dump the artifact ships. Per logical
  -- field: what it serves, every candidate with its populated-row count and
  -- percentage, and every field rejected with the rule that rejected it. This
  -- is what makes the choice arguable instead of trusted — on one real org the
  -- best-populated field matching /linkedin/ on Contact is the *company's*
  -- page, and the record of why it was rejected is the only way to see that.
  provenance     jsonb       not null,

  -- When the org was described. A map is a snapshot of someone else's schema.
  resolved_at    timestamptz not null default now(),

  note           text,
  created_at     timestamptz not null default now(),

  unique (account_id, version_number)
);

-- One active map per account, enforced rather than assumed: two would make the
-- fields a run reads depend on row order.
create unique index crm_field_maps_one_active
  on crm_field_maps (account_id)
  where is_active;
