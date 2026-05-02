# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Unit tests for orchesjob.state."""

import json
import sqlite3
from datetime import datetime

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
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"jobs", "run_keys"} <= tables


def test_db_connect_wal_mode(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_row_to_job_deserialises_command(tmp_path):
    home = tmp_path / "home"
    st.init_db(home)
    conn = st.db_connect(home)
    job_id = st.new_job_id()
    conn.execute(
        """INSERT INTO jobs
           (job_id, run_key, command, status, stdout_file, stderr_file, started_at)
           VALUES (?, 'key', ?, 'STARTING', '', '', ?)""",
        (job_id, json.dumps(["echo", "hi"]), st.now_iso()),
    )
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    job = st.row_to_job(row)
    conn.close()
    assert job["command"] == ["echo", "hi"]


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
    """Calling init_db twice must not raise."""
    home = tmp_path / "home"
    st.init_db(home)
    st.init_db(home)
