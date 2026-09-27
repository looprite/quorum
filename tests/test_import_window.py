"""The import range, and what the import reports about the run.

`quorum import` with no arguments had to be told a date range, so the
deployment path asked whoever was deploying for a decision they had no basis
for. The default is now derived from RECENT_DAYS — the same setting that
decides whether 'Recent contact?' reads yes or no — so the two cannot drift.

No network anywhere: the window is a pure function, the elapsed report is
formatting, and the one end-to-end check stubs Gong out entirely.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quorum.cli import import_window
from quorum.config import Config
from quorum.gong.importer import ImportResult, format_elapsed

TODAY = dt.date(2026, 8, 25)


def _cfg(recent_days: int = 90) -> Config:
    return Config(database_url="postgresql:///x", account="northwind.com",
                  recent_days=recent_days)


def test_no_arguments_imports_the_last_recent_days():
    assert import_window(_cfg(), None, None, today=TODAY) == (
        "2026-05-27",
        "2026-08-25",
    )


def test_the_window_follows_recent_days_rather_than_a_constant():
    """The point of deriving it: raise RECENT_DAYS and the import that feeds
    'Recent contact?' covers the same ground, with nothing else to change."""
    assert import_window(_cfg(365), None, None, today=TODAY)[0] == "2025-08-25"
    assert import_window(_cfg(7), None, None, today=TODAY)[0] == "2026-08-18"


def test_the_default_window_covers_every_date_recent_contact_calls_recent():
    """The two windows are the same window. A meeting on the oldest day
    `recent_contact` still counts as recent must be inside the import range,
    or the artifact says 'no' about someone who was met."""
    from quorum.weekly.stakeholders import recent_contact

    cfg = _cfg()
    from_date, to_date = import_window(cfg, None, None, today=dt.date.today())
    cutoff = dt.date.today() - dt.timedelta(days=cfg.recent_days)

    assert dt.date.fromisoformat(from_date) <= cutoff
    assert dt.date.fromisoformat(to_date) >= dt.date.today()
    # And that cutoff date really is the boundary of a 'yes'.
    assert recent_contact(cfg, {"last_met": cutoff}, None).startswith("yes")


def test_explicit_dates_are_unchanged():
    assert import_window(_cfg(), "2025-10-01", "2026-08-24", today=TODAY) == (
        "2025-10-01",
        "2026-08-24",
    )


def test_yesterday_is_unchanged():
    assert import_window(_cfg(), None, None, yesterday=True, today=TODAY) == (
        "2026-08-24",
        "2026-08-24",
    )


def test_half_a_range_is_refused_rather_than_silently_defaulted():
    """--from alone used to be an error and still is: falling back to the
    default window would import a range nobody asked for."""
    with pytest.raises(ValueError, match="both --from and --to"):
        import_window(_cfg(), "2025-10-01", None, today=TODAY)


def test_yesterday_with_a_range_is_refused():
    with pytest.raises(ValueError, match="cannot be combined"):
        import_window(_cfg(), "2025-10-01", "2026-08-24", yesterday=True, today=TODAY)


# --- What the run reports --------------------------------------------------- #


@pytest.mark.parametrize(
    "seconds, expected",
    [(0, "0s"), (9.6, "10s"), (60, "1m 00s"), (192.4, "3m 12s"),
     (3852.9, "1h 04m 13s")],
)
def test_elapsed_is_readable_at_every_scale(seconds, expected):
    assert format_elapsed(seconds) == expected


def test_an_empty_range_is_still_timed(monkeypatch):
    """The early return is the path most likely to lose the measurement, and
    it is the one that reaches no database — hence no connection here."""
    from tests.conftest import FakeGong

    from quorum.gong import importer

    ticks = iter([100.0, 142.0])
    monkeypatch.setattr(importer.time, "monotonic", lambda: next(ticks))

    result = importer.import_range(
        None, FakeGong([]), "account", [], "2026-01-01", "2026-01-02",
        log=lambda *_: None,
    )

    assert result.calls_seen == 0
    assert "42s elapsed" in str(result)


def test_the_counts_line_carries_the_time_it_took():
    """Someone deciding whether to backfill a year extrapolates from the run
    they just did, so the calls seen and the time taken are on one line."""
    line = str(ImportResult(calls_seen=643, meetings_upserted=640,
                            elapsed_seconds=192.4))

    assert "643 calls in range" in line
    assert "3m 12s elapsed" in line


# --- Through the command line ----------------------------------------------- #


def test_cli_import_with_no_arguments_uses_the_recent_window(
    database, monkeypatch, capsys
):
    """What the deployment path actually runs. Gong is stubbed: this is about
    which range reaches the importer, not about fetching anything."""
    import psycopg

    from quorum import cli
    from quorum.gong.importer import ImportResult

    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(
            "insert into accounts (name, internal_domains) values (%s, %s)",
            ("northwind.com", ["northwind.com"]),
        )

    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("ACCOUNT_DOMAIN", "northwind.com")
    monkeypatch.setenv("RECENT_DAYS", "90")
    monkeypatch.setenv("GONG_ACCESS_KEY", "k")
    monkeypatch.setenv("GONG_ACCESS_KEY_SECRET", "s")

    seen: dict = {}
    monkeypatch.setattr(cli, "GongClient", lambda *a, **k: object())

    def fake_import_range(conn, client, account_id, internal, from_date, to_date, **kw):
        seen.update(from_date=from_date, to_date=to_date, internal=internal)
        return ImportResult(calls_seen=0, elapsed_seconds=1.0)

    monkeypatch.setattr(cli, "import_range", fake_import_range)

    assert cli.main(["import"]) == 0

    today = dt.date.today()
    assert seen["to_date"] == today.isoformat()
    assert seen["from_date"] == (today - dt.timedelta(days=90)).isoformat()

    out = capsys.readouterr().out
    # The window it chose and why, so it is never a mystery which range ran.
    assert "last 90 days (RECENT_DAYS)" in out
    assert "1s elapsed" in out
