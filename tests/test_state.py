# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Unit tests for orchesjob.state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from orchesjob import state as st


def test_init_db_creates_log_dir(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    assert (home / "logs").is_dir()


def test_init_db_creates_db_file(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    assert st.get_db_path(home).exists()


def test_init_db_creates_tables(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert {"jobs", "run_keys", "strict_overrides", "schema_meta"} <= tables


def test_jobs_schema_has_new_columns(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()
    assert {
        "updated_at",
        "aborted_at",
        "abort_reason",
        "rerun_of_job_id",
        "attempt_no",
        "rerun_reason",
    } <= columns


def test_db_connect_wal_mode(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal"


def test_now_ts_returns_epoch_seconds():
    now = st.now_ts()
    assert isinstance(now, int)
    assert now > 1_700_000_000


def test_timestamp_to_iso_is_timezone_aware():
    iso = st.timestamp_to_iso(0)
    assert iso is not None
    dt = datetime.fromisoformat(iso)
    assert dt.tzinfo is not None


def test_parse_datetime_to_ts_accepts_aware_iso():
    assert st.parse_datetime_to_ts("1970-01-01T09:00:00+09:00") == 0


def test_row_to_job_deserialises_command_and_coerces_timestamps(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        job_id = st.new_job_id()
        conn.execute(
            """INSERT INTO jobs
               (job_id, run_key, command, status, stdout_file, stderr_file, started_at)
               VALUES (?, 'key', ?, 'STARTING', '', '', ?)""",
            (job_id, json.dumps(["echo", "hi"]), "1970-01-01T00:00:01+00:00"),
        )
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        job = st.row_to_job(row)
    finally:
        conn.close()
    assert job["command"] == ["echo", "hi"]
    assert job["started_at"] == 1


def test_init_db_migrates_legacy_iso_timestamps(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sqlite_path = st.get_db_path(home)
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.executescript(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                run_key TEXT,
                worker_pid INTEGER,
                target_pid INTEGER,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                stdout_file TEXT,
                stderr_file TEXT,
                started_at TEXT,
                finished_at TEXT
            );
            CREATE TABLE run_keys (
                run_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """INSERT INTO jobs
               (job_id, run_key, command, status, started_at, finished_at)
               VALUES ('job1', 'key1', ?, 'SUCCEEDED', ?, ?)""",
            (json.dumps(["true"]), "1970-01-01T00:00:01+00:00", "1970-01-01T00:00:02+00:00"),
        )
        conn.execute("INSERT INTO run_keys (run_key, job_id, created_at) VALUES ('key1', 'job1', ?)", ("1970-01-01T00:00:01+00:00",))
        conn.commit()
    finally:
        conn.close()

    st.init_db(home)
    conn = st.db_connect(home)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = 'job1'").fetchone()
        rk = conn.execute("SELECT * FROM run_keys WHERE run_key = 'key1'").fetchone()
    finally:
        conn.close()
    assert row["started_at"] == 1
    assert row["finished_at"] == 2
    assert rk["created_at"] == 1


def test_stdout_path(tmp_path):
    home = tmp_path / "home"
    assert st.stdout_path(home, "abc") == home / "logs" / "abc.stdout"


def test_stderr_path(tmp_path):
    home = tmp_path / "home"
    assert st.stderr_path(home, "abc") == home / "logs" / "abc.stderr"


def test_now_iso_is_timezone_aware():
    now = st.now_iso()
    dt = datetime.fromisoformat(now)
    assert dt.tzinfo is not None


def test_new_job_id_unique():
    ids = {st.new_job_id() for _ in range(100)}
    assert len(ids) == 100


def test_init_db_idempotent(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    st.init_db(home)
