# orchesjob

## Overview

`orchesjob` is a lightweight, idempotent one-shot job runner designed for remote orchestration scenarios.

It is intended to be used with external orchestrators such as Apache Airflow, Amazon MWAA, cron, CI/CD pipelines, or SSH-based automation, where a remote job needs to be started, monitored, and safely resumed across retries.

A primary goal of `orchesjob` is to prevent duplicate execution of non-idempotent remote jobs when the orchestrator retries a start operation after SSH failures, timeouts, worker interruptions, or network issues.

## Features

- **Idempotency** — safe to call multiple times with the same run key while a job is active
- **Re-runnable** — finished jobs can be re-triggered under the same run key
- **Rerun** — replay a completed job on demand with `rerun`
- **Abort** — stop a running job gracefully (SIGTERM → SIGKILL) with `abort`
- **Strict mode** — prevent any re-execution under the same run key after completion
- **Strict unlock** — grant a one-time override to strict mode with optional TTL
- **Run history** — all past executions are retained and queryable, with attempt numbers
- **SQLite backend** — fast indexed lookups that stay fast as history grows
- **Sync & async modes** — wait for completion or fire and forget
- **Structured output** — every command prints JSON with both Unix timestamps and ISO 8601 strings

## Requirements

- Python ≥ 3.12
- No third-party dependencies

## Installation

