# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Tests for the `orchesjob logs` command."""


def test_logs_stdout(jcli, cli):
    jcli("start", "--run-key", "l-out", "--sync", "--", "echo", "hello logs")
    stdout, _, rc = cli("logs", "--run-key", "l-out", "--stream", "stdout")
    assert rc == 0
    assert "hello logs" in stdout


def test_logs_stderr(jcli, cli):
    jcli("start", "--run-key", "l-err", "--sync", "--", "sh", "-c", "echo err-output >&2")
    stdout, _, rc = cli("logs", "--run-key", "l-err", "--stream", "stderr")
    assert rc == 0
    assert "err-output" in stdout


def test_logs_default_stream_is_stdout(jcli, cli):
    jcli("start", "--run-key", "l-default", "--sync", "--", "echo", "stdout-default")
    stdout, _, rc = cli("logs", "--run-key", "l-default")
    assert rc == 0
    assert "stdout-default" in stdout


def test_logs_by_job_id(jcli, cli):
    d = jcli("start", "--run-key", "l-jid", "--sync", "--", "echo", "by-job-id")
    stdout, _, rc = cli("logs", "--job-id", d["job_id"])
    assert rc == 0
    assert "by-job-id" in stdout


def test_logs_empty_stdout(jcli, cli):
    jcli("start", "--run-key", "l-empty", "--sync", "--", "true")
    stdout, _, rc = cli("logs", "--run-key", "l-empty")
    assert rc == 0
    assert stdout == ""


def test_logs_run_key_not_found(cli):
    _, stderr, rc = cli("logs", "--run-key", "no-such-key")
    assert rc != 0
    assert "not found" in stderr
