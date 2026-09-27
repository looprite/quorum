"""Every environment value the pipeline reads, in one place.

Nothing organisation-specific lives in this repository. Differentiation is
configuration: an account row, or one of the values below. Secrets are read
from the environment — a git-ignored .env locally, your own secret store in
production — and are never committed.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

try:  # dotenv is a local convenience, not a dependency of a deployed run
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    # A deployed run reads its environment's secret store, has no .env, and
    # should say nothing. The other case is a reader who followed the setup
    # guide, wrote a .env, and installed without the `dev` extra: their file is
    # about to be ignored in silence, and the only symptom is a later
    # "Missing environment: ..." naming variables that are in fact set
    # correctly in the file. Say it here instead, where it is still actionable.
    #
    # The file is never opened — its existence is the whole signal, and no
    # value from it may reach the terminal.
    if os.path.isfile(".env"):
        print(
            "[!] python-dotenv is not installed, so the .env file in this "
            "directory is being ignored.\n"
            "    Values must come from the environment until you install it: "
            "pip install -e '.[dev]'",
            file=sys.stderr,
        )


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class GongConfig:
    """Credentials for the meeting source. Read from the environment, never
    stored as database columns (see migrations/0001_core.sql)."""

    access_key: str = field(default_factory=lambda: os.environ.get("GONG_ACCESS_KEY", ""))
    access_key_secret: str = field(
        default_factory=lambda: os.environ.get("GONG_ACCESS_KEY_SECRET", "")
    )
    base_url: str = field(
        default_factory=lambda: os.environ.get("GONG_BASE_URL", "https://api.gong.io")
    )

    @property
    def configured(self) -> bool:
        return bool(self.access_key and self.access_key_secret)


@dataclass(frozen=True)
class SalesforceConfig:
    """Two ways in.

    Deployed runs use client credentials — no paste, no two-hour expiry. The
    pasted token is the shortcut for trying it out by hand, and stays supported
    because it is the only way in before an External Client App is set up.
    """

    access_token: str = field(default_factory=lambda: os.environ.get("SF_ACCESS_TOKEN", ""))
    instance_url: str = field(default_factory=lambda: os.environ.get("SF_INSTANCE_URL", ""))
    token_url: str = field(default_factory=lambda: os.environ.get("SF_TOKEN_URL", ""))
    client_id: str = field(default_factory=lambda: os.environ.get("SF_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.environ.get("SF_CLIENT_SECRET", ""))
    api_version: str = field(default_factory=lambda: os.environ.get("SF_API_VERSION", "v61.0"))

    @property
    def configured(self) -> bool:
        return bool(
            (self.access_token and self.instance_url)
            or (self.token_url and self.client_id and self.client_secret)
        )

    @property
    def uses_client_credentials(self) -> bool:
        return not (self.access_token and self.instance_url) and bool(self.token_url)


@dataclass(frozen=True)
class HubSpotConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("HUBSPOT_SERVICE_KEY", ""))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class Config:
    # Your Postgres, holding the meeting and attendee data. Schema: migrations/.
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    # Matches accounts.name. One account per deployment; the column exists so the
    # same queries ship everywhere.
    account: str = field(default_factory=lambda: os.environ.get("ACCOUNT_DOMAIN", ""))

    gong: GongConfig = field(default_factory=GongConfig)
    salesforce: SalesforceConfig = field(default_factory=SalesforceConfig)
    hubspot: HubSpotConfig = field(default_factory=HubSpotConfig)

    # Monday of the target week, in the tz below. Unset means the current week.
    week_start: Optional[str] = field(default_factory=lambda: os.environ.get("WEEK_START"))
    tz_offset: str = field(default_factory=lambda: os.environ.get("TZ_OFFSET", "-04"))

    # People per company on the stakeholder list. The cap is a feature.
    shortlist_size: int = field(default_factory=lambda: _int("SHORTLIST_SIZE", 3))
    # Above this many external attendees a meeting is a group call — attending
    # one is not a relationship. Stated on the row, not judged in code.
    group_call_min: int = field(default_factory=lambda: _int("GROUP_CALL_MIN", 8))
    # How far back still counts as recent contact — and, with no dates given,
    # how far back `quorum import` reaches. One number, deliberately: importing
    # less than the recency window answers 'Recent contact?' with 'no' for
    # people who were met inside it.
    recent_days: int = field(default_factory=lambda: _int("RECENT_DAYS", 90))

    # Substrings of Account.Type that mark an existing customer. Empty = the gate
    # is off and ICP fit is the firmographic attributes only. Leave it off
    # unless Type is genuinely maintained as a lifecycle field.
    customer_account_types: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip().lower()
            for t in os.environ.get("CUSTOMER_ACCOUNT_TYPES", "").split(",")
            if t.strip()
        )
    )

    output_dir: str = field(default_factory=lambda: os.environ.get("OUTPUT_DIR", "output"))

    # Off by default — a deployment that has not chosen retention must not
    # silently start storing contact data. On, quorum/weekly/run.py stores the
    # week's three emitted files into run_outputs (migrations/0005, applied
    # separately — see docs/setup.md §14). Read by nothing else in the
    # pipeline.
    retain_runs: bool = field(default_factory=lambda: _bool("RETAIN_RUNS", False))

    def week_bounds(self) -> tuple[str, str]:
        """(start, end) of the target week as timestamps in the configured tz."""
        if self.week_start:
            start = dt.date.fromisoformat(self.week_start)
        else:
            today = dt.date.today()
            start = today - dt.timedelta(days=today.weekday())  # Monday
        end = start + dt.timedelta(days=7)
        return (
            f"{start.isoformat()} 00:00:00{self.tz_offset}",
            f"{end.isoformat()} 00:00:00{self.tz_offset}",
        )

    def week_days_remaining(self) -> float:
        """Days of the target week still in the future. Zero or less means it
        has fully elapsed.

        The default week is the one containing today, so a run reports on a
        window that has not closed unless it is run late in that week or
        `WEEK_START` names an earlier one. Mid-week by hand that is fine and
        sometimes what you want. On a schedule it is the failure this exists to
        catch: a job set for Monday morning reports a window a few hours old,
        produces an artifact with headers and no rows, and exits zero. Nothing
        else in the run has any way to notice.
        """
        _, end = self.week_bounds()
        end_dt = dt.datetime.fromisoformat(end)
        return (end_dt - dt.datetime.now(end_dt.tzinfo)).total_seconds() / 86400

    def missing(self) -> list[str]:
        """Env vars without which a weekly run cannot start at all."""
        gaps = []
        if not self.database_url:
            gaps.append("DATABASE_URL")
        if not self.account:
            gaps.append("ACCOUNT_DOMAIN")
        return gaps
