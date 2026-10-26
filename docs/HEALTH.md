# Computer health: `ping`, `health`, `cleanup`

Every Son runs on its own computer. These three tools let the owner (or Claude / ChatGPT, voice mode included)
check that computer and fix it mid-conversation. The aiteam.space dashboard reads the same endpoint for its graphs.

| Tool | Box endpoint | What it does |
| --- | --- | --- |
| `ping` | `GET /v1/ping` | Is the computer up. One line: "Up for 19 days." |
| `health` | `GET /v1/health` (`?history=1` adds 24 h of samples) | Disk, memory, swap, CPU, uptime, live sessions, scheduled jobs (last run + result), last cleanup, one status `ok` / `warning` / `critical`, and a one-sentence `summary` made to be read out. |
| (graphs) | `GET /v1/health/history` | 24 hours of 5-minute samples (288 points): disk %, memory %, swap %, load, CPU %, status. |
| `cleanup` | `POST /v1/cleanup` | Runs the fixed safe cleanup. No parameters. At most once every 10 minutes per box, one at a time. |

All four endpoints use the same caller-token check as `/v1/sons` (401 without a valid token). `/healthz` stays the
only unauthenticated route and says nothing about the computer.

## Status rules

| Check | Warning | Critical |
| --- | --- | --- |
| Disk used (any watched disk) | 80% | 95% |
| Memory available | 15% or less | 5% or less |
| Swap used (only when the box has swap) | 80% | |
| Load per CPU core (1 min) | 2.0 | |
| A scheduled job failing 3 runs in a row | yes | |
| The host disk guard's status file says so | warning | critical |

Disk thresholds match the fleet disk guard (alert 80%, clean 85%, critical 95%). Override any of them in box.json.

## How it reads the computer

`/proc/meminfo`, `/proc/loadavg`, `/proc/uptime`, `/proc/stat` (two reads 0.25 s apart for CPU %), `statvfs` on each
watched disk, `<HERMES_HOME>/cron/jobs.json`, and Hermes `/api/sessions` (sessions active in the last 15 minutes).
Nothing shells out on a read. `memory.guard` says whether a memory guard (earlyoom, systemd-oomd, nohang) is running.

**Containers.** When the box runs inside a container with a memory limit below the host's RAM (a customer Son on
its plan cap), memory and swap are read from the container's cgroup v2 files (`memory.max`, `memory.current` minus
reclaimable `inactive_file`, `memory.swap.*`), the same way `docker stats` counts them, and `memory.limit` is
`"container"`. Without a limit it reads `/proc/meminfo` and `memory.limit` is `"host"`. So a Son at 1.9 GB of a 2 GB
plan shows as critical even when the host has plenty free.
Samples are written by the box every 5 minutes to `<state_dir>/samples.jsonl`, so a restart keeps the graph.

## Safe cleanup

Default command: `python -m aiteam_connector.safe_cleanup --root <HERMES_HOME>`. It removes only files that can be
rebuilt, each untouched for 2 hours or more:

- build caches: `node_modules`, `.next`, `.turbo`, `.pytest_cache`, `__pycache__`, `.parcel-cache`, `.vite`, skipped
  when a running process works inside that project or when git tracks the folder
- package caches: `~/.npm/_npx`, `~/.npm/_cacache`, `~/.cache/pip`, `~/.cache/uv`
- media, logs and archives older than 6 hours in the scratch folders (`cache/scratch`, `tmp`)

Never touched: memory, sessions, skills, plugins, add-ons, scheduled jobs, settings, `.env`, credentials, meetings,
the MCP gateway, git-tracked files. `--dry-run` lists what it would remove.

The command comes from box.json only (an argv list, never a shell string), never from the request. A request body
is ignored.

## Config (`"health"` in box.json, all optional)

```json
{"health": {"hermes_home": "<HERMES_HOME>", "disks": ["<HERMES_HOME>"],
            "guard_status_file": null, "cleanup_command": null, "cgroup": "/sys/fs/cgroup", "cleanup_min_interval_s": 600,
            "sample_interval_s": 300, "sample_keep": 288,
            "thresholds": {"disk_warn": 80, "disk_crit": 95, "mem_free_warn": 15, "mem_free_crit": 5}}}
```

## Example answer (`health`, first line)

    Healthy. Disk 66% used, memory 48% free.
    Needs attention: disk 91% full. Disk 91% used, memory 48% free.
