# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob clean` command."""

from __future__ import annotations

import json
import pathlib
import time

from tests.conftest import far_future_iso, far_past_iso, iso_now, wait_for_running


def _json_cli(cli, *args):
    stdout, stderr, rc = cli(*args)
    assert rc == 0, f"stdout={stdout}\nstderr={stderr}"
    return json.loads(stdout)


def test_clean_removes_terminal_jobs(jcli, cli):
    jcli("start", "--run-key", "c1", "--sync", "--", "true")
    jcli("start", "--run-key", "c2", "--sync", "--", "false")

    d = _json_cli(cli, "clean", "--before", far_future_iso())
    assert d["deleted"] == 2
    assert d["errors"] == 0
    assert d["dry_run"] is False
    assert len(d["items"]) == 2

    _, _, rc = cli("status", "--run-key", "c1")
    assert rc != 0


def test_clean_after_removes_jobs_at_or_after_cutoff(jcli, cli):
    jcli("start", "--run-key", "c-after-old", "--sync", "--", "true")
    time.sleep(1.2)
    cutoff = iso_now()
    time.sleep(1.2)
    jcli("start", "--run-key", "c-after-new", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--after", cutoff)
    assert result["deleted"] == 1

    assert jcli("status", "--run-key", "c-after-old")["status"] == "SUCCEEDED"
    _, _, rc = cli("status", "--run-key", "c-after-new")
    assert rc != 0


def test_clean_before_and_after_range(jcli, cli):
    jcli("start", "--run-key", "c-range-old", "--sync", "--", "true")
    time.sleep(1.2)
    after = iso_now()
    time.sleep(1.2)
    jcli("start", "--run-key", "c-range-mid", "--sync", "--", "true")
    time.sleep(1.2)
    before = iso_now()
    time.sleep(1.2)
    jcli("start", "--run-key", "c-range-new", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--after", after, "--before", before)
    assert result["deleted"] == 1

    assert jcli("status", "--run-key", "c-range-old")["status"] == "SUCCEEDED"
    assert jcli("status", "--run-key", "c-range-new")["status"] == "SUCCEEDED"
    _, _, rc = cli("status", "--run-key", "c-range-mid")
    assert rc != 0


def test_clean_all_removes_all_terminal_jobs(jcli, cli):
    jcli("start", "--run-key", "c-all-1", "--sync", "--", "true")
    jcli("start", "--run-key", "c-all-2", "--sync", "--", "false")

    result = _json_cli(cli, "clean", "--all")
    assert result["deleted"] == 2


def test_clean_run_key_restricts_selection(jcli, cli):
    jcli("start", "--run-key", "c-rk-1", "--sync", "--", "true")
    jcli("start", "--run-key", "c-rk-2", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--all", "--run-key", "c-rk-1")
    assert result["deleted"] == 1

    _, _, rc1 = cli("status", "--run-key", "c-rk-1")
    assert rc1 != 0
    assert jcli("status", "--run-key", "c-rk-2")["status"] == "SUCCEEDED"


def test_clean_by_job_id(jcli, cli):
    d1 = jcli("start", "--run-key", "c-jobid-1", "--sync", "--", "true")
    d2 = jcli("start", "--run-key", "c-jobid-2", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--job-id", d1["job_id"])
    assert result["deleted"] == 1

    _, _, rc1 = cli("status", "--job-id", d1["job_id"])
    assert rc1 != 0
    assert jcli("status", "--job-id", d2["job_id"])["status"] == "SUCCEEDED"


def test_clean_removes_log_files(jcli, cli):
    d = jcli("start", "--run-key", "c-logs", "--sync", "--", "echo", "bye")
    stdout_file = pathlib.Path(d["stdout_file"])
    stderr_file = pathlib.Path(d["stderr_file"])
    assert stdout_file.exists()
    assert stderr_file.exists()

    _json_cli(cli, "clean", "--before", far_future_iso())
    assert not stdout_file.exists()
    assert not stderr_file.exists()


def test_clean_preserves_running_jobs(home, jcli, cli):
    jcli("start", "--run-key", "c-run", "--", "sleep", "30")
    wait_for_running(home, cli, "c-run")
    try:
        result = _json_cli(cli, "clean", "--before", far_future_iso())
        assert result["deleted"] == 0

        d2 = jcli("status", "--run-key", "c-run")
        assert d2["status"] == "RUNNING"
    finally:
        jcli("abort", "--run-key", "c-run", "--grace-seconds", "0")


def test_clean_dry_run_does_not_delete(jcli, cli):
    jcli("start", "--run-key", "c-dry", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--before", far_future_iso(), "--dry-run")
    assert result["deleted"] == 1
    assert result["dry_run"] is True
    assert result["items"][0]["run_key"] == "c-dry"

    d = jcli("status", "--run-key", "c-dry")
    assert d["status"] == "SUCCEEDED"


def test_clean_only_older_than_cutoff(jcli, cli):
    jcli("start", "--run-key", "c-before", "--sync", "--", "true")
    time.sleep(1.2)
    cutoff = iso_now()
    time.sleep(1.2)
    jcli("start", "--run-key", "c-after", "--sync", "--", "true")

    result = _json_cli(cli, "clean", "--before", cutoff)
    assert result["deleted"] == 1

    d = jcli("status", "--run-key", "c-after")
    assert d["status"] == "SUCCEEDED"


def test_clean_nothing_to_delete(cli):
    result = _json_cli(cli, "clean", "--before", far_past_iso())
    assert result["deleted"] == 0


def test_clean_invalid_datetime(cli):
    _, stderr, rc = cli("clean", "--before", "not-a-date")
    assert rc != 0
    assert "Invalid --before" in stderr


def test_clean_rejects_invalid_option_combinations(cli):
    _, stderr, rc = cli("clean", "--all", "--before", far_future_iso())
    assert rc != 0
    assert "--all cannot be combined" in stderr

    _, stderr, rc = cli("clean", "--job-id", "abc", "--run-key", "rk")
    assert rc != 0
    assert "--job-id cannot be combined" in stderr


def test_clean_run_key_entry_removed(jcli, cli):
    jcli("start", "--run-key", "c-rk", "--sync", "--", "true")
    _json_cli(cli, "clean", "--before", far_future_iso())

    _, _, rc = cli("status", "--run-key", "c-rk")
    assert rc != 0


def test_clean_history_only_removes_old_entries(jcli, cli):
    jcli("start", "--run-key", "c-hist", "--sync", "--", "echo", "run1")
    time.sleep(1.2)
    cutoff = iso_now()
    time.sleep(1.2)
    jcli("start", "--run-key", "c-hist", "--sync", "--", "echo", "run2")

    result = _json_cli(cli, "clean", "--before", cutoff)
    assert result["deleted"] == 1

    history = jcli("status", "--run-key", "c-hist", "--all")
    assert len(history) == 1
    assert history[0]["command"] == ["echo", "run2"]
