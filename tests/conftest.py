# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Shared fixtures for orchesjob tests."""

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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
        )
        assert result.returncode == 0, (
            f"orchesjob {list(args)} exited with {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return json.loads(result.stdout)
    return _jcli


def kill_pid(pid: Optional[int]) -> None:
    """Best-effort SIGTERM, ignoring errors."""
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def wait_for_running(home, cli, run_key: str, timeout: float = 3.0) -> Dict[str, Any]:
    """Poll status until job is RUNNING (or timeout raises)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stdout, _, rc = cli("status", "--run-key", run_key)
        if rc == 0:
            d = json.loads(stdout)
            if d["status"] == "RUNNING":
                return d
        time.sleep(0.05)
    raise TimeoutError(f"Job {run_key!r} did not reach RUNNING within {timeout}s")
