"""Gong import.

Writes `meetings` and `attendees`, and resolves people through identity.py.
This is the only part of the system that writes; everything else reads.

Deliberately absent: any handling of transcripts. Quorum stores meeting
metadata and attendees only, so there is nothing here that fetches, flattens
or stores transcript text, and `meetings` has no column to put it in.

Idempotency has two layers, because a backfill and an overnight run will overlap:
  * the meeting upsert keys on (account_id, provider, provider_id);
  * a call whose attendees are already present is skipped rather than doubled.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from ..domains import classify_domain, domain_of
from .client import GongClient
from .identity import deduplicate_attendees

BATCH_SIZE = 50  # call ids per getCallsExtensive request


def format_elapsed(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@dataclass
class ImportResult:
    calls_seen: int = 0
    calls_processed: int = 0
    calls_already_imported: int = 0
    meetings_upserted: int = 0
    attendees_created: int = 0
    people_created: int = 0
    calls_without_parties: int = 0
    # Reported next to the counts because the decision it informs is whether to
    # extend the range. Calls-in-range and the time it took are the two numbers
    # someone needs to work out what a year would cost before starting one.
    elapsed_seconds: float = 0.0

    def __str__(self) -> str:
        return (
            f"{self.calls_seen} calls in range · {self.meetings_upserted} meetings "
            f"upserted · {self.attendees_created} attendees · {self.people_created} "
            f"new people · {self.calls_already_imported} calls already imported · "
            + (
                f"{self.calls_without_parties} with no attendees · "
                if self.calls_without_parties
                else ""
            )
            + f"{format_elapsed(self.elapsed_seconds)} elapsed"
        )


def import_range(
    conn: psycopg.Connection,
    client: GongClient,
    account_id: str,
    internal_domains: list[str],
    from_date: str,
    to_date: str,
    log=print,
) -> ImportResult:
    result = ImportResult()
    started = time.monotonic()
    try:
        call_ids = list(client.iter_call_ids(from_date, to_date))
        result.calls_seen = len(call_ids)
        log(f"[*] {len(call_ids)} calls between {from_date} and {to_date}")
        if not call_ids:
            return result

        for i in range(0, len(call_ids), BATCH_SIZE):
            batch = call_ids[i : i + BATCH_SIZE]
            extensive = client.get_calls_extensive(batch)

            for call in extensive.get("calls") or []:
                meta = call.get("metaData") or {}
                provider_id = meta.get("id")
                if not provider_id:
                    continue

                meeting_id = _upsert_meeting(conn, account_id, meta)
                result.meetings_upserted += 1

                if _has_attendees(conn, meeting_id):
                    result.calls_already_imported += 1
                    result.calls_processed += 1
                    continue

                parties = call.get("parties") or []
                rows = _attendee_rows(parties, meeting_id, account_id, internal_domains)
                if not rows:
                    result.calls_without_parties += 1
                    result.calls_processed += 1
                    continue

                inserted = _insert_attendees(conn, rows)
                result.attendees_created += len(inserted)
                result.people_created += deduplicate_attendees(conn, inserted, account_id)
                result.calls_processed += 1

            conn.commit()
            log(f"[*] {result.calls_processed}/{len(call_ids)} calls")

        return result
    finally:
        # In a finally so every exit — including the empty range — carries it.
        result.elapsed_seconds = time.monotonic() - started


def _upsert_meeting(conn: psycopg.Connection, account_id: str, meta: dict) -> str:
    start = _parse_ts(meta.get("started"))
    duration = meta.get("duration")
    end = start + dt.timedelta(seconds=duration) if (start and duration) else None

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into meetings
              (account_id, provider, provider_id, title, start_time, end_time, duration_seconds)
            values (%s, 'gong', %s, %s, %s, %s, %s)
            on conflict (account_id, provider, provider_id) do update
              set title            = excluded.title,
                  start_time       = excluded.start_time,
                  end_time         = excluded.end_time,
                  duration_seconds = excluded.duration_seconds,
                  updated_at       = now()
            returning id
            """,
            (account_id, meta["id"], meta.get("title"), start, end, duration),
        )
        return str(cur.fetchone()[0])


def _has_attendees(conn: psycopg.Connection, meeting_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select 1 from attendees where meeting_id = %s limit 1", (meeting_id,))
        return cur.fetchone() is not None


def _attendee_rows(
    parties: list[dict], meeting_id: str, account_id: str, internal_domains: list[str]
) -> list[tuple]:
    rows = []
    for p in parties:
        # A party with neither a name nor an email is not a person.
        if not (p.get("name") or p.get("emailAddress")):
            continue

        email = (p.get("emailAddress") or "").strip().lower() or None
        domain = domain_of(email)

        # Gong's own affiliation is trusted where it exists; otherwise classify
        # by domain. An external party with no email stays external — it is a
        # gap to report, not a row to drop.
        affiliation = p.get("affiliation")
        if affiliation == "Internal":
            kind: Optional[str] = "internal"
        elif affiliation == "External":
            kind = classify_domain(domain, internal_domains) if domain else "external"
        else:
            kind = classify_domain(domain, internal_domains) if domain else None

        rows.append(
            (
                meeting_id,
                account_id,
                p.get("name"),
                email,
                domain,
                kind,
                "gong",
                p.get("userId") or p.get("id"),
            )
        )
    return rows


def _insert_attendees(conn: psycopg.Connection, rows: list[tuple]) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.executemany(
            """
            insert into attendees
              (meeting_id, account_id, name, email, domain, domain_kind, provider, provider_uid)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            returning id, email, name, domain_kind
            """,
            rows,
            returning=True,
        )
        out: list[dict] = []
        while True:
            row = cur.fetchone()
            if row:
                out.append({**row, "id": str(row["id"])})
            if not cur.nextset():
                break
        return out


def _parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
