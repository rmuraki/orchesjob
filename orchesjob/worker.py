# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""Worker process for orchesjob.

Invoked internally as: orchesjob _worker --job-id <job_id>

Responsibilities:
1. Read job state from DB
2. Open stdout/stderr log files
3. Execute the target command
4. Wait for completion
5. Update job status atomically in DB
"""

import json
import os
import subprocess
import sys
from typing import Optional

from . import state as st


def run_worker(job_id: str) -> None:
    home = st.get_home()
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

        # Mark as RUNNING with worker_pid
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE jobs SET worker_pid = ?, status = 'RUNNING' WHERE job_id = ?",
            (worker_pid, job_id),
        )
        conn.execute("COMMIT")

        target_pid: Optional[int] = None
        exit_code: int = -1

        try:
            with open(stdout_file, "wb") as out_f, open(stderr_file, "wb") as err_f:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                )
                target_pid = proc.pid

                conn.execute("BEGIN")
                conn.execute(
                    "UPDATE jobs SET target_pid = ? WHERE job_id = ?",
                    (target_pid, job_id),
                )
                conn.execute("COMMIT")

                exit_code = proc.wait()

        except Exception as e:
            finished_at = st.now_iso()
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE jobs SET status = 'FAILED', exit_code = -1, finished_at = ? WHERE job_id = ?",
                (finished_at, job_id),
            )
            conn.execute("COMMIT")
            print(f"orchesjob _worker: error running command: {e}", file=sys.stderr)
            return

        finished_at = st.now_iso()
        status = "SUCCEEDED" if exit_code == 0 else "FAILED"

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, finished_at = ? WHERE job_id = ?",
            (status, exit_code, finished_at, job_id),
        )
        conn.execute("COMMIT")

    finally:
        conn.close()
