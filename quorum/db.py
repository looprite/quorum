"""Your Postgres database — the one holding the meeting and attendee data.

Every query the artifact makes against it lives here. Schema: migrations/.

`domain_kind` and `provider` are stored as text with a CHECK constraint rather
than as Postgres enums (see migrations/0001_core.sql), which is why no read
below casts `::text` to compare them.
"""

from __future__ import annotations

import contextlib
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row

from .config import Config


@contextlib.contextmanager
def connect(cfg: Config) -> Iterator[psycopg.Connection]:
    with psycopg.connect(cfg.database_url) as conn:
        yield conn


# --- Step 1 — the week's external attendees -------------------------------- #
#
# Serves: which rows exist on tab 1 (Met this week) at all, plus Name, Email,
# Company (domain) and the meeting titles.

WEEK_ATTENDEES_SQL = """
select m.id           as meeting_id,
       m.title        as meeting_title,
       m.start_time   as start_time,
       a.name         as attendee_name,
       lower(a.email) as email,
       a.domain       as domain,
       a.domain_kind  as domain_kind
from meetings m
join attendees a on a.meeting_id = m.id
join accounts  acc on acc.id = m.account_id
where acc.name = %(account)s
  and m.start_time >= %(start)s
  and m.start_time <  %(end)s
  and a.domain_kind = 'external'
order by m.start_time, a.domain, a.name;
"""


def week_attendees(conn: psycopg.Connection, cfg: Config) -> list[dict]:
    start, end = cfg.week_bounds()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            WEEK_ATTENDEES_SQL, {"account": cfg.account, "start": start, "end": end}
        )
        return cur.fetchall()


# --- Step 5 — all-time external meeting history ---------------------------- #
#
# Serves: 'Recent contact?' on tab 3. All-time, not the week — which is why the
# importer persists meetings rather than the pipeline paging Gong on each run.
#
# The meeting-size columns are what separate a relationship from an audience
# member: a 29-person training session is not seven touches of rapport.

MET_HISTORY_SQL = """
with ext as (
  select m.id as meeting_id, m.start_time, a.domain,
         lower(a.email) as email, a.name as attendee_name
  from meetings m
  join attendees a on a.meeting_id = m.id
  join accounts  acc on acc.id = m.account_id
  where acc.name = %(account)s
    and a.domain_kind = 'external'
    and a.email is not null
),
sizes as (
  select meeting_id, count(*) as external_attendees from ext group by meeting_id
)
select e.domain,
       e.email,
       max(e.attendee_name)         as attendee_name,
       count(distinct e.meeting_id) as meetings_ever,
       min(e.start_time)::date      as first_met,
       max(e.start_time)::date      as last_met,
       min(s.external_attendees)    as smallest_meeting,
       max(s.external_attendees)    as biggest_meeting
from ext e
join sizes s on s.meeting_id = e.meeting_id
where e.domain = any(%(domains)s)
group by e.domain, e.email;
"""


def met_history(
    conn: psycopg.Connection, cfg: Config, domains: list[str]
) -> dict[str, dict]:
    """All-time history keyed by lowercased email.

    KNOWN LIMITATION, recorded rather than hidden: the key is the email address,
    so a person who has changed address appears twice and their history splits.
    person_identifiers exists to collapse them; wiring it in here is the read
    path that retires the write-ahead noted in migrations/0002_identity.sql.
    """
    if not domains:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(MET_HISTORY_SQL, {"account": cfg.account, "domains": domains})
        return {r["email"]: r for r in cur.fetchall()}


# --- Step 4 — the account's focus profile ---------------------------------- #
#
# Serves: the ICP filter (employee band + HQ geography) on tab 2, and the
# seniority terms behind the senior CRM bench on tab 3.
#
# Read from the same connection as the meeting data, which is what makes moving
# a deployment to a different database a connection-string change.

FOCUS_PROFILE_SQL = """
select ufp.profile_data
from user_focus_profiles ufp
join accounts acc on acc.id = ufp.account_id
where acc.name = %(account)s and ufp.is_active = true
order by ufp.version_number desc
limit 1;
"""


def focus_profile(conn: psycopg.Connection, cfg: Config) -> dict:
    with conn.cursor() as cur:
        cur.execute(FOCUS_PROFILE_SQL, {"account": cfg.account})
        row = cur.fetchone()
    return (row[0] if row and row[0] else {}) or {}


# --- Step 4/5 — the account's resolved CRM field map ----------------------- #
#
# Serves: every Salesforce query the pipeline builds. The repository holds the
# patterns; the resolved API names for this org live here (see the README's
# 'CRM field map'). Read once per run and handed to the client.

CRM_FIELD_MAP_SQL = """
select cfm.field_map
from crm_field_maps cfm
join accounts acc on acc.id = cfm.account_id
where acc.name = %(account)s and cfm.is_active = true
order by cfm.version_number desc
limit 1;
"""


def crm_field_map(conn: psycopg.Connection, cfg: Config) -> dict:
    with conn.cursor() as cur:
        cur.execute(CRM_FIELD_MAP_SQL, {"account": cfg.account})
        row = cur.fetchone()
    return (row[0] if row and row[0] else {}) or {}


def account_id(conn: psycopg.Connection, cfg: Config) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("select id from accounts where name = %(account)s", {"account": cfg.account})
        row = cur.fetchone()
    return str(row[0]) if row else None


def internal_domains(conn: psycopg.Connection, cfg: Config) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select internal_domains from accounts where name = %(account)s",
            {"account": cfg.account},
        )
        row = cur.fetchone()
    return list(row[0]) if row and row[0] else []
