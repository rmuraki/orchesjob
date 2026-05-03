# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ryosuke Muraki
"""CLI entry point for orchesjob."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from . import __version__
from . import state as st

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_NOT_FOUND = 3
EXIT_INVALID_STATE = 4
EXIT_LOCK_ERROR = 5

_TERMINAL_STATUSES = st.TERMINAL_STATUSES
_ACTIVE_STATUSES = st.ACTIVE_STATUSES

JsonValue = Union[Dict[str, Any], List[Any]]


def die(msg: str, code: int = EXIT_ERROR) -> None:
    print(f"orchesjob: {msg}", file=sys.stderr)
    sys.exit(code)


def output_json(data: JsonValue) -> None:
    print(json.dumps(data, indent=2))


def is_process_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False

    # On Linux, os.kill(pid, 0) returns success for zombies. Treat zombies as
    # not alive so sync waits and reconcile do not hang if a detached worker
    # exits before being reaped.
    stat_path = pathlib.Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text()
        parts = stat.split()
        if len(parts) >= 3 and parts[2] == "Z":
            return False
    except OSError:
        pass

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it.
        return True
    except OSError:
        return False


def validate_job_id(job_id: str) -> None:
    if "/" in job_id or "\\" in job_id:
        die("Invalid job_id: contains path separator", EXIT_INVALID_ARGS)


def resolve_job_id(conn: sqlite3.Connection, run_key: Optional[str], job_id: Optional[str]) -> str:
    if job_id:
        validate_job_id(job_id)
        return job_id
    if run_key:
        row = conn.execute("SELECT job_id FROM run_keys WHERE run_key = ?", (run_key,)).fetchone()
        if row is None:
            die("run_key not found", EXIT_NOT_FOUND)
        return row["job_id"]
    die("Either --run-key or --job-id is required", EXIT_INVALID_ARGS)


def parse_ttl_to_seconds(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip().lower()
    if not value:
        die("Invalid ttl: empty", EXIT_INVALID_ARGS)
    unit = value[-1]
    number = value[:-1]
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit in multipliers:
        try:
            amount = int(number)
        except ValueError:
            die(f"Invalid ttl: {value!r}", EXIT_INVALID_ARGS)
        if amount <= 0:
            die("ttl must be positive", EXIT_INVALID_ARGS)
        return amount * multipliers[unit]
    try:
        amount = int(value)
    except ValueError:
        die(f"Invalid ttl: {value!r}. Use seconds or suffix s/m/h/d", EXIT_INVALID_ARGS)
    if amount <= 0:
        die("ttl must be positive", EXIT_INVALID_ARGS)
    return amount


def _format_ts_fields(out: Dict[str, Any], job: Dict[str, Any]) -> None:
    for key in ("started_at", "finished_at", "updated_at", "aborted_at"):
        out[key] = job.get(key)
        out[f"{key}_iso"] = st.timestamp_to_iso(job.get(key))


def format_job(job: Dict[str, Any]) -> Dict[str, Any]:
    pid = job.get("target_pid") or job.get("worker_pid")
    pid_kind = "target" if job.get("target_pid") is not None else ("worker" if job.get("worker_pid") is not None else None)
    out: Dict[str, Any] = {
        "job_id": job["job_id"],
        "run_key": job.get("run_key"),
        "command": job.get("command"),
        "pid": pid,
        "pid_kind": pid_kind,
        "worker_pid": job.get("worker_pid"),
        "target_pid": job.get("target_pid"),
        "status": job["status"],
        "exit_code": job.get("exit_code"),
        "stdout_file": job.get("stdout_file"),
        "stderr_file": job.get("stderr_file"),
        "attempt_no": job.get("attempt_no"),
        "rerun_of_job_id": job.get("rerun_of_job_id"),
        "rerun_reason": job.get("rerun_reason"),
        "abort_reason": job.get("abort_reason"),
    }
    _format_ts_fields(out, job)
    return out


def build_start_response(
    job: Dict[str, Any],
    *,
    existing: bool,
    mode: str,
    strict: bool,
    strict_override_used: bool = False,
) -> Dict[str, Any]:
    out = format_job(job)
    out.update(
        {
            "accepted": not existing,
            "existing": existing,
            "mode": mode,
            "strict": strict,
            "strict_override_used": strict_override_used,
        }
    )
    return out


def read_job(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        die(f"Job not found: {job_id}", EXIT_NOT_FOUND)
    return st.row_to_job(row)


def reconcile_running_job(conn: sqlite3.Connection, job_id: str) -> Dict[str, Any]:
    """Re-read job state; mark LOST if worker process is gone."""
    job = read_job(conn, job_id)

    if job["status"] in _ACTIVE_STATUSES:
        worker_pid = job.get("worker_pid")
        if worker_pid is not None and not is_process_alive(worker_pid):
            now = st.now_ts()
            conn.execute("BEGIN")
            conn.execute(
                """UPDATE jobs
                   SET status='LOST', finished_at = COALESCE(finished_at, ?), updated_at = ?
                   WHERE job_id = ?
                     AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','LOST','ABORTED')""",
                (now, now, job_id),
            )
            conn.execute("COMMIT")
            job = read_job(conn, job_id)

    return job


