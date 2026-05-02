# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""CLI entry point for orchesjob."""

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from . import __version__
from . import state as st

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_NOT_FOUND = 3
EXIT_INVALID_STATE = 4
EXIT_LOCK_ERROR = 5


def die(msg: str, code: int = EXIT_ERROR) -> None:
    print(f"orchesjob: {msg}", file=sys.stderr)
    sys.exit(code)


def output_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, indent=2))


def is_process_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it
        return True


def validate_job_id(job_id: str) -> None:
    if "/" in job_id or "\\" in job_id:
        die(f"Invalid job_id: contains path separator", EXIT_INVALID_ARGS)


def resolve_job_id(conn: sqlite3.Connection, run_key: Optional[str], job_id: Optional[str]) -> str:
    if job_id:
        validate_job_id(job_id)
        return job_id
    if run_key:
        row = conn.execute(
            "SELECT job_id FROM run_keys WHERE run_key = ?", (run_key,)
        ).fetchone()
        if row is None:
            die(f"run_key not found", EXIT_NOT_FOUND)
        return row["job_id"]
    die("Either --run-key or --job-id is required", EXIT_INVALID_ARGS)


def build_start_response(job: Dict[str, Any], existing: bool, mode: str, strict: bool) -> Dict[str, Any]:
    return {
        "accepted": not existing,
        "existing": existing,
        "mode": mode,
        "strict": strict,
        "run_key": job.get("run_key"),
        "job_id": job["job_id"],
        "pid": job.get("target_pid") or job.get("worker_pid"),
        "command": job.get("command"),
        "status": job["status"],
        "stdout_file": job.get("stdout_file"),
        "stderr_file": job.get("stderr_file"),
        "exit_code": job.get("exit_code"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def reconcile_running_job(
    conn: sqlite3.Connection, job_id: str
) -> Dict[str, Any]:
    """Re-read job state; mark LOST if worker process is gone."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        die(f"Job not found: {job_id}", EXIT_NOT_FOUND)
    job = st.row_to_job(row)

    if job["status"] in ("RUNNING", "STARTING"):
        worker_pid = job.get("worker_pid")
        if worker_pid is not None and not is_process_alive(worker_pid):
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE jobs SET status='LOST' WHERE job_id = ? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','LOST')",
                (job_id,),
            )
            conn.execute("COMMIT")
            job["status"] = "LOST"

    return job


_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "LOST", "CANCELLED"}


def wait_for_job(
    conn: sqlite3.Connection,
    job_id: str,
    poll_interval: float = 0.5,
) -> Dict[str, Any]:
    """Poll until the job reaches a terminal state, then return the job dict."""
    while True:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        job = st.row_to_job(row)

        if job["status"] in _TERMINAL_STATUSES:
            return job

        worker_pid = job.get("worker_pid")
        if worker_pid is not None and not is_process_alive(worker_pid):
            # Re-read in case worker just finished
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            job = st.row_to_job(row)
            if job["status"] in _TERMINAL_STATUSES:
                return job
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE jobs SET status='LOST' WHERE job_id = ? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','LOST')",
                (job_id,),
            )
            conn.execute("COMMIT")
            job["status"] = "LOST"
            return job

        time.sleep(poll_interval)


def cmd_start(args: argparse.Namespace) -> None:
    run_key: str = args.run_key
    command: List[str] = args.command
    # Strip leading '--' if present (from argparse REMAINDER)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        die("command is required (provide it after --)", EXIT_INVALID_ARGS)

    sync_mode: bool = args.sync
    strict: bool = args.strict
    mode = "sync" if sync_mode else "async"

    home = st.get_home()
    st.init_db(home)

    conn = st.db_connect(home)
    existing = False
    job_id: Optional[str] = None

    try:
        conn.execute("BEGIN EXCLUSIVE")
        row = conn.execute(
            """SELECT rk.job_id, j.status
               FROM run_keys rk JOIN jobs j ON rk.job_id = j.job_id
               WHERE rk.run_key = ?""",
            (run_key,),
        ).fetchone()

        if row is not None and (row["status"] not in _TERMINAL_STATUSES or strict):
            # Active job → always return existing.
            # Terminal job + --strict → also return existing (one execution per run_key, ever).
            existing = True
            job_id = row["job_id"]
            conn.execute("COMMIT")
        else:
            # No prior job, or prior job has finished (and --strict not set) — start a new one
            job_id = st.new_job_id()
            started_at = st.now_iso()
            stdout_file = str(st.stdout_path(home, job_id))
            stderr_file = str(st.stderr_path(home, job_id))

            conn.execute(
                """INSERT INTO jobs
                   (job_id, run_key, worker_pid, target_pid, command, status,
                    exit_code, stdout_file, stderr_file, started_at, finished_at)
                   VALUES (?, ?, NULL, NULL, ?, 'STARTING', NULL, ?, ?, ?, NULL)""",
                (job_id, run_key, json.dumps(command), stdout_file, stderr_file, started_at),
            )
            if row is None:
                conn.execute(
                    "INSERT INTO run_keys (run_key, job_id, created_at) VALUES (?, ?, ?)",
                    (run_key, job_id, started_at),
                )
            else:
                conn.execute(
                    "UPDATE run_keys SET job_id = ?, created_at = ? WHERE run_key = ?",
                    (job_id, started_at, run_key),
                )
            conn.execute("COMMIT")

            # Launch detached worker
            worker_cmd = [sys.executable, "-m", "orchesjob", "_worker", "--job-id", job_id]
            worker_proc = subprocess.Popen(
                worker_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE jobs SET worker_pid = ?, status = 'RUNNING' WHERE job_id = ?",
                (worker_proc.pid, job_id),
            )
            conn.execute("COMMIT")

        if sync_mode:
            job = wait_for_job(conn, job_id)
        else:
            job = reconcile_running_job(conn, job_id)

        output_json(build_start_response(job, existing=existing, mode=mode, strict=strict))

    finally:
        conn.close()


def format_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "run_key": job.get("run_key"),
        "command": job.get("command"),
        "pid": job.get("target_pid") or job.get("worker_pid"),
        "status": job["status"],
        "exit_code": job.get("exit_code"),
        "stdout_file": job.get("stdout_file"),
        "stderr_file": job.get("stderr_file"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def cmd_status(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        show_all: bool = getattr(args, "all", False)
        run_key: Optional[str] = getattr(args, "run_key", None)
        job_id: Optional[str] = getattr(args, "job_id", None)

        if show_all and run_key:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE run_key = ? ORDER BY started_at DESC",
                (run_key,),
            ).fetchall()
            if not rows:
                die(f"No jobs found for run_key: {run_key}", EXIT_NOT_FOUND)
            history = []
            for row in rows:
                job = st.row_to_job(row)
                # Reconcile only the most recent (active) job
                if job["status"] in ("RUNNING", "STARTING"):
                    job = reconcile_running_job(conn, job["job_id"])
                history.append(format_job(job))
            output_json(history)
        else:
            resolved_id = resolve_job_id(conn, run_key, job_id)
            job = reconcile_running_job(conn, resolved_id)
            output_json(format_job(job))
    finally:
        conn.close()


def cmd_logs(args: argparse.Namespace) -> None:
    home = st.get_home()
    conn = st.db_connect(home)
    try:
        job_id = resolve_job_id(conn, getattr(args, "run_key", None), getattr(args, "job_id", None))
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            die(f"Job not found: {job_id}", EXIT_NOT_FOUND)
        job = st.row_to_job(row)
    finally:
        conn.close()

    stream: str = args.stream
    if stream == "stdout":
        log_file = job.get("stdout_file")
    elif stream == "stderr":
        log_file = job.get("stderr_file")
    else:
        die(f"Invalid stream: {stream}. Use 'stdout' or 'stderr'", EXIT_INVALID_ARGS)

    if not log_file or not pathlib.Path(log_file).exists():
        die(f"Log file not found: {log_file}", EXIT_NOT_FOUND)

    with open(log_file, "rb") as f:
        sys.stdout.buffer.write(f.read())


def cmd_clean(args: argparse.Namespace) -> None:
    before_str: str = args.before
    dry_run: bool = args.dry_run

    try:
        cutoff = datetime.fromisoformat(before_str)
        if cutoff.tzinfo is None:
            cutoff = cutoff.astimezone()
    except ValueError:
        die(
            f"Invalid datetime: {before_str!r}. Use ISO 8601 format (e.g. 2026-01-01 or 2026-01-01T12:00:00+09:00)",
            EXIT_INVALID_ARGS,
        )

    from datetime import timezone as _tz
    cutoff_utc = cutoff.astimezone(_tz.utc)

    def _ts_before_cutoff(ts_str: Optional[str]) -> bool:
        if not ts_str:
            return False
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            return ts.astimezone(_tz.utc) < cutoff_utc
        except ValueError:
            return False

    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        # Only terminal jobs are eligible; RUNNING/STARTING are never touched.
        terminal = ", ".join(f"'{s}'" for s in sorted(_TERMINAL_STATUSES))
        all_rows = conn.execute(
            f"""SELECT j.job_id, j.run_key, j.stdout_file, j.stderr_file,
                       j.finished_at, j.started_at,
                       rk.job_id AS current_job_id
                FROM jobs j
                LEFT JOIN run_keys rk ON rk.run_key = j.run_key AND rk.job_id = j.job_id
                WHERE j.status IN ({terminal})
                ORDER BY j.started_at""",
        ).fetchall()

        rows = [r for r in all_rows if _ts_before_cutoff(r["finished_at"] or r["started_at"])]

        if not rows:
            output_json({"deleted": 0, "errors": 0})
            return

        deleted = 0
        errors = 0

        for row in rows:
            job_id = row["job_id"]
            run_key = row["run_key"]
            finished_at = row["finished_at"] or row["started_at"]
            is_current = row["current_job_id"] is not None

            if dry_run:
                print(
                    f"Would delete job {job_id} (run_key={run_key}, finished_at={finished_at})",
                    file=sys.stderr,
                )
                deleted += 1
                continue

            for log_file in (row["stdout_file"], row["stderr_file"]):
                if log_file:
                    try:
                        pathlib.Path(log_file).unlink(missing_ok=True)
                    except OSError as e:
                        print(f"orchesjob: warning: {e}", file=sys.stderr)
                        errors += 1

            conn.execute("BEGIN")
            if is_current:
                conn.execute("DELETE FROM run_keys WHERE run_key = ?", (run_key,))
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.execute("COMMIT")
            deleted += 1

        output_json({"deleted": deleted, "errors": errors})
    finally:
        conn.close()


def cmd_worker(args: argparse.Namespace) -> None:
    from .worker import run_worker
    job_id: str = args.job_id
    validate_job_id(job_id)
    run_worker(job_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="orchesjob",
        description="Lightweight idempotent one-shot job runner",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- start ---
    p_start = subparsers.add_parser("start", help="Start a job")
    p_start.add_argument(
        "--run-key",
        required=True,
        dest="run_key",
        metavar="RUN_KEY",
        help="Idempotency key for this job",
    )
    p_start.add_argument(
        "--sync",
        action="store_true",
        help="Wait for job completion before returning",
    )
    p_start.add_argument(
        "--strict",
        action="store_true",
        help="Never create more than one execution per run_key, even after the previous job has finished",
    )
    p_start.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run (specify after --)",
    )

    # --- status ---
    p_status = subparsers.add_parser("status", help="Get job status")
    g_status = p_status.add_mutually_exclusive_group(required=True)
    g_status.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_status.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    p_status.add_argument(
        "--all",
        action="store_true",
        dest="all",
        help="Show all past executions for the run_key (requires --run-key)",
    )

    # --- logs ---
    p_logs = subparsers.add_parser("logs", help="Print job log output")
    g_logs = p_logs.add_mutually_exclusive_group(required=True)
    g_logs.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_logs.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    p_logs.add_argument(
        "--stream",
        default="stdout",
        choices=["stdout", "stderr"],
        help="Log stream to print (default: stdout)",
    )

    # --- clean ---
    p_clean = subparsers.add_parser("clean", help="Delete old terminal job data")
    p_clean.add_argument(
        "--before",
        required=True,
        metavar="DATETIME",
        help="Delete terminal jobs finished before this datetime (ISO 8601, e.g. 2026-01-01 or 2026-01-01T12:00:00+09:00)",
    )
    p_clean.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview what would be deleted without making any changes",
    )

    # --- _worker (internal) ---
    p_worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    p_worker.add_argument("--job-id", required=True, dest="job_id")

    args = parser.parse_args()

    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "clean": cmd_clean,
        "_worker": cmd_worker,
    }

    func = dispatch.get(args.subcommand)
    if func is None:
        die(f"Unknown command: {args.subcommand}", EXIT_INVALID_ARGS)
    func(args)
