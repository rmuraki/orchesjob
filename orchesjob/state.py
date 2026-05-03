# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""State management for orchesjob using SQLite."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


SCHEMA_VERSION = 2


TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "LOST", "CANCELLED", "ABORTED"}
ACTIVE_STATUSES = {"STARTING", "RUNNING"}


def get_home() -> pathlib.Path:
    home = os.environ.get("ORCHESJOB_HOME", "/var/lib/orchesjob")
    return pathlib.Path(home)


def new_job_id() -> str:
    return str(uuid.uuid4())


def now_ts() -> int:
    """Return current UTC Unix timestamp in seconds."""
    return int(datetime.now(timezone.utc).timestamp())


def now_iso() -> str:
    """Compatibility helper. Prefer now_ts() for DB state."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def timestamp_to_iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().isoformat()


def parse_datetime_to_ts(value: str) -> int:
    """Parse an ISO-8601-like datetime into UTC Unix timestamp seconds.

    Naive datetimes are interpreted in the local timezone, matching Python's
    datetime.astimezone() behavior.
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "Use ISO 8601 format, e.g. 2026-01-01 or 2026-01-01T12:00:00+09:00"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.astimezone(timezone.utc).timestamp())


def _coerce_ts(value: Any) -> Optional[int]:
    """Coerce legacy TEXT timestamps or INTEGER timestamps to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.astimezone(timezone.utc).timestamp())
        except ValueError:
            return None
    return None


def get_db_path(home: pathlib.Path) -> pathlib.Path:
    return home / "orchesjob.db"


def db_connect(home: pathlib.Path) -> sqlite3.Connection:
    db_path = get_db_path(home)
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> Dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")




def _column_type(conn: sqlite3.Connection, table: str, column: str) -> str:
    info = _table_columns(conn, table).get(column)
    if info is None:
        return ""
    return str(info["type"] or "").upper()


def _needs_table_rebuild_for_integer_timestamps(conn: sqlite3.Connection) -> bool:
    """Return True if legacy TEXT timestamp columns need physical rebuild.

    SQLite keeps column affinity. Updating a TEXT column with integer bind values
    can still return strings such as '1'. Rebuild the table so timestamp columns
    have INTEGER affinity after migration.
    """
    checks = [
        ("jobs", "started_at"),
        ("jobs", "finished_at"),
        ("jobs", "updated_at"),
        ("jobs", "aborted_at"),
        ("run_keys", "created_at"),
    ]
    for table, column in checks:
        cols = _table_columns(conn, table)
        if column in cols and "INT" not in str(cols[column]["type"] or "").upper():
            return True
    return False


def _rebuild_tables_with_integer_timestamps(conn: sqlite3.Connection) -> None:
    """Physically rebuild legacy tables so timestamp columns have INTEGER affinity."""
    old_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
        conn.execute("ALTER TABLE run_keys RENAME TO run_keys_old")

        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id          TEXT PRIMARY KEY,
                run_key         TEXT,
                worker_pid      INTEGER,
                target_pid      INTEGER,
                command         TEXT NOT NULL,
                status          TEXT NOT NULL,
                exit_code       INTEGER,
                stdout_file     TEXT,
                stderr_file     TEXT,
                started_at      INTEGER,
                finished_at     INTEGER,
                updated_at      INTEGER,
                aborted_at      INTEGER,
                abort_reason    TEXT,
                rerun_of_job_id TEXT,
                attempt_no      INTEGER NOT NULL DEFAULT 1,
                rerun_reason    TEXT
            );
            CREATE TABLE run_keys (
                run_key    TEXT PRIMARY KEY,
                job_id     TEXT NOT NULL REFERENCES jobs(job_id),
                created_at INTEGER NOT NULL
            );
            """
        )

        old_job_cols = _table_columns(conn, "jobs_old")
        def old_expr(name: str, default: str = "NULL") -> str:
            return name if name in old_job_cols else f"{default} AS {name}"

        conn.execute(
            f"""
            INSERT INTO jobs (
                job_id, run_key, worker_pid, target_pid, command, status,
                exit_code, stdout_file, stderr_file, started_at, finished_at,
                updated_at, aborted_at, abort_reason, rerun_of_job_id,
                attempt_no, rerun_reason
            )
            SELECT
                job_id,
                run_key,
                {old_expr('worker_pid')},
                {old_expr('target_pid')},
                command,
                status,
                {old_expr('exit_code')},
                {old_expr('stdout_file')},
                {old_expr('stderr_file')},
                started_at,
                finished_at,
                {old_expr('updated_at')},
                {old_expr('aborted_at')},
                {old_expr('abort_reason')},
                {old_expr('rerun_of_job_id')},
                {old_expr('attempt_no', '1')},
                {old_expr('rerun_reason')}
            FROM jobs_old
            """
        )

        conn.execute(
            """
            INSERT INTO run_keys (run_key, job_id, created_at)
            SELECT run_key, job_id, created_at FROM run_keys_old
            """
        )

        conn.execute("DROP TABLE jobs_old")
        conn.execute("DROP TABLE run_keys_old")
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if old_fk else 'OFF'}")


