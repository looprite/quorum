"""A real Postgres, the real migrations, and a fake Gong.

The CRM and provider legs a test cannot reach are left
unconfigured on purpose, so the run has to produce an artifact with stated gaps
rather than silently missing columns. That is the behaviour worth testing; the
CRM legs themselves are verified by hand against the live systems.

The absence is enforced rather than assumed — see `no_developer_credentials`
below.

Set QUORUM_TEST_DSN to a Postgres a test may create databases on. Without it the
database tests skip rather than fail.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from types import SimpleNamespace

import psycopg
import pytest

from quorum.config import SalesforceConfig

MIGRATIONS = sorted(
    (pathlib.Path(__file__).resolve().parents[1] / "migrations").glob("0*.sql")
)


# The CRM legs this suite requires to be absent. SF_ is a prefix rather than a
# list so a new Salesforce variable cannot quietly reintroduce the problem.
# DATABASE_URL and the Gong pair are here for a second reason: a test that
# builds a configuration object picks up whatever the developer's .env holds for
# anything it does not set, and a failing assertion prints that object. Clearing
# them means a traceback has nothing live to print.
CREDENTIAL_VARS = ("HUBSPOT_SERVICE_KEY", "DATABASE_URL")
CREDENTIAL_PREFIXES = ("SF_", "GONG_")


def crm_config(configured: bool = True):
    """A configuration stub for the CRM adapters — never a real `Config`.

    A real one reads the environment for every field a test does not set, so a
    failing assertion can put a live credential in a traceback. The adapters
    read one attribute each, so a namespace is enough, and every Salesforce
    field is given explicitly rather than defaulted from the environment.
    """
    return SimpleNamespace(
        salesforce=SalesforceConfig(
            access_token="t" if configured else "",
            instance_url="https://x" if configured else "",
            token_url="", client_id="", client_secret="",
        ),
    )


@pytest.fixture(autouse=True)
def no_developer_credentials(monkeypatch):
    """Keep whoever is running the tests out of the results.

    `quorum.config` calls `load_dotenv()` at import, so a populated `.env` at the
    repo root configures the CRM legs these tests need unconfigured. The weekly
    run then reports `NO` where it should report `not checked` — the exact
    conflation this suite exists to catch, and it fails on a developer machine
    while passing in a clean checkout.

    Deleting is safe because `load_dotenv()` runs once at import, before any
    fixture, so nothing repopulates these. QUORUM_TEST_DSN is deliberately left
    alone — it is how the database tests are switched on.
    """
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    # Enrichment providers, asked for their own variable names rather than
    # listed here — so a provider added later is cleared too, and this file
    # never has to name one. With none set, a weekly run makes no provider call.
    from quorum import enrich

    for name in enrich.env_vars():
        monkeypatch.delenv(name, raising=False)
    for name in [k for k in os.environ if k.startswith(CREDENTIAL_PREFIXES)]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    dsn = os.environ.get("QUORUM_TEST_DSN")
    if not dsn:
        pytest.skip("QUORUM_TEST_DSN not set")
    return dsn


@pytest.fixture
def database(admin_dsn: str):
    """A fresh database with the migrations applied, dropped afterwards."""
    name = f"quorum_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')

    dsn = _swap_database(admin_dsn, name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for path in MIGRATIONS:
                conn.execute(path.read_text())
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = %s and pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'drop database if exists "{name}"')


def _swap_database(dsn: str, name: str) -> str:
    """Point a DSN at a different database, in whatever form it was given —
    URL, key=value, with or without a query string."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    return make_conninfo(**params)


class FakeGong:
    """Stands in for GongClient with the same two methods the importer calls."""

    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    def iter_call_ids(self, from_date: str, to_date: str):
        for call in self._calls:
            yield call["metaData"]["id"]

    def get_calls_extensive(self, call_ids: list[str]) -> dict:
        wanted = set(call_ids)
        return {
            "calls": [c for c in self._calls if c["metaData"]["id"] in wanted]
        }


def sample_calls() -> list[dict]:
    """One ordinary call, one group call, one historical call, and the awkward
    parties: an internal employee, a personal address, a party with a name and
    no email, and a party with neither (a meeting bot)."""
    trainees = [
        {"id": f"t{i}", "name": f"Trainee {i}", "emailAddress": f"trainee{i}@acme.com",
         "affiliation": "External"}
        for i in range(1, 10)
    ]
    return [
        {
            "metaData": {
                "id": "call-1",
                "title": "Acme <> Us",
                "started": "2026-08-18T14:00:00Z",
                "duration": 1800,
            },
            "parties": [
                {"id": "p1", "name": "Dana Reyes", "emailAddress": "Dana.Reyes@acme.com",
                 "affiliation": "External"},
                {"id": "p2", "name": "Sam Rivera", "emailAddress": "sam@northwind.com",
                 "affiliation": "Internal"},
                {"id": "p3", "name": "Unknown Speaker"},
                {"id": "p4", "emailAddress": "support@acme.com", "affiliation": "External"},
                {"id": "p5", "name": "Sam Fox", "emailAddress": "sam@gmail.com"},
                {"id": "p6"},  # neither name nor email — not a person
            ],
        },
        {
            "metaData": {
                "id": "call-2",
                "title": "Team Training",
                "started": "2026-08-19T10:00:00Z",
                "duration": 3600,
            },
            "parties": trainees + [
                {"id": "p2", "name": "Sam Rivera", "emailAddress": "sam@northwind.com",
                 "affiliation": "Internal"},
            ],
        },
        {
            "metaData": {
                "id": "call-3",
                "title": "Acme kickoff",
                "started": "2025-10-21T09:00:00Z",
                "duration": 2400,
            },
            "parties": [
                # The same human under a second address — the case
                # person_identifiers exists for.
                {"id": "p1", "name": "Dana Reyes", "emailAddress": "d.reyes@acme.com",
                 "affiliation": "External"},
            ],
        },
    ]


@pytest.fixture
def gong_calls() -> list[dict]:
    return sample_calls()
