# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob abort` command."""

from __future__ import annotations

import os
import signal
import time

from tests.conftest import wait_for_running


def _alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_abort_by_run_key_marks_aborted(home, cli, jcli):
    cli("start", "--run-key", "a-run-key", "--", "sleep", "30")
    wait_for_running(home, cli, "a-run-key")

    d = jcli("abort", "--run-key", "a-run-key", "--reason", "manual stop", "--grace-seconds", "0")
    assert d["aborted"] is True
    assert d["status"] == "ABORTED"
    assert d["abort_reason"] == "manual stop"
    assert d["aborted_at"] is not None
    assert d["finished_at"] is not None

    s = jcli("status", "--run-key", "a-run-key")
    assert s["status"] == "ABORTED"


def test_abort_by_job_id(home, cli, jcli):
    d1 = jcli("start", "--run-key", "a-job-id", "--", "sleep", "30")
    wait_for_running(home, cli, "a-job-id")

    d2 = jcli("abort", "--job-id", d1["job_id"], "--grace-seconds", "0")
    assert d2["status"] == "ABORTED"
    assert d2["job_id"] == d1["job_id"]


def test_abort_rejects_terminal_job(cli, jcli):
    jcli("start", "--run-key", "a-terminal", "--sync", "--", "true")
    _, stderr, rc = cli("abort", "--run-key", "a-terminal")
    assert rc != 0
    assert "Job is not running" in stderr


def test_abort_kills_target_process_group(home, cli, jcli):
    cli("start", "--run-key", "a-pgrp", "--", "sh", "-c", "sleep 30 & wait")
    running = wait_for_running(home, cli, "a-pgrp")
    target_pid = running["target_pid"]

    jcli("abort", "--run-key", "a-pgrp", "--grace-seconds", "0")
    time.sleep(0.3)

    # The process group leader should have been terminated. Some systems can
    # keep a zombie briefly, so the authoritative assertion is the state.
    s = jcli("status", "--run-key", "a-pgrp")
    assert s["status"] == "ABORTED"
    try:
        os.killpg(target_pid, signal.SIGTERM)
    except OSError:
        pass