def wait_for_job(conn: sqlite3.Connection, job_id: str, poll_interval: float = 0.5) -> Dict[str, Any]:
    """Poll until the job reaches a terminal state, then return the job dict."""
    while True:
        job = read_job(conn, job_id)
        if job["status"] in _TERMINAL_STATUSES:
            return job

        worker_pid = job.get("worker_pid")
        if worker_pid is not None and not is_process_alive(worker_pid):
            return reconcile_running_job(conn, job_id)

        time.sleep(poll_interval)


def _latest_job_for_run_key(conn: sqlite3.Connection, run_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT rk.job_id, j.status, j.command, j.attempt_no
           FROM run_keys rk JOIN jobs j ON rk.job_id = j.job_id
           WHERE rk.run_key = ?""",
        (run_key,),
    ).fetchone()


def _max_attempt_no(conn: sqlite3.Connection, run_key: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) AS max_attempt_no FROM jobs WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    return int(row["max_attempt_no"] or 0)


def _valid_strict_override(conn: sqlite3.Connection, run_key: str, now: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM strict_overrides
           WHERE run_key = ?
             AND used_at IS NULL
             AND (expires_at IS NULL OR expires_at > ?)""",
        (run_key, now),
    ).fetchone()


def _insert_job_locked(
    conn: sqlite3.Connection,
    *,
    home: pathlib.Path,
    run_key: str,
    command: Sequence[str],
    rerun_of_job_id: Optional[str] = None,
    rerun_reason: Optional[str] = None,
) -> str:
    job_id = st.new_job_id()
    now = st.now_ts()
    stdout_file = str(st.stdout_path(home, job_id))
    stderr_file = str(st.stderr_path(home, job_id))
    attempt_no = _max_attempt_no(conn, run_key) + 1

    conn.execute(
        """INSERT INTO jobs
           (job_id, run_key, worker_pid, target_pid, command, status,
            exit_code, stdout_file, stderr_file, started_at, finished_at, updated_at,
            aborted_at, abort_reason, rerun_of_job_id, attempt_no, rerun_reason)
           VALUES (?, ?, NULL, NULL, ?, 'STARTING', NULL, ?, ?, ?, NULL, ?,
                   NULL, NULL, ?, ?, ?)""",
        (
            job_id,
            run_key,
            json.dumps(list(command)),
            stdout_file,
            stderr_file,
            now,
            now,
            rerun_of_job_id,
            attempt_no,
            rerun_reason,
        ),
    )

    row = conn.execute("SELECT 1 FROM run_keys WHERE run_key = ?", (run_key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO run_keys (run_key, job_id, created_at) VALUES (?, ?, ?)",
            (run_key, job_id, now),
        )
    else:
        conn.execute(
            "UPDATE run_keys SET job_id = ?, created_at = ? WHERE run_key = ?",
            (job_id, now, run_key),
        )
    return job_id


def _launch_worker(conn: sqlite3.Connection, job_id: str) -> None:
    worker_cmd = [sys.executable, "-m", "orchesjob", "_worker", "--job-id", job_id]
    try:
        worker_proc = subprocess.Popen(
            worker_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        now = st.now_ts()
        conn.execute("BEGIN")
        conn.execute(
            """UPDATE jobs
               SET status = 'FAILED', exit_code = -1, finished_at = ?, updated_at = ?
               WHERE job_id = ?""",
            (now, now, job_id),
        )
        conn.execute("COMMIT")
        die(f"failed to launch worker: {exc}", EXIT_ERROR)

    # Important: launching the orchesjob worker is not the same as starting the
    # target command. Keep the job in STARTING here. The worker changes the
    # status to RUNNING only after it has successfully spawned the target and
    # stored target_pid.
    now = st.now_ts()
    conn.execute("BEGIN")
    conn.execute(
        """UPDATE jobs
           SET worker_pid = ?, updated_at = ?
           WHERE job_id = ?
             AND status = 'STARTING'""",
        (worker_proc.pid, now, job_id),
    )
    conn.execute("COMMIT")


def wait_for_job_started(
    conn: sqlite3.Connection,
    job_id: str,
    timeout: float,
    poll_interval: float = 0.05,
) -> Dict[str, Any]:
    """Wait briefly until the target command is identifiable.

    Async start should return a coherent state:
      * RUNNING means target_pid is known and the target command was spawned.
      * Very short commands may reach a terminal status before start returns.
      * If startup takes longer than timeout, return STARTING rather than lying
        with RUNNING + target_pid=None.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        job = read_job(conn, job_id)
        if job["status"] in _TERMINAL_STATUSES:
            return job
        if job["status"] == "RUNNING" and job.get("target_pid") is not None:
            return job

        worker_pid = job.get("worker_pid")
        if worker_pid is not None and not is_process_alive(worker_pid):
            return reconcile_running_job(conn, job_id)

        if time.monotonic() >= deadline:
            return job
        time.sleep(poll_interval)


def cmd_start(args: argparse.Namespace) -> None:
    run_key: str = args.run_key
    command: List[str] = list(args.command)
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
    strict_override_used = False

    try:
        conn.execute("BEGIN EXCLUSIVE")
        now = st.now_ts()
        row = _latest_job_for_run_key(conn, run_key)
        override = _valid_strict_override(conn, run_key, now) if strict else None

        if row is not None and row["status"] not in _TERMINAL_STATUSES:
            existing = True
            job_id = row["job_id"]
            conn.execute("COMMIT")
        elif row is not None and strict and override is None:
            existing = True
            job_id = row["job_id"]
            conn.execute("COMMIT")
        else:
            rerun_of_job_id = row["job_id"] if row is not None else None
            rerun_reason = override["reason"] if override is not None else None
            job_id = _insert_job_locked(
                conn,
                home=home,
                run_key=run_key,
                command=command,
                rerun_of_job_id=rerun_of_job_id,
                rerun_reason=rerun_reason,
            )
            if override is not None:
                strict_override_used = True
                conn.execute(
                    "UPDATE strict_overrides SET used_at = ? WHERE run_key = ? AND used_at IS NULL",
                    (now, run_key),
                )
            conn.execute("COMMIT")
            _launch_worker(conn, job_id)

        if sync_mode:
            job = wait_for_job(conn, job_id)
        else:
            job = wait_for_job_started(conn, job_id, args.start_timeout)
        output_json(
            build_start_response(
                job,
                existing=existing,
                mode=mode,
                strict=strict,
                strict_override_used=strict_override_used,
            )
        )
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        show_running: bool = getattr(args, "running", False)
        show_all: bool = getattr(args, "all", False)
        run_key: Optional[str] = getattr(args, "run_key", None)
        job_id: Optional[str] = getattr(args, "job_id", None)

        if show_running:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status IN ('STARTING','RUNNING') ORDER BY started_at"
            ).fetchall()
            running = []
            for row in rows:
                job = reconcile_running_job(conn, row["job_id"])
                if job["status"] in _ACTIVE_STATUSES:
                    running.append(format_job(job))
            output_json(running)
            return

        if show_all:
            if not run_key:
                die("status --all requires --run-key", EXIT_INVALID_ARGS)
            rows = conn.execute(
                "SELECT * FROM jobs WHERE run_key = ? ORDER BY attempt_no DESC, started_at DESC",
                (run_key,),
            ).fetchall()
            if not rows:
                die(f"No jobs found for run_key: {run_key}", EXIT_NOT_FOUND)
            history = []
            for row in rows:
                job = st.row_to_job(row)
                if job["status"] in _ACTIVE_STATUSES:
                    job = reconcile_running_job(conn, job["job_id"])
                history.append(format_job(job))
            output_json(history)
            return

        resolved_id = resolve_job_id(conn, run_key, job_id)
        job = reconcile_running_job(conn, resolved_id)
        output_json(format_job(job))
    finally:
        conn.close()


def cmd_logs(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        job_id = resolve_job_id(conn, getattr(args, "run_key", None), getattr(args, "job_id", None))
        job = read_job(conn, job_id)
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


def _clean_selection_sql(args: argparse.Namespace) -> Tuple[str, List[Any]]:
    clauses = ["j.status IN ('SUCCEEDED','FAILED','LOST','CANCELLED','ABORTED')"]
    params: List[Any] = []

    if args.job_id:
        validate_job_id(args.job_id)
        clauses.append("j.job_id = ?")
        params.append(args.job_id)
        return " AND ".join(clauses), params

    if args.run_key:
        clauses.append("j.run_key = ?")
        params.append(args.run_key)

    if not args.all:
        if args.before:
            try:
                before_ts = st.parse_datetime_to_ts(args.before)
            except ValueError as exc:
                die(f"Invalid --before: {args.before!r}. {exc}", EXIT_INVALID_ARGS)
            clauses.append("COALESCE(j.finished_at, j.started_at) < ?")
            params.append(before_ts)

        if args.after:
            try:
                after_ts = st.parse_datetime_to_ts(args.after)
            except ValueError as exc:
                die(f"Invalid --after: {args.after!r}. {exc}", EXIT_INVALID_ARGS)
            clauses.append("COALESCE(j.finished_at, j.started_at) >= ?")
            params.append(after_ts)

    return " AND ".join(clauses), params


def cmd_clean(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        where_sql, params = _clean_selection_sql(args)
        rows = conn.execute(
            f"""SELECT j.job_id, j.run_key, j.stdout_file, j.stderr_file,
                       j.finished_at, j.started_at,
                       rk.job_id AS current_job_id
                FROM jobs j
                LEFT JOIN run_keys rk ON rk.run_key = j.run_key AND rk.job_id = j.job_id
                WHERE {where_sql}
                ORDER BY j.started_at""",
            params,
        ).fetchall()

        if not rows:
            output_json({"deleted": 0, "errors": 0})
            return

        deleted = 0
        errors = 0
        items: List[Dict[str, Any]] = []

        for row in rows:
            job_id = row["job_id"]
            run_key = row["run_key"]
            selected_at = row["finished_at"] or row["started_at"]
            is_current = row["current_job_id"] is not None

            item = {
                "job_id": job_id,
                "run_key": run_key,
                "selected_at": selected_at,
                "selected_at_iso": st.timestamp_to_iso(selected_at),
            }
            items.append(item)

            if args.dry_run:
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

        output_json({"deleted": deleted, "errors": errors, "dry_run": args.dry_run, "items": items})
    finally:
        conn.close()


def _terminate_pid(pid: Optional[int], sig: signal.Signals) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        raise
    except OSError:
        return False


def _terminate_process_group_or_pid(pid: Optional[int], sig: signal.Signals) -> bool:
    if pid is None:
        return False
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            raise
        except OSError:
            # Fall back to pid kill below.
            pass
    return _terminate_pid(pid, sig)


def cmd_abort(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        job_id = resolve_job_id(conn, args.run_key, args.job_id)
        reason = args.reason
        now = st.now_ts()

        conn.execute("BEGIN EXCLUSIVE")
        job = read_job(conn, job_id)
        if job["status"] not in _ACTIVE_STATUSES:
            conn.execute("ROLLBACK")
            die(f"Job is not running: {job_id} status={job['status']}", EXIT_INVALID_STATE)

        conn.execute(
            """UPDATE jobs
               SET status = 'ABORTED', finished_at = ?, aborted_at = ?, updated_at = ?, abort_reason = ?
               WHERE job_id = ?
                 AND status IN ('STARTING','RUNNING')""",
            (now, now, now, reason, job_id),
        )
        conn.execute("COMMIT")

        target_pid = job.get("target_pid")
        worker_pid = job.get("worker_pid")
        sent_term_target = _terminate_process_group_or_pid(target_pid, signal.SIGTERM)
        sent_term_worker = _terminate_pid(worker_pid, signal.SIGTERM)

        deadline = time.monotonic() + args.grace_seconds
        while time.monotonic() < deadline:
            target_alive = is_process_alive(target_pid)
            worker_alive = is_process_alive(worker_pid)
            if not target_alive and not worker_alive:
                break
            time.sleep(0.2)

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        sent_kill_target = False
        sent_kill_worker = False
        if is_process_alive(target_pid):
            sent_kill_target = _terminate_process_group_or_pid(target_pid, kill_signal)
        if is_process_alive(worker_pid):
            sent_kill_worker = _terminate_pid(worker_pid, kill_signal)

        job = read_job(conn, job_id)
        result = format_job(job)
        result.update(
            {
                "aborted": True,
                "sent_term_target": sent_term_target,
                "sent_term_worker": sent_term_worker,
                "sent_kill_target": sent_kill_target,
                "sent_kill_worker": sent_kill_worker,
            }
        )
        output_json(result)
    finally:
        conn.close()


def cmd_unlock(args: argparse.Namespace) -> None:
    run_key: str = args.run_key
    ttl_seconds = parse_ttl_to_seconds(args.ttl)
    now = st.now_ts()
    expires_at = now + ttl_seconds if ttl_seconds is not None else None

    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        row = _latest_job_for_run_key(conn, run_key)
        if row is None:
            conn.execute("ROLLBACK")
            die("run_key not found", EXIT_NOT_FOUND)
        if row["status"] not in _TERMINAL_STATUSES:
            conn.execute("ROLLBACK")
            die(f"Cannot unlock active job: status={row['status']}", EXIT_INVALID_STATE)

        conn.execute(
            """INSERT INTO strict_overrides (run_key, allowed_at, used_at, reason, expires_at)
               VALUES (?, ?, NULL, ?, ?)
               ON CONFLICT(run_key) DO UPDATE SET
                   allowed_at = excluded.allowed_at,
                   used_at = NULL,
                   reason = excluded.reason,
                   expires_at = excluded.expires_at""",
            (run_key, now, args.reason, expires_at),
        )
        conn.execute("COMMIT")
        output_json(
            {
                "unlocked": True,
                "run_key": run_key,
                "reason": args.reason,
                "allowed_at": now,
                "allowed_at_iso": st.timestamp_to_iso(now),
                "expires_at": expires_at,
                "expires_at_iso": st.timestamp_to_iso(expires_at),
            }
        )
    finally:
        conn.close()


def cmd_rerun(args: argparse.Namespace) -> None:
    home = st.get_home()
    st.init_db(home)
    conn = st.db_connect(home)
    job_id: Optional[str] = None
    try:
        conn.execute("BEGIN EXCLUSIVE")
        source_job_id = resolve_job_id(conn, args.run_key, args.job_id)
        source = read_job(conn, source_job_id)
        if source["status"] not in _TERMINAL_STATUSES:
            conn.execute("ROLLBACK")
            die(f"Cannot rerun active job: status={source['status']}", EXIT_INVALID_STATE)
        run_key = source.get("run_key")
        if not run_key:
            conn.execute("ROLLBACK")
            die("Cannot rerun a job without run_key", EXIT_INVALID_STATE)

        job_id = _insert_job_locked(
            conn,
            home=home,
            run_key=run_key,
            command=source["command"],
            rerun_of_job_id=source_job_id,
            rerun_reason=args.reason,
        )
        conn.execute("COMMIT")
        _launch_worker(conn, job_id)

        job = wait_for_job(conn, job_id) if args.sync else wait_for_job_started(conn, job_id, args.start_timeout)
        result = build_start_response(
            job,
            existing=False,
            mode="sync" if args.sync else "async",
            strict=False,
        )
        result["rerun"] = True
        output_json(result)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


def cmd_worker(args: argparse.Namespace) -> None:
    from .worker import run_worker

    job_id: str = args.job_id
    validate_job_id(job_id)
    run_worker(job_id)


def add_clean_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p_clean = subparsers.add_parser(
        "clean",
        help="Delete terminal job data",
        description=(
            "Delete terminal job data. STARTING/RUNNING jobs are never deleted.\n\n"
            "Selection rules:\n"
            "  * Specify --all to delete all terminal job data.\n"
            "  * Specify --job-id to delete one terminal job.\n"
            "  * Otherwise, specify at least one of --before or --after.\n"
            "  * --before and --after may be combined as a range.\n"
            "  * --run-key may be combined with --all, --before, or --after.\n"
            "  * --job-id cannot be combined with other selection options."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_clean.add_argument("--before", metavar="DATETIME", help="Delete terminal jobs finished before this datetime")
    p_clean.add_argument("--after", metavar="DATETIME", help="Delete terminal jobs finished at or after this datetime")
    p_clean.add_argument("--all", action="store_true", help="Delete all matching terminal job data")
    p_clean.add_argument("--job-id", metavar="JOB_ID", help="Delete a specific terminal job")
    p_clean.add_argument("--run-key", metavar="RUN_KEY", help="Restrict deletion to a run_key")
    p_clean.add_argument("--dry-run", action="store_true", dest="dry_run", help="Preview without deleting")
    return p_clean


def validate_clean_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.job_id:
        if args.before or args.after or args.all or args.run_key:
            parser.error("clean: --job-id cannot be combined with --before, --after, --all, or --run-key")
        return

    if args.all:
        if args.before or args.after:
            parser.error("clean: --all cannot be combined with --before or --after")
        return

    if not args.before and not args.after:
        parser.error("clean: one of --before, --after, --all, or --job-id is required")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="orchesjob",
        description="Lightweight idempotent one-shot job runner",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- start ---
    p_start = subparsers.add_parser("start", help="Start a job")
    p_start.add_argument("--run-key", required=True, dest="run_key", metavar="RUN_KEY", help="Idempotency key")
    p_start.add_argument("--sync", action="store_true", help="Wait for completion")
    p_start.add_argument(
        "--start-timeout",
        type=float,
        default=10.0,
        help="Seconds async start waits for target_pid or terminal status before returning",
    )
    p_start.add_argument(
        "--strict",
        action="store_true",
        help="Do not create another execution for a completed run_key unless it is unlocked",
    )
    p_start.add_argument("command", nargs=argparse.REMAINDER, help="Command to run (specify after --)")

    # --- status ---
    p_status = subparsers.add_parser("status", help="Get job status")
    g_status = p_status.add_mutually_exclusive_group(required=True)
    g_status.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_status.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    g_status.add_argument("--running", action="store_true", dest="running", help="Show all running jobs")
    p_status.add_argument("--all", action="store_true", dest="all", help="Show all executions for --run-key")

    # --- logs ---
    p_logs = subparsers.add_parser("logs", help="Print job log output")
    g_logs = p_logs.add_mutually_exclusive_group(required=True)
    g_logs.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_logs.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    p_logs.add_argument("--stream", default="stdout", choices=["stdout", "stderr"], help="Log stream")

    # --- clean ---
    p_clean = add_clean_parser(subparsers)
    p_clean.set_defaults(validator=lambda args: validate_clean_args(p_clean, args))

    # --- abort ---
    p_abort = subparsers.add_parser("abort", help="Abort a running job")
    g_abort = p_abort.add_mutually_exclusive_group(required=True)
    g_abort.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_abort.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    p_abort.add_argument("--reason", default=None, help="Abort reason")
    p_abort.add_argument("--grace-seconds", type=float, default=5.0, help="Seconds to wait before SIGKILL")

    # --- unlock ---
    p_unlock = subparsers.add_parser("unlock", help="Allow the next start --strict for a completed run_key")
    p_unlock.add_argument("--run-key", required=True, dest="run_key", metavar="RUN_KEY")
    p_unlock.add_argument("--reason", default=None, help="Reason for strict override")
    p_unlock.add_argument("--ttl", default=None, help="Override TTL, e.g. 30m, 1h, 1d")

    # --- rerun ---
    p_rerun = subparsers.add_parser("rerun", help="Immediately rerun a completed job")
    g_rerun = p_rerun.add_mutually_exclusive_group(required=True)
    g_rerun.add_argument("--run-key", dest="run_key", metavar="RUN_KEY")
    g_rerun.add_argument("--job-id", dest="job_id", metavar="JOB_ID")
    p_rerun.add_argument("--sync", action="store_true", help="Wait for completion")
    p_rerun.add_argument(
        "--start-timeout",
        type=float,
        default=10.0,
        help="Seconds async rerun waits for target_pid or terminal status before returning",
    )
    p_rerun.add_argument("--reason", default=None, help="Rerun reason")

    # --- _worker (internal) ---
    p_worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    p_worker.add_argument("--job-id", required=True, dest="job_id")

    args = parser.parse_args()
    if hasattr(args, "validator"):
        args.validator(args)

    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "logs": cmd_logs,
        "clean": cmd_clean,
        "abort": cmd_abort,
        "unlock": cmd_unlock,
        "rerun": cmd_rerun,
        "_worker": cmd_worker,
    }
    func = dispatch.get(args.subcommand)
    if func is None:
        die(f"Unknown command: {args.subcommand}", EXIT_INVALID_ARGS)
    func(args)


if __name__ == "__main__":
    main()
