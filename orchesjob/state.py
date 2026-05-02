# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""State management for orchesjob using SQLite."""

import json
import os
import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def get_home() -> pathlib.Path:
    home = os.environ.get("ORCHESJOB_HOME", "/var/lib/orchesjob")
    return pathlib.Path(home)


def new_job_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


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


def init_db(home: pathlib.Path) -> None:
    """Create directory structure and initialize the SQLite schema."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "logs").mkdir(exist_ok=True, mode=0o700)

    conn = db_connect(home)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id       TEXT PRIMARY KEY,
                run_key      TEXT,
                worker_pid   INTEGER,
                target_pid   INTEGER,
                command      TEXT NOT NULL,
                status       TEXT NOT NULL,
                exit_code    INTEGER,
                stdout_file  TEXT,
                stderr_file  TEXT,
                started_at   TEXT,
                finished_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS run_keys (
                run_key  TEXT PRIMARY KEY,
                job_id   TEXT NOT NULL REFERENCES jobs(job_id),
                created_at TEXT NOT NULL
            );
        """)
        os.chmod(str(get_db_path(home)), 0o600)
    finally:
        conn.close()


def row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if isinstance(d.get("command"), str):
        d["command"] = json.loads(d["command"])
    return d


def stdout_path(home: pathlib.Path, job_id: str) -> pathlib.Path:
    return home / "logs" / f"{job_id}.stdout"


def stderr_path(home: pathlib.Path, job_id: str) -> pathlib.Path:
    return home / "logs" / f"{job_id}.stderr"