**Recommended — [pipx](https://pipx.pypa.io/) (isolated, globally available CLI):**

```bash
pipx install orchesjob
```

**pip:**

```bash
pip install orchesjob
```

The default state directory is `/var/lib/orchesjob`. Override it with the
`ORCHESJOB_HOME` environment variable:

```bash
export ORCHESJOB_HOME=~/.local/share/orchesjob
```

## Quick Start

```bash
# Start a job (async)
orchesjob start --run-key nightly-backup -- /usr/local/bin/backup.sh

# Start a job and wait for it to finish
orchesjob start --run-key nightly-backup --sync -- /usr/local/bin/backup.sh

# Check the current status
orchesjob status --run-key nightly-backup

# List all currently running jobs
orchesjob status --running

# Print stdout
orchesjob logs --run-key nightly-backup --stream stdout

# Abort a running job
orchesjob abort --run-key nightly-backup --reason "manual intervention"

# Rerun a completed job immediately
orchesjob rerun --run-key nightly-backup --sync
```

## Commands

### `start`

Start a job or return the existing one if it is still running.

```
orchesjob start --run-key KEY [--sync] [--strict] [--start-timeout SECS] [--] COMMAND [ARGS...]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Idempotency key (required) |
| `--sync` | Block until the job finishes |
| `--strict` | One execution per run key, ever — see below |
| `--start-timeout SECS` | Seconds async start waits for `target_pid` before returning (default: 10) |
| `--` | Separator between orchesjob flags and the command |

**Idempotency rules:**

| Existing job state | Default behaviour | With `--strict` |
|--------------------|-------------------|-----------------|
| `RUNNING` / `STARTING` | Returns the existing job | Returns the existing job |
| Terminal (`SUCCEEDED`, `FAILED`, `LOST`, `CANCELLED`, `ABORTED`) | Starts a new job | Returns the existing job |
| None | Starts a new job | Starts a new job |

#### Strict idempotency

By default, orchesjob provides active-execution idempotency: repeated `start`
calls with the same `run_key` return the existing job only while it is
`STARTING` or `RUNNING`.

Use `--strict` when the same `run_key` must never create more than one physical
execution, even after the previous job has already reached a terminal state.
This is useful when the run key already encodes uniqueness (e.g. a date or
event ID) and re-triggering would be a bug.

```bash
orchesjob start --run-key daily-import-2026-05-02 --strict -- /jobs/import.sh
```

Use `unlock` to grant a one-time exception for a completed strict run key.

**Example output:**

```json
{
  "accepted": true,
  "existing": false,
  "mode": "sync",
  "strict": false,
  "strict_override_used": false,
  "job_id": "3f2a1b4c-...",
  "run_key": "nightly-backup",
  "command": ["/usr/local/bin/backup.sh"],
  "pid": 12345,
  "pid_kind": "target",
  "worker_pid": 12344,
  "target_pid": 12345,
  "status": "SUCCEEDED",
  "exit_code": 0,
  "stdout_file": "/var/lib/orchesjob/logs/3f2a1b4c-....stdout",
  "stderr_file": "/var/lib/orchesjob/logs/3f2a1b4c-....stderr",
  "attempt_no": 1,
  "rerun_of_job_id": null,
  "rerun_reason": null,
  "abort_reason": null,
  "started_at": 1746032400,
  "started_at_iso": "2026-05-01T02:00:00+09:00",
  "finished_at": 1746032742,
  "finished_at_iso": "2026-05-01T02:05:42+09:00",
  "updated_at": 1746032742,
  "updated_at_iso": "2026-05-01T02:05:42+09:00",
  "aborted_at": null,
  "aborted_at_iso": null
}
```

### `status`

Get the current status of a job, or the full run history for a run key.

```
orchesjob status (--run-key KEY | --job-id ID | --running) [--all]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Look up by run key |
| `--job-id ID` | Look up by job ID |
| `--running` | List all jobs currently in `STARTING` or `RUNNING` state |
| `--all` | Return all past executions for the run key as a JSON array (requires `--run-key`) |

Without `--all`, returns a single JSON object for the most recent job.
With `--all`, returns a JSON array ordered by `attempt_no` descending.
With `--running`, returns a JSON array of all active jobs.

### `logs`

Print the stdout or stderr of a job.

```
orchesjob logs (--run-key KEY | --job-id ID) [--stream stdout|stderr]
```

| Flag | Description |
|------|-------------|
| `--stream stdout` | Print stdout (default) |
| `--stream stderr` | Print stderr |

### `clean`

Delete terminal jobs finished before a given point in time, along with their
log files. Jobs that are currently `RUNNING` or `STARTING` are never deleted.

```
orchesjob clean (--before DATETIME | --after DATETIME | --all | --job-id ID) [--run-key KEY] [--dry-run]
```

| Flag | Description |
|------|-------------|
| `--before DATETIME` | Delete terminal jobs finished before this datetime |
| `--after DATETIME` | Delete terminal jobs finished at or after this datetime |
| `--all` | Delete all terminal job data |
| `--job-id ID` | Delete one specific terminal job |
| `--run-key KEY` | Restrict deletion to a specific run key (combine with `--before`, `--after`, or `--all`) |
| `--dry-run` | Print what would be deleted without making any changes |

`--before` and `--after` may be combined as a date range.
`--job-id` cannot be combined with other selection options.
Times without a timezone offset are interpreted as local time.

**Examples:**

```bash
# Delete all finished jobs from before 2026-01-01 (local time)
orchesjob clean --before 2026-01-01

# Delete jobs in a date range
orchesjob clean --after 2026-01-01 --before 2026-02-01

# Delete all terminal data for one run key
orchesjob clean --run-key daily-import-2026-05-02 --all

# Delete a specific job
orchesjob clean --job-id 3f2a1b4c-...

# Preview what would be removed
orchesjob clean --before "$(date -d '7 days ago' -Iseconds)" --dry-run
```

**Output:**

```json
{
  "deleted": 3,
  "errors": 0,
  "dry_run": false,
  "items": [
    {
      "job_id": "3f2a1b4c-...",
      "run_key": "nightly-backup",
      "selected_at": 1746032742,
      "selected_at_iso": "2026-05-01T02:05:42+09:00"
    }
  ]
}
```

### `abort`

Stop a running job. Sends SIGTERM to the target process group, waits for a
grace period, then sends SIGKILL if the process is still alive.

```
orchesjob abort (--run-key KEY | --job-id ID) [--reason TEXT] [--grace-seconds SECS]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Abort job identified by run key |
| `--job-id ID` | Abort job identified by job ID |
| `--reason TEXT` | Abort reason (stored in the job record) |
| `--grace-seconds SECS` | Seconds to wait between SIGTERM and SIGKILL (default: 5) |

The job status is set to `ABORTED` in the database before signals are sent, so
subsequent `start` calls with `--strict` will see the key as consumed.

**Example output:**

```json
{
  "job_id": "3f2a1b4c-...",
  "run_key": "nightly-backup",
  "status": "ABORTED",
  "abort_reason": "manual intervention",
  "aborted": true,
  "sent_term_target": true,
  "sent_term_worker": true,
  "sent_kill_target": false,
  "sent_kill_worker": false,
  ...
}
```

### `unlock`

Grant a one-time override so the next `start --strict` for a completed run key
creates a new execution instead of returning the existing one. The override is
consumed on use and can optionally expire.

```
orchesjob unlock --run-key KEY [--reason TEXT] [--ttl DURATION]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Run key to unlock (required) |
| `--reason TEXT` | Reason for the override (stored in the job record) |
| `--ttl DURATION` | Override expiry: integer seconds, or a suffix `s`, `m`, `h`, `d` (e.g. `30m`, `2h`) |

The run key must have a terminal job before it can be unlocked.

**Example:**

```bash
# Allow one re-execution within the next 30 minutes
orchesjob unlock --run-key daily-import-2026-05-02 --reason "data fix" --ttl 30m

# Then trigger the re-run
orchesjob start --run-key daily-import-2026-05-02 --strict -- /jobs/import.sh
```

**Example output:**

```json
{
  "unlocked": true,
  "run_key": "daily-import-2026-05-02",
  "reason": "data fix",
  "allowed_at": 1746032400,
  "allowed_at_iso": "2026-05-01T02:00:00+09:00",
  "expires_at": 1746034200,
  "expires_at_iso": "2026-05-01T02:30:00+09:00"
}
```

### `rerun`

Immediately start a new execution of a completed job, reusing its command.
Unlike `start`, `rerun` always creates a new execution regardless of strict mode.

```
orchesjob rerun (--run-key KEY | --job-id ID) [--sync] [--reason TEXT] [--start-timeout SECS]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Rerun by run key |
| `--job-id ID` | Rerun a specific job |
| `--sync` | Block until the new job finishes |
| `--reason TEXT` | Rerun reason (stored in the job record) |
| `--start-timeout SECS` | Seconds async rerun waits for `target_pid` before returning (default: 10) |

The source job must be in a terminal state. The new job records `rerun_of_job_id`
and `rerun_reason` for traceability, and its `attempt_no` is incremented.

**Example:**

```bash
orchesjob rerun --run-key nightly-backup --sync --reason "retry after disk error"
```

## Job Statuses

| Status | Description |
|--------|-------------|
| `STARTING` | Job record created; worker process not yet confirmed running |
| `RUNNING` | Worker is executing the command |
| `SUCCEEDED` | Command exited with code 0 |
| `FAILED` | Command exited with a non-zero code, or failed to launch |
| `LOST` | Worker process disappeared without writing a result |
| `CANCELLED` | Job was cancelled (reserved for future use) |
| `ABORTED` | Job was stopped via the `abort` command |

## State Directory Layout

```
$ORCHESJOB_HOME/
├── orchesjob.db      # SQLite database (run keys + job metadata)
└── logs/
    ├── <job-id>.stdout
    └── <job-id>.stderr
```

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments |
| 3 | Job / run key not found |
| 4 | Inconsistent internal state |
| 5 | Lock error |

## License

MIT — Copyright (c) 2026 Ryosuke Muraki