def _migrate_legacy_timestamps(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT job_id, started_at, finished_at, updated_at, aborted_at FROM jobs"
    ).fetchall():
        started_at = _coerce_ts(row["started_at"])
        finished_at = _coerce_ts(row["finished_at"])
        updated_at = _coerce_ts(row["updated_at"]) or finished_at or started_at
        aborted_at = _coerce_ts(row["aborted_at"])
        conn.execute(
            """UPDATE jobs
               SET started_at = ?, finished_at = ?, updated_at = ?, aborted_at = ?
               WHERE job_id = ?""",
            (started_at, finished_at, updated_at, aborted_at, row["job_id"]),
        )

    for row in conn.execute("SELECT run_key, created_at FROM run_keys").fetchall():
        conn.execute(
            "UPDATE run_keys SET created_at = ? WHERE run_key = ?",
            (_coerce_ts(row["created_at"]) or now_ts(), row["run_key"]),
        )


def init_db(home: pathlib.Path) -> None:
    """Create directory structure and initialize/migrate the SQLite schema."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "logs").mkdir(exist_ok=True, mode=0o700)

    conn = db_connect(home)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id          TEXT PRIMARY KEY,
                run_key         TEXT,
                worker_pid      INTEGER,
                target_pid      INTEGER,
                command         TEXT NOT NULL,
                status          TEXT NOT NULL,
                exit_code       INTEGER,
                stdout_file     TEXT,
                stderr_file     TEXT,
                started_at      INTEGER,
                finished_at     INTEGER,
                updated_at      INTEGER,
                aborted_at      INTEGER,
                abort_reason    TEXT,
                rerun_of_job_id TEXT,
                attempt_no      INTEGER NOT NULL DEFAULT 1,
                rerun_reason    TEXT
            );
            CREATE TABLE IF NOT EXISTS run_keys (
                run_key    TEXT PRIMARY KEY,
                job_id     TEXT NOT NULL REFERENCES jobs(job_id),
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strict_overrides (
                run_key     TEXT PRIMARY KEY,
                allowed_at  INTEGER NOT NULL,
                used_at     INTEGER,
                reason      TEXT,
                expires_at  INTEGER
            );
            CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Lightweight additive migrations for databases created by older versions.
        _ensure_column(conn, "jobs", "updated_at", "INTEGER")
        _ensure_column(conn, "jobs", "aborted_at", "INTEGER")
        _ensure_column(conn, "jobs", "abort_reason", "TEXT")
        _ensure_column(conn, "jobs", "rerun_of_job_id", "TEXT")
        _ensure_column(conn, "jobs", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "jobs", "rerun_reason", "TEXT")
        if _needs_table_rebuild_for_integer_timestamps(conn):
            _rebuild_tables_with_integer_timestamps(conn)
        _migrate_legacy_timestamps(conn)

        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        os.chmod(str(get_db_path(home)), 0o600)
    finally:
        conn.close()


def row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("command"), str):
        d["command"] = json.loads(d["command"])
    for key in ("started_at", "finished_at", "updated_at", "aborted_at"):
        d[key] = _coerce_ts(d.get(key))
    return d


def stdout_path(home: pathlib.Path, job_id: str) -> pathlib.Path:
    return home / "logs" / f"{job_id}.stdout"


def stderr_path(home: pathlib.Path, job_id: str) -> pathlib.Path:
    return home / "logs" / f"{job_id}.stderr"
