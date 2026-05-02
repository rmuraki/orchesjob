# orchesjob

A lightweight, idempotent one-shot job runner backed by SQLite.

Submit a command with a **run key**. If a job for that key is already running,
orchesjob returns the existing job instead of starting a duplicate. Once the job
finishes, the same run key can be reused to start a fresh execution — keeping
the full history of past runs intact.

## Features

- **Idempotency** — safe to call multiple times with the same run key while a job is active
- **Re-runnable** — finished jobs can be re-triggered under the same run key
- **Run history** — all past executions are retained and queryable
- **SQLite backend** — fast indexed lookups that stay fast as history grows
- **Sync & async modes** — wait for completion or fire and forget
- **Structured output** — every command prints JSON

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

# Print stdout
orchesjob logs --run-key nightly-backup --stream stdout
```

## Commands

### `start`

Start a job or return the existing one if it is still running.

```
orchesjob start --run-key KEY [--sync] [--] COMMAND [ARGS...]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Idempotency key (required) |
| `--sync` | Block until the job finishes |
| `--` | Separator between orchesjob flags and the command |

**Idempotency rules:**

| Existing job state | Behaviour |
|--------------------|-----------|
| `RUNNING` / `STARTING` | Returns the existing job (`"existing": true`) |
| Terminal (`SUCCEEDED`, `FAILED`, `LOST`, `CANCELLED`) | Starts a new job |
| None | Starts a new job |

**Example output:**

```json
{
  "accepted": true,
  "existing": false,
  "mode": "sync",
  "run_key": "nightly-backup",
  "job_id": "3f2a1b4c-...",
  "pid": 12345,
  "command": ["/usr/local/bin/backup.sh"],
  "status": "SUCCEEDED",
  "stdout_file": "/var/lib/orchesjob/logs/3f2a1b4c-....stdout",
  "stderr_file": "/var/lib/orchesjob/logs/3f2a1b4c-....stderr",
  "exit_code": 0,
  "started_at": "2026-05-01T02:00:00.123456+09:00",
  "finished_at": "2026-05-01T02:05:42.654321+09:00"
}
```

### `status`

Get the current status of a job, or the full run history for a run key.

```
orchesjob status (--run-key KEY | --job-id ID) [--all]
```

| Flag | Description |
|------|-------------|
| `--run-key KEY` | Look up by run key |
| `--job-id ID` | Look up by job ID |
| `--all` | Return all past executions for the run key as a JSON array (requires `--run-key`) |

Without `--all`, returns a single JSON object for the most recent job.
With `--all`, returns a JSON array ordered by `started_at` descending.

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
orchesjob clean --before DATETIME [--dry-run]
```

| Flag | Description |
|------|-------------|
| `--before DATETIME` | ISO 8601 datetime cutoff. Times without a timezone offset are interpreted as local time. |
| `--dry-run` | Print what would be deleted to stderr without making any changes |

**Examples:**

```bash
# Delete all finished jobs from before 2026-01-01 (local time)
orchesjob clean --before 2026-01-01

# Preview what would be removed from the last 7 days
orchesjob clean --before "$(date -d '7 days ago' -Iseconds)" --dry-run
```

**Output:**

```json
{ "deleted": 42, "errors": 0 }
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
