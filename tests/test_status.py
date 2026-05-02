# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob status` command."""

import json
import os
import signal
import sqlite3
import time

from tests.conftest import kill_pid, wait_for_running


def test_status_by_run_key(jcli):
    jcli("start", "--run-key", "s1", "--sync", "--", "true")
    d = jcli("status", "--run-key", "s1")
    assert d["run_key"] == "s1"
    assert d["status"] == "SUCCEEDED"
    assert d["exit_code"] == 0
    assert d["command"] == ["true"]


def test_status_by_job_id(jcli):
    d1 = jcli("start", "--run-key", "s2", "--sync", "--", "true")
    d2 = jcli("status", "--job-id", d1["job_id"])
    assert d2["job_id"] == d1["job_id"]
    assert d2["status"] == "SUCCEEDED"


def test_status_includes_command(jcli):
    jcli("start", "--run-key", "s-cmd", "--sync", "--", "echo", "a", "b")
    d = jcli("status", "--run-key", "s-cmd")
    assert d["command"] == ["echo", "a", "b"]


def test_status_includes_timestamps(jcli):
    jcli("start", "--run-key", "s-ts", "--sync", "--", "true")
    d = jcli("status", "--run-key", "s-ts")
    assert d["started_at"]
    assert d["finished_at"]


def test_status_all_returns_list(jcli):
    jcli("start", "--run-key", "s-all", "--sync", "--", "true")
    jcli("start", "--run-key", "s-all", "--sync", "--", "echo", "run2")
    jcli("start", "--run-key", "s-all", "--sync", "--", "echo", "run3")

    history = jcli("status", "--run-key", "s-all", "--all")
    assert isinstance(history, list)
    assert len(history) == 3


def test_status_all_ordered_newest_first(jcli):
    jcli("start", "--run-key", "s-ord", "--sync", "--", "echo", "first")
    jcli("start", "--run-key", "s-ord", "--sync", "--", "echo", "second")
    jcli("start", "--run-key", "s-ord", "--sync", "--", "echo", "third")

    history = jcli("status", "--run-key", "s-ord", "--all")
    assert history[0]["command"] == ["echo", "third"]
    assert history[2]["command"] == ["echo", "first"]


def test_status_all_single_entry(jcli):
    jcli("start", "--run-key", "s-one", "--sync", "--", "true")
    history = jcli("status", "--run-key", "s-one", "--all")
    assert len(history) == 1


def test_status_run_key_not_found(cli):
    _, stderr, rc = cli("status", "--run-key", "no-such-key")
    assert rc != 0
    assert "not found" in stderr


def test_status_job_id_not_found(cli):
    _, stderr, rc = cli("status", "--job-id", "00000000-0000-0000-0000-000000000000")
    assert rc != 0


def test_status_reconcile_lost(home, cli, jcli):
    d = jcli("start", "--run-key", "s-lost", "--", "sleep", "60")

    # Wait until the worker is confirmed running
    wait_for_running(home, cli, "s-lost")

    # Retrieve worker_pid from the DB directly
    conn = sqlite3.connect(str(home / "orchesjob.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT worker_pid, target_pid FROM jobs WHERE job_id = ?", (d["job_id"],)
    ).fetchone()
    worker_pid = row["worker_pid"]
    target_pid = row["target_pid"]
    conn.close()

    # Kill the worker — target (sleep) becomes orphan, status should become LOST
    os.kill(worker_pid, signal.SIGKILL)
    time.sleep(0.3)

    d2 = jcli("status", "--run-key", "s-lost")
    assert d2["status"] == "LOST"

    # Cleanup orphaned sleep
    kill_pid(target_pid)
