"""Entry points.

    quorum init --internal-domains … --employee-min … …  # once, after the migrations
    quorum resolve-fields                                # re-read the CRM's schema
    quorum import                                        # the last RECENT_DAYS days
    quorum import --from 2025-10-01 --to 2026-08-24      # a longer history
    quorum import --yesterday                            # the overnight run
    quorum weekly                                        # the artifact

All three read their configuration from the environment. None writes to any
system other than the product database (init, import) or the local output
directory (weekly).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Optional

import psycopg

from . import bootstrap, db
from .config import Config
from . import geography
from .crm.fieldmap import FieldMapError, describe_lines, resolve
from .crm.salesforce import Salesforce
from .gong.client import GongClient
from .gong.importer import import_range
from .weekly.run import (
    MissingFieldMap,
    MissingFocusProfile,
    MissingRunOutputsTable,
    run_weekly,
)


def _require(cfg: Config) -> None:
    missing = cfg.missing()
    if missing:
        print(f"[!] Missing environment: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _geographies(value: str) -> list[dict]:
    """`--geographies 'North America,country:Germany'`.

    A bare value is a region, which is both the common case and what the
    profiles written before levels existed hold.
    """
    out = []
    for part in _csv(value):
        level, sep, name = part.partition(":")
        out.append(
            {"level": level.strip(), "value": name.strip()}
            if sep
            else {"level": "region", "value": part}
        )
    return out


def cmd_init(args, cfg: Config) -> int:
    """Create the account row and the active focus profile — the two rows the
    migrations leave empty and everything downstream needs."""
    _require(cfg)
    try:
        profile = bootstrap.build_profile(
            args.employee_min, args.employee_max, args.geographies, args.seniority
        )
        with db.connect(cfg) as conn:
            result = bootstrap.init_deployment(
                conn, cfg, args.internal_domains, profile, replace=args.replace
            )
            fields = _resolve_into(conn, result.account_id, cfg)
            conn.commit()
    except (bootstrap.InitError, FieldMapError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    except psycopg.errors.UndefinedTable:
        print(
            "[!] The schema is not there. Apply the migrations first — "
            "see migrations/README.md.",
            file=sys.stderr,
        )
        return 2

    print(
        f"[✓] Account {cfg.account} {result.account} · internal domains: "
        f"{', '.join(result.internal_domains)}"
    )
    print(
        f"[✓] Focus profile v{result.profile_version} {result.profile} · "
        f"{bootstrap.describe(profile)}"
    )
    _report_field_map(fields)

    # Warnings belong on stderr, but a report a human reads has to stay in
    # order when the two streams are captured to one file.
    sys.stdout.flush()
    for warning in result.warnings:
        print(f"[!] {warning}", file=sys.stderr)
    sys.stderr.flush()
    print("[*] Next: quorum import")
    return 0


def _resolve_into(conn, account_id: str, cfg: Config, force: bool = False):
    """Resolve the CRM field map and store it, where there is a CRM to read.

    Returns the stored result, or a string saying why there is none — an
    unconfigured Salesforce is a legitimate state (the run reports 'not checked'
    throughout), and an already-resolved map is left alone unless this is the
    re-resolve command.
    """
    sf = Salesforce(cfg)
    if not sf.configured:
        return "Salesforce not configured — no field map resolved."
    existing = bootstrap.has_field_map(conn, account_id)
    if existing and not force:
        return (
            f"Field map v{existing} already resolved — left alone. "
            "Re-read the org's schema with `quorum resolve-fields`."
        )
    field_map, provenance = resolve(sf)
    return bootstrap.install_field_map(conn, account_id, field_map, provenance)


def _report_field_map(fields) -> None:
    if isinstance(fields, str):
        print(f"[i] {fields}")
        return
    print(f"[✓] CRM field map v{fields.version} {fields.action}, by describing the org:")
    for line in describe_lines(fields.field_map):
        print(f"      {line}")


def cmd_resolve_fields(args, cfg: Config) -> int:
    """Re-read the org's schema and store a new version of the field map.

    Run it when an admin adds a field, a package is installed or removed, or a
    column in the artifact stops looking right. The superseded version stays,
    inactive, so what last week's artifact read is still answerable.
    """
    _require(cfg)
    try:
        with db.connect(cfg) as conn:
            account_id = db.account_id(conn, cfg)
            if not account_id:
                print(
                    f"[!] No account named {cfg.account!r}. Run `quorum init` first.",
                    file=sys.stderr,
                )
                return 2
            fields = _resolve_into(conn, account_id, cfg, force=True)
            conn.commit()
    except FieldMapError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    except psycopg.errors.UndefinedTable:
        print(
            "[!] The schema is not there. Apply the migrations first — "
            "see migrations/README.md.",
            file=sys.stderr,
        )
        return 2

    if isinstance(fields, str):
        print(f"[!] {fields}", file=sys.stderr)
        return 2
    _report_field_map(fields)
    for line in _provenance_lines(fields.provenance):
        print(f"      {line}")
    return 0


def _provenance_lines(provenance: dict) -> list[str]:
    """The counts that justified each choice, and what was thrown out."""
    out = []
    for sobject in sorted(provenance):
        for logical, detail in sorted(provenance[sobject].get("fields", {}).items()):
            cands = ", ".join(
                f"{c['field']} {c['pct']}%" for c in detail.get("candidates", [])
            )
            out.append(f"{sobject}.{logical}: {cands or 'nothing resolved'}")
            for r in detail.get("rejected", []):
                out.append(f"  rejected {r['field']} — {r['why']}")
    return out


def import_window(
    cfg: Config,
    from_date: Optional[str],
    to_date: Optional[str],
    yesterday: bool = False,
    today: Optional[dt.date] = None,
) -> tuple[str, str]:
    """The date range to import, as (from, to), both inclusive.

    With no arguments: the last `RECENT_DAYS` days. That is not a default
    chosen for being round — it is the same window `weekly/stakeholders.py`
    tests `last_met` against to answer 'Recent contact?'. Importing less would
    put 'no' next to people who were met inside it, and deriving the window from
    the setting keeps that one number instead of two that can drift apart.

    `--from/--to` extend the history beyond it and are unchanged.
    """
    today = today or dt.date.today()

    if yesterday:
        if from_date or to_date:
            raise ValueError("--yesterday cannot be combined with --from/--to.")
        day = (today - dt.timedelta(days=1)).isoformat()
        return day, day

    if from_date or to_date:
        if not (from_date and to_date):
            raise ValueError("Give both --from and --to, or neither.")
        return from_date, to_date

    return (today - dt.timedelta(days=cfg.recent_days)).isoformat(), today.isoformat()


def cmd_import(args, cfg: Config) -> int:
    _require(cfg)
    if not cfg.gong.configured:
        print(
            "[!] Gong credentials not configured "
            "(GONG_ACCESS_KEY / GONG_ACCESS_KEY_SECRET).",
            file=sys.stderr,
        )
        return 2

    try:
        from_date, to_date = import_window(
            cfg, args.from_date, args.to_date, args.yesterday
        )
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    if not (args.from_date or args.to_date or args.yesterday):
        print(
            f"[*] No range given: importing the last {cfg.recent_days} days "
            f"(RECENT_DAYS), {from_date} .. {to_date}."
        )

    client = GongClient(
        cfg.gong.access_key, cfg.gong.access_key_secret, cfg.gong.base_url
    )

    with db.connect(cfg) as conn:
        account_id = db.account_id(conn, cfg)
        if not account_id:
            print(
                f"[!] No account named {cfg.account!r} in the database. "
                "Run the migrations and insert the account row first.",
                file=sys.stderr,
            )
            return 2
        internal = db.internal_domains(conn, cfg)
        if not internal:
            print(
                "[!] accounts.internal_domains is empty — every attendee would be "
                "classified external. Set it before importing.",
                file=sys.stderr,
            )
            return 2

        result = import_range(
            conn, client, account_id, internal, from_date, to_date
        )
        conn.commit()

    print(f"[✓] {result}")
    return 0


def cmd_weekly(args, cfg: Config) -> int:
    _require(cfg)
    try:
        run_weekly(cfg)
    except (
        MissingFocusProfile,
        MissingFieldMap,
        MissingRunOutputsTable,
        geography.GeographyError,
    ) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quorum")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", help="Create the account and its focus profile (run once)"
    )
    p_init.add_argument(
        "--internal-domains",
        type=_csv,
        required=True,
        metavar="A.COM,B.COM",
        help="The account's own email domains — how an attendee is told from a colleague",
    )
    p_init.add_argument(
        "--employee-min", type=int, required=True, help="ICP employee band, lower bound"
    )
    p_init.add_argument(
        "--employee-max", type=int, required=True, help="ICP employee band, upper bound"
    )
    p_init.add_argument(
        "--geographies",
        type=_geographies,
        required=True,
        metavar="'North America,country:Germany'",
        help=(
            "ICP HQ geographies. A bare value is a region "
            f"({', '.join(sorted(geography.REGIONS))}); prefix with 'country:' "
            "to name one country"
        ),
    )
    p_init.add_argument(
        "--seniority",
        type=_csv,
        required=True,
        metavar="C-LEVEL,VP,DIRECTOR",
        help="Seniority levels a title must reach for the stakeholder list",
    )
    p_init.add_argument(
        "--replace",
        action="store_true",
        help="Supersede the active focus profile with a new version (the old one is kept)",
    )
    p_init.set_defaults(func=cmd_init)

    p_resolve = sub.add_parser(
        "resolve-fields",
        help="Re-read the CRM's schema and store a new field map version",
    )
    p_resolve.set_defaults(func=cmd_resolve_fields)

    p_import = sub.add_parser(
        "import",
        help="Import meetings from Gong (default: the last RECENT_DAYS days)",
    )
    p_import.add_argument(
        "--from", dest="from_date", help="YYYY-MM-DD — extend the history back"
    )
    p_import.add_argument("--to", dest="to_date", help="YYYY-MM-DD")
    p_import.add_argument(
        "--yesterday", action="store_true", help="Import the previous day only"
    )
    p_import.set_defaults(func=cmd_import)

    p_weekly = sub.add_parser("weekly", help="Produce the weekly stakeholder map")
    p_weekly.set_defaults(func=cmd_weekly)

    args = parser.parse_args(argv)
    return args.func(args, Config())


if __name__ == "__main__":
    raise SystemExit(main())
