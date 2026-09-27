"""`quorum init` — the two rows a fresh deployment cannot start without.

The migrations create empty tables. Two rows have to exist before the pipeline
does anything useful, and this is what creates them:

  * the **account**, which every query in the product scopes on and which the
    importer reads `internal_domains` from to tell a colleague from a customer.
    Without it `quorum import` stops and says so.

  * the **active focus profile**, which carries the ICP test (employee band, HQ
    geography) and the seniority bar behind the stakeholder list. Without it a
    run would have no ICP test to apply, and every company met would pass — an
    artifact that looks entirely normal and is wrong. `quorum.weekly.run`
    refuses to start without one; this module is how you satisfy it.

A third row is written where a CRM is reachable: the **resolved field map**
(`crm/fieldmap.py`), which is what lets every Salesforce query name
standard fields plus whatever this org actually calls the rest. It is resolved
by describing and counting rather than typed by anyone, and re-resolved by
`quorum resolve-fields` when the org's schema moves.

Everything written here is account configuration, not customer data. Nothing
customer-specific is in this file: the values come from the command line, the
account name from ACCOUNT_DOMAIN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

import psycopg
from psycopg.types.json import Jsonb

from .config import Config
from . import geography
from .weekly.coverage import SENIORITY_KEYWORDS

NOTE = "created by quorum init"


class InitError(RuntimeError):
    """Bad input, or a deployment that is already initialised."""


# --- What goes in the profile ---------------------------------------------- #


def normalise_domains(domains: Iterable[str]) -> list[str]:
    """Lowercase, de-duplicated, order preserved.

    A leading '@' is tolerated because it is the natural thing to type. An
    address, a URL or a bare word is not: `internal_domains` is matched against
    the domain part of an attendee's email, so `you@acme.com` here would match
    nothing and quietly classify every colleague as an external attendee — the
    whole company, on tab 1, every week.
    """
    out: list[str] = []
    for raw in domains:
        d = raw.strip().lower().lstrip("@")
        if not d:
            continue
        if "@" in d or "/" in d or " " in d or "." not in d:
            raise InitError(
                f"{raw!r} is not a domain. Give the part after the @, "
                "e.g. --internal-domains acme.com,acme.io"
            )
        if d not in out:
            out.append(d)
    if not out:
        raise InitError(
            "--internal-domains is required: with none, every attendee "
            "including your own colleagues is classified external."
        )
    return out


def build_profile(
    employee_min: int,
    employee_max: int,
    geographies: Iterable[str],
    seniority: Iterable[str],
) -> dict:
    """The `profile_data` jsonb, in exactly the four keys the pipeline reads.

    Keys and their readers: `employee_count_min` / `employee_count_max` and
    `hq_geographies` are the ICP test in `weekly/coverage.py:meets_profile`;
    `focus_seniority` becomes the Salesforce Title terms in `seniority_terms`.

    A geography the ICP test cannot act on is refused here rather than warned
    about. It used to be a warning, and a profile naming a region the test did
    not know applied no geography filter at all — every company met passed.
    """
    if employee_min < 0 or employee_max < 0:
        raise InitError("Employee counts cannot be negative.")
    if employee_min > employee_max:
        raise InitError(
            f"--employee-min {employee_min} is above --employee-max {employee_max}; "
            "no company can match that band."
        )

    geos = [g for g in (geographies or []) if g]
    if not geos:
        raise InitError("--geographies is required (e.g. 'North America').")
    try:
        selections = geography.parse_selections(geos)
    except geography.GeographyError as exc:
        raise InitError(str(exc)) from exc

    levels = [s.strip().lower() for s in seniority if s.strip()]
    if not levels:
        raise InitError(
            "--seniority is required: it is the bar a Salesforce title has to "
            "clear to reach the stakeholder list."
        )

    return {
        "employee_count_min": employee_min,
        "employee_count_max": employee_max,
        # Stored level-aware. A bare string still reads as a region everywhere,
        # which is what the profiles written before levels existed hold.
        "hq_geographies": selections,
        "focus_seniority": levels,
    }


def profile_warnings(profile: dict) -> list[str]:
    """Ways a profile can be accepted by the schema and still not mean what the
    operator thinks. Said once, here, rather than discovered in an artifact."""
    out: list[str] = []

    unknown = [
        level
        for level in profile.get("focus_seniority") or []
        if level not in SENIORITY_KEYWORDS
    ]
    if unknown:
        out.append(
            f"Seniority level(s) not recognised: {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(SENIORITY_KEYWORDS))}. An unrecognised "
            "level is matched against the Salesforce Title verbatim, so a typo "
            "matches nobody and the company's bench comes back empty."
        )

    return out


# --- Writing them ----------------------------------------------------------- #


@dataclass
class InitResult:
    account_id: str
    account: str = "created"          # created | updated | unchanged
    internal_domains: list[str] = field(default_factory=list)
    profile: str = "created"          # created | unchanged | replaced
    profile_version: int = 1
    warnings: list[str] = field(default_factory=list)


@dataclass
class FieldMapResult:
    version: int
    action: str                       # created | replaced
    field_map: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def init_deployment(
    conn: psycopg.Connection,
    cfg: Config,
    internal_domains: Iterable[str],
    profile_data: dict,
    replace: bool = False,
) -> InitResult:
    """Create (or update) the account and install an active focus profile.

    Idempotent: run it twice with the same arguments and the second run writes
    nothing. Run it with a *different* profile and it refuses — a profile change
    alters which companies appear on the map, so superseding one is `--replace`,
    which leaves the old version in place, inactive (see migrations/0003).

    Which is also how a domain is added to an account that is already running:
    same profile arguments, one more domain, no new profile version.
    """
    domains = normalise_domains(internal_domains)
    account_id, account_action = _upsert_account(conn, cfg.account, domains)
    profile_action, version = _install_profile(conn, account_id, profile_data, replace)
    return InitResult(
        account_id=account_id,
        account=account_action,
        internal_domains=domains,
        profile=profile_action,
        profile_version=version,
        warnings=profile_warnings(profile_data),
    )


def _upsert_account(
    conn: psycopg.Connection, name: str, domains: list[str]
) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "select id, internal_domains from accounts where name = %s", (name,)
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "insert into accounts (name, internal_domains) values (%s, %s) "
                "returning id",
                (name, domains),
            )
            return str(cur.fetchone()[0]), "created"

        account_id, existing = str(row[0]), list(row[1] or [])
        if existing == domains:
            return account_id, "unchanged"
        cur.execute(
            "update accounts set internal_domains = %s, updated_at = now() "
            "where id = %s",
            (domains, account_id),
        )
        return account_id, "updated"


def _install_profile(
    conn: psycopg.Connection, account_id: str, profile_data: dict, replace: bool
) -> tuple[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "select version_number, profile_data from user_focus_profiles "
            "where account_id = %s and is_active order by version_number desc limit 1",
            (account_id,),
        )
        active = cur.fetchone()

        # Already exactly this profile: nothing to write, and no new version to
        # imply a change of ICP that did not happen.
        if active and active[1] == profile_data:
            return "unchanged", int(active[0])

        if active and not replace:
            raise InitError(
                f"A different active focus profile already exists (version "
                f"{active[0]}): {describe(active[1])}. Pass --replace to supersede "
                "it with a new version; the old one is kept, inactive."
            )

        if active:
            cur.execute(
                "update user_focus_profiles set is_active = false "
                "where account_id = %s and is_active",
                (account_id,),
            )

        # Across every version, not just the active ones: (account_id,
        # version_number) is unique, so a replaced profile still holds its number.
        cur.execute(
            "select coalesce(max(version_number), 0) + 1 from user_focus_profiles "
            "where account_id = %s",
            (account_id,),
        )
        version = int(cur.fetchone()[0])
        cur.execute(
            "insert into user_focus_profiles "
            "(account_id, version_number, is_active, profile_data, note) "
            "values (%s, %s, true, %s, %s)",
            (account_id, version, Jsonb(profile_data), NOTE),
        )
        return ("replaced" if active else "created"), version


def describe(profile: dict) -> str:
    """The profile as one line, for the operator to check against what they meant."""
    return (
        f"{profile.get('employee_count_min')}-{profile.get('employee_count_max')} "
        f"employees · {geography.label(geography.parse_selections(profile.get('hq_geographies')))} · "
        f"{', '.join(profile.get('focus_seniority') or [])}"
    )


# --- The resolved CRM field map -------------------------------------------- #


def install_field_map(
    conn: psycopg.Connection,
    account_id: str,
    field_map: dict,
    provenance: dict,
    note: str = NOTE,
) -> FieldMapResult:
    """Store a freshly resolved map as the active one, superseding any before it.

    Always a new version rather than an update in place, and unconditionally —
    unlike the focus profile, re-resolving is not a decision someone might make
    by accident. It is what you run *because* the org changed, and the previous
    version has to stay readable to explain what last week's artifact read.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select version_number from crm_field_maps "
            "where account_id = %s and is_active order by version_number desc limit 1",
            (account_id,),
        )
        active = cur.fetchone()
        if active:
            cur.execute(
                "update crm_field_maps set is_active = false "
                "where account_id = %s and is_active",
                (account_id,),
            )
        cur.execute(
            "select coalesce(max(version_number), 0) + 1 from crm_field_maps "
            "where account_id = %s",
            (account_id,),
        )
        version = int(cur.fetchone()[0])
        cur.execute(
            "insert into crm_field_maps "
            "(account_id, version_number, is_active, field_map, provenance, note) "
            "values (%s, %s, true, %s, %s, %s)",
            (account_id, version, Jsonb(field_map), Jsonb(provenance), note),
        )
    return FieldMapResult(
        version=version,
        action="replaced" if active else "created",
        field_map=field_map,
        provenance=provenance,
    )


def has_field_map(conn: psycopg.Connection, account_id: str) -> Optional[int]:
    """The active map's version, or None. `quorum init` leaves an existing map
    alone — re-resolving is `quorum resolve-fields`, on purpose."""
    with conn.cursor() as cur:
        cur.execute(
            "select version_number from crm_field_maps "
            "where account_id = %s and is_active order by version_number desc limit 1",
            (account_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


