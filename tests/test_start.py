# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob start` command."""

import json
import time

import pytest

from tests.conftest import kill_pid, wait_for_running


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------

def test_start_sync_success(jcli):
    d = jcli("start", "--run-key", "k-ok", "--sync", "--", "true")
    assert d["accepted"] is True
    assert d["existing"] is False
    assert d["status"] == "SUCCEEDED"
    assert d["exit_code"] == 0
    assert d["run_key"] == "k-ok"
    assert d["command"] == ["true"]
    assert d["strict"] is False
    assert d["job_id"]
    assert d["started_at"]
    assert d["finished_at"]


def test_start_sync_failure(jcli):
    d = jcli("start", "--run-key", "k-fail", "--sync", "--", "false")
    assert d["status"] == "FAILED"
    assert d["exit_code"] == 1


def test_start_async_returns_before_completion(home, cli):
    t0 = time.monotonic()
    stdout, _, rc = cli("start", "--run-key", "k-async", "--", "sleep", "30")
    elapsed = time.monotonic() - t0
    assert rc == 0
    d = json.loads(stdout)
    assert d["accepted"] is True
    assert elapsed < 3.0
    kill_pid(d.get("pid"))


def test_start_command_recorded(jcli):
    d = jcli("start", "--run-key", "k-cmd", "--sync", "--", "echo", "hello", "world")
    assert d["command"] == ["echo", "hello", "world"]


def test_start_stdout_stderr_files_exist(home, jcli):
    import pathlib
    d = jcli("start", "--run-key", "k-files", "--sync", "--", "echo", "hi")
    assert pathlib.Path(d["stdout_file"]).exists()
    assert pathlib.Path(d["stderr_file"]).exists()


# ---------------------------------------------------------------------------
# Idempotency — active job
# ---------------------------------------------------------------------------

def test_start_idempotent_while_running(home, cli, jcli):
    stdout1, _, rc = cli("start", "--run-key", "k-idem", "--", "sleep", "30")
    assert rc == 0
    d1 = json.loads(stdout1)
    try:
        wait_for_running(home, cli, "k-idem")

        stdout2, _, rc2 = cli("start", "--run-key", "k-idem", "--", "sleep", "30")
        assert rc2 == 0
        d2 = json.loads(stdout2)
        assert d2["existing"] is True
        assert d2["accepted"] is False
        assert d2["job_id"] == d1["job_id"]
    finally:
        kill_pid(d1.get("pid"))

def test_start_idempotent_returns_same_job_id(home, cli, jcli):
    stdout1, _, _ = cli("start", "--run-key", "k-idem2", "--", "sleep", "30")
    d1 = json.loads(stdout1)
    try:
        wait_for_running(home, cli, "k-idem2")
        stdout2, _, _ = cli("start", "--run-key", "k-idem2", "--", "sleep", "30")
        d2 = json.loads(stdout2)
        assert d2["job_id"] == d1["job_id"]
    finally:
        kill_pid(d1.get("pid"))


# ---------------------------------------------------------------------------
# Re-run after terminal state (default mode)
# ---------------------------------------------------------------------------

def test_start_reruns_after_succeeded(jcli):
    d1 = jcli("start", "--run-key", "k-rerun", "--sync", "--", "true")
    assert d1["status"] == "SUCCEEDED"

    d2 = jcli("start", "--run-key", "k-rerun", "--sync", "--", "echo", "second")
    assert d2["accepted"] is True
    assert d2["existing"] is False
    assert d2["job_id"] != d1["job_id"]
    assert d2["status"] == "SUCCEEDED"


def test_start_reruns_after_failed(jcli):
    d1 = jcli("start", "--run-key", "k-rerun-fail", "--sync", "--", "false")
    assert d1["status"] == "FAILED"

    d2 = jcli("start", "--run-key", "k-rerun-fail", "--sync", "--", "true")
    assert d2["accepted"] is True
    assert d2["job_id"] != d1["job_id"]
    assert d2["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# --strict mode
# ---------------------------------------------------------------------------

def test_start_strict_first_run_accepted(jcli):
    d = jcli("start", "--run-key", "k-strict", "--strict", "--sync", "--", "true")
    assert d["accepted"] is True
    assert d["strict"] is True
    assert d["status"] == "SUCCEEDED"


def test_start_strict_prevents_rerun_after_succeeded(jcli):
    d1 = jcli("start", "--run-key", "k-strict-block", "--strict", "--sync", "--", "true")
    d2 = jcli("start", "--run-key", "k-strict-block", "--strict", "--sync", "--", "echo", "nope")
    assert d2["existing"] is True
    assert d2["accepted"] is False
    assert d2["job_id"] == d1["job_id"]
    assert d2["status"] == "SUCCEEDED"


def test_start_strict_prevents_rerun_after_failed(jcli):
    d1 = jcli("start", "--run-key", "k-strict-fail", "--strict", "--sync", "--", "false")
    d2 = jcli("start", "--run-key", "k-strict-fail", "--strict", "--sync", "--", "true")
    assert d2["existing"] is True
    assert d2["job_id"] == d1["job_id"]


def test_start_no_strict_allows_rerun_after_terminal(jcli):
    d1 = jcli("start", "--run-key", "k-nostrict", "--sync", "--", "true")
    d2 = jcli("start", "--run-key", "k-nostrict", "--sync", "--", "echo", "again")
    assert d2["accepted"] is True
    assert d2["job_id"] != d1["job_id"]


def test_start_strict_idempotent_while_running(home, cli, jcli):
    stdout1, _, _ = cli("start", "--run-key", "k-strict-run", "--strict", "--", "sleep", "30")
    d1 = json.loads(stdout1)
    try:
        wait_for_running(home, cli, "k-strict-run")
        stdout2, _, _ = cli("start", "--run-key", "k-strict-run", "--strict", "--", "sleep", "30")
        d2 = json.loads(stdout2)
        assert d2["existing"] is True
        assert d2["job_id"] == d1["job_id"]
    finally:
        kill_pid(d1.get("pid"))


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_start_no_command_error(cli):
    _, stderr, rc = cli("start", "--run-key", "k-nocmd")
    assert rc != 0
    assert "command is required" in stderr


def test_start_missing_run_key_error(cli):
    _, stderr, rc = cli("start", "--", "echo", "hi")
    assert rc != 0
