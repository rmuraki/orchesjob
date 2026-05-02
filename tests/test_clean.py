# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob clean` command."""

import json
import pathlib
import time
from datetime import datetime, timezone

from tests.conftest import kill_pid, wait_for_running


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def test_clean_removes_terminal_jobs(jcli, cli):
    jcli("start", "--run-key", "c1", "--sync", "--", "true")
    jcli("start", "--run-key", "c2", "--sync", "--", "false")

    d = json.loads(cli("clean", "--before", _now())[0])
    assert d["deleted"] == 2
    assert d["errors"] == 0

    _, _, rc = cli("status", "--run-key", "c1")
    assert rc != 0


def test_clean_removes_log_files(jcli, cli):
    d = jcli("start", "--run-key", "c-logs", "--sync", "--", "echo", "bye")
    stdout_file = pathlib.Path(d["stdout_file"])
    stderr_file = pathlib.Path(d["stderr_file"])
    assert stdout_file.exists()
    assert stderr_file.exists()

    cli("clean", "--before", _now())
    assert not stdout_file.exists()
    assert not stderr_file.exists()


def test_clean_preserves_running_jobs(home, jcli, cli):
    d = jcli("start", "--run-key", "c-run", "--", "sleep", "30")
    try:
        wait_for_running(home, cli, "c-run")
        result = json.loads(cli("clean", "--before", _now())[0])
        assert result["deleted"] == 0

        d2 = jcli("status", "--run-key", "c-run")
        assert d2["status"] == "RUNNING"
    finally:
        kill_pid(d.get("pid"))


def test_clean_dry_run_does_not_delete(jcli, cli):
    jcli("start", "--run-key", "c-dry", "--sync", "--", "true")

    stdout, stderr, rc = cli("clean", "--before", _now(), "--dry-run")
    assert rc == 0
    result = json.loads(stdout)
    assert result["deleted"] == 1
    assert "Would delete" in stderr

    # Job should still exist
    d = jcli("status", "--run-key", "c-dry")
    assert d["status"] == "SUCCEEDED"


def test_clean_only_older_than_cutoff(jcli, cli):
    jcli("start", "--run-key", "c-before", "--sync", "--", "true")
    cutoff = _now()
    jcli("start", "--run-key", "c-after", "--sync", "--", "true")

    result = json.loads(cli("clean", "--before", cutoff)[0])
    assert result["deleted"] == 1

    # c-after should still exist
    d = jcli("status", "--run-key", "c-after")
    assert d["status"] == "SUCCEEDED"


def test_clean_nothing_to_delete(jcli, cli):
    # Cutoff in the past — nothing should match
    past = "2000-01-01T00:00:00+00:00"
    result = json.loads(cli("clean", "--before", past)[0])
    assert result["deleted"] == 0


def test_clean_invalid_datetime(cli):
    _, stderr, rc = cli("clean", "--before", "not-a-date")
    assert rc != 0
    assert "Invalid datetime" in stderr


def test_clean_run_key_entry_removed(jcli, cli):
    jcli("start", "--run-key", "c-rk", "--sync", "--", "true")
    cli("clean", "--before", _now())

    # run_key lookup should now fail (entry deleted)
    _, _, rc = cli("status", "--run-key", "c-rk")
    assert rc != 0


def test_clean_history_only_removes_old_entries(jcli, cli):
    jcli("start", "--run-key", "c-hist", "--sync", "--", "echo", "run1")
    cutoff = _now()
    jcli("start", "--run-key", "c-hist", "--sync", "--", "echo", "run2")

    result = json.loads(cli("clean", "--before", cutoff)[0])
    assert result["deleted"] == 1

    # Current job (run2) should still be accessible
    history = jcli("status", "--run-key", "c-hist", "--all")
    assert len(history) == 1
    assert history[0]["command"] == ["echo", "run2"]
