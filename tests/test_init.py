"""`quorum init`, and the run that now refuses to start without what it writes.

Two things are being pinned. First, that init writes exactly what the pipeline
reads — same database, same queries, no fixture in between. Second, that a
deployment missing a focus profile fails instead of producing a normal-looking
artifact in which every company is an ICP target.

No network: the account and the profile are the only things init touches.
"""

from __future__ import annotations

import psycopg
import pytest

from quorum import bootstrap, db
from quorum.cli import main
from quorum.config import Config
from quorum.weekly.run import MissingFocusProfile, run_weekly

from tests.test_import_and_weekly import ACCOUNT, _cfg, _import

PROFILE = dict(
    employee_min=200,
    employee_max=10000,
    geographies=["North America"],
    seniority=["c-level", "vp", "director"],
)


def _init(dsn: str, *, replace: bool = False, **overrides) -> bootstrap.InitResult:
    kwargs = {**PROFILE, **overrides}
    domains = kwargs.pop("internal_domains", ["northwind.com"])
    cfg = Config(database_url=dsn, account=ACCOUNT)
    with psycopg.connect(dsn) as conn:
        result = bootstrap.init_deployment(
            conn, cfg, domains, bootstrap.build_profile(**kwargs), replace=replace
        )
        conn.commit()
    return result


# --- What init writes is what the pipeline reads --------------------------- #


def test_init_writes_what_the_pipeline_reads(database):
    """The point of the command: after it, the three reads that a fresh
    deployment used to fail on all return something."""
    result = _init(database)

    assert result.account == "created"
    assert result.profile == "created"
    assert result.profile_version == 1
    assert result.warnings == []

    cfg = Config(database_url=database, account=ACCOUNT)
    with psycopg.connect(database) as conn:
        assert db.account_id(conn, cfg) == result.account_id
        assert db.internal_domains(conn, cfg) == ["northwind.com"]
        assert db.focus_profile(conn, cfg) == {
            "employee_count_min": 200,
            "employee_count_max": 10000,
            # Stored level-aware, canonicalised. A bare string still reads as a
            # region, which is what older profiles hold.
            "hq_geographies": [{"level": "region", "value": "north america"}],
            "focus_seniority": ["c-level", "vp", "director"],
        }


def test_init_then_import_then_weekly(database, gong_calls, tmp_path):
    """The deployment path end to end, with nothing hand-seeded: init, import,
    artifact. This is the sequence that could not be run before."""
    result = _init(database)
    _import(database, result.account_id, gong_calls)

    paths = run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    assert paths["xlsx"]


def test_re_running_init_unchanged_writes_nothing(database):
    _init(database)
    again = _init(database)

    assert (again.account, again.profile) == ("unchanged", "unchanged")
    assert again.profile_version == 1
    with psycopg.connect(database) as conn:
        assert _profile_rows(conn) == [(1, True)]


def test_a_changed_profile_without_replace_is_refused(database):
    """The schema allows one active row, and which one won would decide the ICP
    test — so a second, different profile is a decision, not a re-run."""
    _init(database)

    with pytest.raises(bootstrap.InitError, match="already exists"):
        _init(database, employee_min=1, employee_max=5)

    cfg = Config(database_url=database, account=ACCOUNT)
    with psycopg.connect(database) as conn:
        assert db.focus_profile(conn, cfg)["employee_count_min"] == 200
        assert _profile_rows(conn) == [(1, True)]


def test_replace_supersedes_and_keeps_the_old_version(database):
    """A profile change alters which companies appear on the map, so the run
    that produced last week's artifact stays reconstructable."""
    _init(database)
    result = _init(database, replace=True, employee_min=50, employee_max=500)

    assert (result.profile, result.profile_version) == ("replaced", 2)

    cfg = Config(database_url=database, account=ACCOUNT)
    with psycopg.connect(database) as conn:
        assert db.focus_profile(conn, cfg)["employee_count_max"] == 500
        assert _profile_rows(conn) == [(1, False), (2, True)]


def test_adding_a_domain_does_not_version_the_profile(database):
    """A domain the account acquired is not a change of ICP, so it needs no
    --replace and leaves the profile at the version that produced last week."""
    first = _init(database)
    result = _init(database, internal_domains=["northwind.com", "northwind.io"])

    assert (result.account, result.profile) == ("updated", "unchanged")
    assert result.account_id == first.account_id

    cfg = Config(database_url=database, account=ACCOUNT)
    with psycopg.connect(database) as conn:
        assert db.internal_domains(conn, cfg) == ["northwind.com", "northwind.io"]
        assert _profile_rows(conn) == [(1, True)]


def _profile_rows(conn) -> list[tuple]:
    return conn.execute(
        "select version_number, is_active from user_focus_profiles "
        "order by version_number"
    ).fetchall()


# --- The weekly run will not start without a profile ----------------------- #


