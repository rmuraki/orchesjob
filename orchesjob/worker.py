# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Worker process for orchesjob.

Invoked internally as: orchesjob _worker --job-id <job_id>

Responsibilities:
1. Read job state from DB
2. Open stdout/stderr log files
3. Execute the target command in its own process group
4. Wait for completion
5. Update job status atomically in DB
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Optional

from . import state as st


TERMINAL_STATUSES = st.TERMINAL_STATUSES


def _update_failed(conn, job_id: str, message: str) -> None:
    now = st.now_ts()
    conn.execute("BEGIN")
    conn.execute(
        """UPDATE jobs
           SET status = 'FAILED', exit_code = -1, finished_at = ?, updated_at = ?
           WHERE job_id = ?
             AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','LOST','ABORTED')""",
        (now, now, job_id),
    )
    conn.execute("COMMIT")
    print(f"orchesjob _worker: {message}", file=sys.stderr)


def _terminate_target_process_group(pid: int) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc_exited(pid):
            return
        time.sleep(0.1)

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sigkill)
        except OSError:
            pass
    else:
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass


def proc_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


def run_worker(job_id: str) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)

    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            print(f"orchesjob _worker: job state not found for {job_id}", file=sys.stderr)
            sys.exit(1)

        job = st.row_to_job(row)
        stdout_file = job["stdout_file"]
        stderr_file = job["stderr_file"]
        command = job["command"]
        worker_pid = os.getpid()
        now = st.now_ts()

        # Record the worker PID, but keep the job in STARTING. RUNNING is
        # reserved for the moment the target command has actually been spawned
        # and target_pid has been stored.
        conn.execute("BEGIN")
        cur = conn.execute(
            """UPDATE jobs
               SET worker_pid = ?, updated_at = ?
               WHERE job_id = ?
                 AND status = 'STARTING'""",
            (worker_pid, now, job_id),
        )
        changed = cur.rowcount
        conn.execute("COMMIT")
        if changed == 0:
            return

        target_pid: Optional[int] = None
        exit_code: int = -1

        try:
            with open(stdout_file, "wb") as out_f, open(stderr_file, "wb") as err_f:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                    start_new_session=True,
                )
                target_pid = proc.pid
                now = st.now_ts()

                conn.execute("BEGIN")
                cur = conn.execute(
                    """UPDATE jobs
                       SET target_pid = ?, status = 'RUNNING', updated_at = ?
                       WHERE job_id = ?
                         AND status = 'STARTING'""",
                    (target_pid, now, job_id),
                )
                changed = cur.rowcount
                conn.execute("COMMIT")

                if changed == 0:
                    # The controller may have aborted the job between Popen() and
                    # the target_pid update. Kill the just-created process group so
                    # the target is not orphaned.
                    _terminate_target_process_group(target_pid)
                    return

                exit_code = proc.wait()

        except Exception as e:
            _update_failed(conn, job_id, f"error running command: {e}")
            return

        now = st.now_ts()
        status = "SUCCEEDED" if exit_code == 0 else "FAILED"

        # Do not overwrite ABORTED/CANCELLED/LOST set by the controller.
        conn.execute("BEGIN")
        conn.execute(
            """UPDATE jobs
               SET status = ?, exit_code = ?, finished_at = ?, updated_at = ?
               WHERE job_id = ?
                 AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','LOST','ABORTED')""",
            (status, exit_code, now, now, job_id),
        )
        conn.execute("COMMIT")

    finally:
        conn.close()
