# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Shared fixtures for orchesjob tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import pytest


@pytest.fixture
def home(tmp_path):
    """Isolated ORCHESJOB_HOME for each test."""
    return tmp_path / "orchesjob_home"


@pytest.fixture
def cli(home):
    """Run orchesjob and return (stdout, stderr, returncode)."""

    def _cli(*args: str) -> Tuple[str, str, int]:
        result = subprocess.run(
            [sys.executable, "-m", "orchesjob", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "ORCHESJOB_HOME": str(home)},
            timeout=30,
        )
        return result.stdout, result.stderr, result.returncode

    return _cli


@pytest.fixture
def jcli(home):
    """Run orchesjob and return parsed JSON (asserts exit code == 0)."""

    def _jcli(*args: str) -> Any:
        result = subprocess.run(
            [sys.executable, "-m", "orchesjob", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "ORCHESJOB_HOME": str(home)},
            timeout=30,
        )
        assert result.returncode == 0, (
            f"orchesjob {list(args)} exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return json.loads(result.stdout)

    return _jcli


def iso_now(offset_seconds: int = 0) -> str:
    """Return an aware ISO timestamp for CLI datetime options."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def far_future_iso() -> str:
    """Cutoff safely after all jobs created in a test."""
    return iso_now(3600)


def far_past_iso() -> str:
    """Cutoff safely before all jobs created in a test."""
    return "2000-01-01T00:00:00+00:00"


def kill_pid(pid: Optional[int]) -> None:
    """Best-effort SIGTERM for a single PID, ignoring errors."""
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def kill_process_group_or_pid(pid: Optional[int]) -> None:
    """Best-effort cleanup for commands started with start_new_session=True."""
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    kill_pid(pid)


def wait_for_running(home, cli, run_key: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Poll status until job is RUNNING (or timeout raises)."""
    deadline = time.monotonic() + timeout
    last: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        stdout, _, rc = cli("status", "--run-key", run_key)
        if rc == 0:
            last = json.loads(stdout)
            if last["status"] == "RUNNING" and last.get("worker_pid"):
                return last
        time.sleep(0.05)
    raise TimeoutError(f"Job {run_key!r} did not reach RUNNING within {timeout}s; last={last!r}")