def test_weekly_refuses_without_a_focus_profile(database, gong_calls, tmp_path):
    """The silent failure this replaces: the run used to log a warning, pass
    every company through the ICP test and write a workbook that looks normal."""
    account_id = _seed_account_only(database)
    _import(database, account_id, gong_calls)

    with pytest.raises(MissingFocusProfile, match="quorum init"):
        run_weekly(_cfg(database, tmp_path), log=lambda *_: None)

    # Nothing half-written, and nothing to mistake for a real artifact.
    assert list(tmp_path.iterdir()) == []


def test_an_empty_profile_counts_as_missing(database, gong_calls, tmp_path):
    """A row whose profile_data is `{}` constrains nothing, so it is the same
    failure wearing a row."""
    account_id = _seed_account_only(database)
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(
            "insert into user_focus_profiles (account_id, version_number, "
            "is_active, profile_data) values (%s, 1, true, '{}'::jsonb)",
            (account_id,),
        )

    with pytest.raises(MissingFocusProfile):
        run_weekly(_cfg(database, tmp_path), log=lambda *_: None)


def _seed_account_only(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "insert into accounts (name, internal_domains) values (%s, %s) returning id",
            (ACCOUNT, ["northwind.com"]),
        ).fetchone()
    return str(row[0])


# --- Validation, without a database ---------------------------------------- #


def test_domains_are_normalised():
    assert bootstrap.normalise_domains(
        ["@Acme.com ", "acme.io", "ACME.COM"]
    ) == ["acme.com", "acme.io"]


@pytest.mark.parametrize("bad", ["sam@acme.com", "https://acme.com/x", "acme"])
def test_an_address_or_a_url_is_not_a_domain(bad):
    """Accepting one would classify every colleague as an external attendee —
    the entire company on tab 1, every week, looking like customer contacts."""
    with pytest.raises(bootstrap.InitError):
        bootstrap.normalise_domains([bad])


def test_no_domains_at_all_is_refused():
    with pytest.raises(bootstrap.InitError, match="classified external"):
        bootstrap.normalise_domains([" ", ""])


@pytest.mark.parametrize(
    "overrides, message",
    [
        (dict(employee_min=5000, employee_max=100), "no company can match"),
        (dict(employee_min=-1), "negative"),
        (dict(geographies=[]), "--geographies"),
        (dict(seniority=[""]), "--seniority"),
    ],
)
def test_a_profile_that_cannot_work_is_refused(overrides, message):
    with pytest.raises(bootstrap.InitError, match=message):
        bootstrap.build_profile(**{**PROFILE, **overrides})


def test_a_good_profile_warns_about_nothing():
    assert bootstrap.profile_warnings(bootstrap.build_profile(**PROFILE)) == []


def test_an_unknown_seniority_level_is_called_out():
    """`seniority_terms` falls back to matching the level verbatim against the
    Salesforce Title, so a typo returns an empty bench rather than an error."""
    profile = bootstrap.build_profile(**{**PROFILE, "seniority": ["vpp"]})

    assert "vpp" in bootstrap.profile_warnings(profile)[0]


def test_a_region_the_icp_test_knows_is_accepted():
    """EMEA used to warn and then apply no geography filter at all."""
    profile = bootstrap.build_profile(**{**PROFILE, "geographies": ["EMEA"]})

    assert profile["hq_geographies"] == [{"level": "region", "value": "emea"}]
    assert bootstrap.profile_warnings(profile) == []


@pytest.mark.parametrize(
    "geographies, message",
    [
        (["Atlantis"], "Unknown region"),
        ([{"level": "country", "value": "Freedonia"}], "Unknown country"),
        ([{"level": "state", "value": "California"}], "not one of"),
    ],
)
def test_a_geography_the_icp_test_cannot_act_on_is_refused(geographies, message):
    """Refused, not warned: a profile naming a region the test did not know
    applied no geography filter at all, and every company met passed."""
    with pytest.raises(bootstrap.InitError, match=message):
        bootstrap.build_profile(**{**PROFILE, "geographies": geographies})


# --- Through the command line ---------------------------------------------- #


def test_cli_init_reports_and_then_refuses_to_repeat(database, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("ACCOUNT_DOMAIN", ACCOUNT)
    argv = [
        "init",
        "--internal-domains", "northwind.com,northwind.io",
        "--employee-min", "200",
        "--employee-max", "10000",
        "--geographies", "North America",
        "--seniority", "c-level,vp,director",
    ]

    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "Account northwind.com created" in out
    assert "Focus profile v1 created" in out
    assert "quorum import" in out          # the next step, not just the failure

    # The same command again is a no-op, not a second profile.
    assert main(argv) == 0
    assert "Focus profile v1 unchanged" in capsys.readouterr().out

    # A different band is a decision, and needs saying so.
    narrower = list(argv)
    narrower[narrower.index("--employee-min") + 1] = "50"
    assert main(narrower) == 2
    assert "--replace" in capsys.readouterr().err
