"""Storing a weekly run's three emitted files, when RETAIN_RUNS is on.

docs/setup.md §14. Nothing in the pipeline reads run_outputs back — this is
the write path only, and it runs only when the deployment has opted in
(quorum/config.py).

run_weekly's own `with db.connect(...)` block (quorum/weekly/run.py) closes
before the files are written, so this opens a connection of its own.
"""

from __future__ import annotations

import psycopg

from ..config import Config

FILE_TYPES = ("xlsx", "json", "html")

_INSERT_SQL = """
insert into run_outputs (run_date, file_type, content)
values (%(run_date)s, %(file_type)s, %(content)s);
"""


def store(cfg: Config, run_date: str, paths: dict[str, str]) -> None:
    """Insert all three files for one run in a single transaction.

    A partial failure — a file missing, a connection dropped partway through —
    must leave no rows behind, not one file and the appearance of a stored
    run. psycopg's connection context manager gives us that for free: it
    commits on a clean exit and rolls back on any exception raised inside it.
    """
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            for file_type in FILE_TYPES:
                with open(paths[file_type], "rb") as f:
                    content = f.read()
                cur.execute(
                    _INSERT_SQL,
                    {"run_date": run_date, "file_type": file_type, "content": content},
                )
