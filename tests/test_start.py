# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob start`, `unlock`, and `rerun` commands."""

from __future__ import annotations

import json
import time

from tests.conftest import wait_for_running


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
    assert d["strict_override_used"] is False
    assert d["job_id"]
    assert isinstance(d["started_at"], int)
    assert isinstance(d["finished_at"], int)
    assert d["started_at_iso"]
    assert d["finished_at_iso"]
    assert d["attempt_no"] == 1


def test_start_sync_failure(jcli):
    d = jcli("start", "--run-key", "k-fail", "--sync", "--", "false")
    assert d["status"] == "FAILED"
    assert d["exit_code"] == 1


def test_start_async_returns_before_completion(home, cli, jcli):
    t0 = time.monotonic()
    stdout, stderr, rc = cli("start", "--run-key", "k-async", "--", "sleep", "30")
    elapsed = time.monotonic() - t0
    assert rc == 0, stderr
    d = json.loads(stdout)
    assert d["accepted"] is True
    assert elapsed < 10.0
    wait_for_running(home, cli, "k-async")
    jcli("abort", "--run-key", "k-async", "--grace-seconds", "0")


def test_start_command_recorded(jcli):
    d = jcli("start", "--run-key", "k-cmd", "--sync", "--", "echo", "hello", "world")
    assert d["command"] == ["echo", "hello", "world"]


def test_start_stdout_stderr_files_exist(jcli):
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
        jcli("abort", "--run-key", "k-idem", "--grace-seconds", "0")


def test_start_idempotent_returns_same_job_id(home, cli, jcli):
    stdout1, _, _ = cli("start", "--run-key", "k-idem2", "--", "sleep", "30")
    d1 = json.loads(stdout1)
    try:
        wait_for_running(home, cli, "k-idem2")
        stdout2, _, _ = cli("start", "--run-key", "k-idem2", "--", "sleep", "30")
        d2 = json.loads(stdout2)
        assert d2["job_id"] == d1["job_id"]
    finally:
        jcli("abort", "--run-key", "k-idem2", "--grace-seconds", "0")


# ---------------------------------------------------------------------------
# Re-run after terminal state (default mode)
# ---------------------------------------------------------------------------

def test_start_reruns_after_succeeded(jcli):
    d1 = jcli("start", "--run-key", "k-rerun", "--sync", "--", "true")
    d2 = jcli("start", "--run-key", "k-rerun", "--sync", "--", "echo", "second")
    assert d2["accepted"] is True
    assert d2["existing"] is False
    assert d2["job_id"] != d1["job_id"]
    assert d2["status"] == "SUCCEEDED"
    assert d2["attempt_no"] == 2
    assert d2["rerun_of_job_id"] == d1["job_id"]


def test_start_reruns_after_failed(jcli):
    d1 = jcli("start", "--run-key", "k-rerun-fail", "--sync", "--", "false")
    d2 = jcli("start", "--run-key", "k-rerun-fail", "--sync", "--", "true")
    assert d2["accepted"] is True
    assert d2["job_id"] != d1["job_id"]
    assert d2["status"] == "SUCCEEDED"
    assert d2["attempt_no"] == 2


# ---------------------------------------------------------------------------
# --strict mode and unlock
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


def test_unlock_allows_next_strict_start_once(jcli):
    d1 = jcli("start", "--run-key", "k-unlock", "--strict", "--sync", "--", "false")
    unlock = jcli("unlock", "--run-key", "k-unlock", "--reason", "manual recovery")
    assert unlock["unlocked"] is True
    assert unlock["run_key"] == "k-unlock"

    d2 = jcli("start", "--run-key", "k-unlock", "--strict", "--sync", "--", "true")
    assert d2["accepted"] is True
    assert d2["strict_override_used"] is True
    assert d2["job_id"] != d1["job_id"]
    assert d2["attempt_no"] == 2
    assert d2["rerun_of_job_id"] == d1["job_id"]
    assert d2["rerun_reason"] == "manual recovery"

    d3 = jcli("start", "--run-key", "k-unlock", "--strict", "--sync", "--", "echo", "blocked")
    assert d3["existing"] is True
    assert d3["job_id"] == d2["job_id"]
    assert d3["strict_override_used"] is False


def test_unlock_rejects_active_job(home, cli, jcli):
    cli("start", "--run-key", "k-unlock-active", "--strict", "--", "sleep", "30")
    try:
        wait_for_running(home, cli, "k-unlock-active")
        _, stderr, rc = cli("unlock", "--run-key", "k-unlock-active")
        assert rc != 0
        assert "Cannot unlock active job" in stderr
    finally:
        jcli("abort", "--run-key", "k-unlock-active", "--grace-seconds", "0")


def test_unlock_ttl_expires(cli, jcli):
    d1 = jcli("start", "--run-key", "k-unlock-ttl", "--strict", "--sync", "--", "true")
    jcli("unlock", "--run-key", "k-unlock-ttl", "--ttl", "1s")
    time.sleep(1.2)
    d2 = jcli("start", "--run-key", "k-unlock-ttl", "--strict", "--sync", "--", "echo", "blocked")
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
        jcli("abort", "--run-key", "k-strict-run", "--grace-seconds", "0")


# ---------------------------------------------------------------------------
# rerun command
# ---------------------------------------------------------------------------

def test_rerun_by_run_key_uses_original_command(jcli):
    d1 = jcli("start", "--run-key", "k-rerun-cmd", "--strict", "--sync", "--", "echo", "original")
    d2 = jcli("rerun", "--run-key", "k-rerun-cmd", "--sync", "--reason", "retry")
    assert d2["rerun"] is True
    assert d2["command"] == ["echo", "original"]
    assert d2["job_id"] != d1["job_id"]
    assert d2["attempt_no"] == 2
    assert d2["rerun_of_job_id"] == d1["job_id"]
    assert d2["rerun_reason"] == "retry"


def test_rerun_by_job_id(jcli):
    d1 = jcli("start", "--run-key", "k-rerun-jid", "--sync", "--", "true")
    d2 = jcli("rerun", "--job-id", d1["job_id"], "--sync")
    assert d2["rerun"] is True
    assert d2["attempt_no"] == 2


def test_rerun_rejects_active_job(home, cli, jcli):
    stdout, _, _ = cli("start", "--run-key", "k-rerun-active", "--", "sleep", "30")
    d = json.loads(stdout)
    try:
        wait_for_running(home, cli, "k-rerun-active")
        _, stderr, rc = cli("rerun", "--job-id", d["job_id"])
        assert rc != 0
        assert "Cannot rerun active job" in stderr
    finally:
        jcli("abort", "--run-key", "k-rerun-active", "--grace-seconds", "0")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_start_no_command_error(cli):
    _, stderr, rc = cli("start", "--run-key", "k-nocmd")
    assert rc != 0
    assert "command is required" in stderr


def test_start_missing_run_key_error(cli):
    _, _, rc = cli("start", "--", "echo", "hi")
    assert rc != 0
