"""Live health of the computer a Son runs on: disk, memory, swap, CPU, uptime, sessions, scheduled jobs, cleanup.

Served by the messaging box (``GET /v1/ping``, ``GET /v1/health``, ``POST /v1/cleanup``, same caller-token check as
``/v1/sons``) and exposed on the connector as the ``ping`` / ``health`` / ``cleanup`` tools, so Claude or ChatGPT
(voice included) can check the computer and fix it mid-conversation. The dashboard reads the same endpoint.

Design rules:
* Reads ``/proc`` and small JSON files only. Nothing shells out on a read.
* One overall status: ``ok`` / ``warning`` / ``critical``, plus a one-sentence ``summary`` that reads well aloud.
* 24 hours of 5-minute samples (288 points) kept in ``<state_dir>/samples.jsonl`` so restarts keep the graph.
* Cleanup runs ONE fixed command from the box config (never anything from the request), at most once every
  10 minutes per box, and only one at a time. The default command is ``safe_cleanup.py`` in this package, which
  only removes files that can be rebuilt (caches, old scratch media and logs, stale node_modules in scratch).

Config (``"health"`` in box.json, every key optional):
  {"hermes_home": "~/.hermes", "disks": ["~/.hermes"], "state_dir": "<hermes_home>/health",
   "guard_status_file": null, "cleanup_command": null, "cgroup": "/sys/fs/cgroup", "cleanup_min_interval_s": 600, "cleanup_timeout_s": 1200,
   "sample_interval_s": 300, "sample_keep": 288,
   "thresholds": {"disk_warn": 80, "disk_crit": 95, "mem_free_warn": 15, "mem_free_crit": 5,
                  "swap_warn": 80, "load_warn": 2.0}}
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

RANK = {"ok": 0, "warning": 1, "critical": 2}
DEFAULT_THRESHOLDS = {"disk_warn": 80.0, "disk_crit": 95.0, "mem_free_warn": 15.0, "mem_free_crit": 5.0,
                      "swap_warn": 80.0, "load_warn": 2.0}
CLEANUP_JOB_RX = re.compile(r"clean|disk-pressure|disk-guard", re.I)
LIVE_WINDOW_S = 15 * 60


@dataclass
class HealthConfig:
    hermes_home: str = field(default_factory=lambda: os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    disks: list[str] = field(default_factory=list)
    state_dir: str = ""
    guard_status_file: Optional[str] = None
    cleanup_command: Optional[list[str]] = None
    cleanup_min_interval_s: int = 600
    cleanup_timeout_s: int = 1200
    sample_interval_s: int = 300
    sample_keep: int = 288
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    proc: str = "/proc"  # tests point this at a fake tree
    cgroup: str = "/sys/fs/cgroup"  # cgroup v2 root as seen from inside the container

    def __post_init__(self) -> None:
        if not self.disks:
            self.disks = [self.hermes_home if os.path.isdir(self.hermes_home) else "/"]
        if not self.state_dir:
            self.state_dir = os.path.join(self.hermes_home, "health")
        self.thresholds = {**DEFAULT_THRESHOLDS, **(self.thresholds or {})}
        if self.cleanup_command is None:
            self.cleanup_command = [sys.executable, "-m", "aiteam_connector.safe_cleanup", "--root", self.hermes_home]
        if not (isinstance(self.cleanup_command, list) and self.cleanup_command
                and all(isinstance(a, str) for a in self.cleanup_command)):
            raise ValueError("health.cleanup_command must be a non-empty list of strings (argv, no shell)")

    @classmethod
    def from_raw(cls, raw: Optional[dict]) -> "HealthConfig":
        raw = dict(raw or {})
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)


# --------------------------------------------------------------------------------------------- readers
def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def meminfo(proc: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in _read(f"{proc}/meminfo").splitlines():
        k, _, rest = ln.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[k.strip()] = int(parts[0]) * 1024  # kB -> bytes
    return out


def _cg_int(path: str) -> Optional[int]:
    """A cgroup v2 value file as an int; None for "max", missing or unreadable."""
    try:
        v = _read(path).strip()
    except OSError:
        return None
    return int(v) if v.isdigit() else None


def container_memory(cg: str, host_total: int) -> Optional[dict[str, int]]:
    """The container's own memory limit (cgroup v2), when one is set below the host's RAM.

    A Son runs in a container capped by its plan, so /proc/meminfo (the whole host) overstates what it may use: the
    kernel kills it at memory.max, not at host RAM. Used = memory.current minus reclaimable page cache
    (inactive_file), the same way `docker stats` counts it. None = no limit set: report the host.
    """
    limit = _cg_int(f"{cg}/memory.max")
    current = _cg_int(f"{cg}/memory.current")
    if not limit or current is None or (host_total and limit >= host_total):
        return None
    inactive = 0
    try:
        for ln in _read(f"{cg}/memory.stat").splitlines():
            k, _, v = ln.partition(" ")
            if k == "inactive_file" and v.strip().isdigit():
                inactive = int(v)
                break
    except OSError:
        pass
    used = max(0, min(limit, current - inactive))
    out = {"MemTotal": limit, "MemAvailable": limit - used}
    swap_max, swap_cur = _cg_int(f"{cg}/memory.swap.max"), _cg_int(f"{cg}/memory.swap.current")
    if swap_max is not None and swap_cur is not None:
        out.update(SwapTotal=swap_max, SwapFree=max(0, swap_max - swap_cur))
    return out


def cpu_times(proc: str) -> tuple[int, int]:
    """(busy, total) jiffies from the aggregate cpu line."""
    first = _read(f"{proc}/stat").splitlines()[0].split()
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals) - idle, sum(vals)


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def memory_guard_running(proc: str) -> bool:
    try:
        for pid in os.listdir(proc):
            if pid.isdigit():
                try:
                    if _read(f"{proc}/{pid}/comm").strip() in ("earlyoom", "systemd-oomd", "nohang"):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def _iso(ts: Any) -> Optional[str]:
    return ts if isinstance(ts, str) and ts else None


def cron_jobs(hermes_home: str) -> list[dict]:
    try:
        raw = json.loads(_read(os.path.join(hermes_home, "cron", "jobs.json")))
    except (OSError, ValueError):
        return []
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict):
            continue
        sched = j.get("schedule")
        err = j.get("last_error") or j.get("last_delivery_error")
        out.append({
            "id": j.get("id"), "name": j.get("name") or j.get("id"),
            "enabled": bool(j.get("enabled", True)) and j.get("state") not in ("paused", "completed"),
            "schedule": j.get("schedule_display") or (sched.get("display") if isinstance(sched, dict) else sched),
            "last_run_at": _iso(j.get("last_run_at")), "last_status": j.get("last_status"),
            "last_error": str(err)[:200] if err else None, "failure_streak": int(j.get("failure_streak") or 0),
            "next_run_at": _iso(j.get("next_run_at")),
        })
    return out


def _load_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    try:
        d = json.loads(_read(path))
        return d if isinstance(d, dict) else {"value": d}
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------- status
def _pct(a: float, b: float) -> float:
    return round(a * 100.0 / b, 1) if b else 0.0


def _gb(n: float) -> float:
    return round(n / 1e9, 1)


def _worst(*levels: str) -> str:
    return max(levels, key=lambda s: RANK[s]) if levels else "ok"


def human_duration(s: float) -> str:
    s = int(s)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d} day{'s' if d != 1 else ''}" + (f" {h} h" if h else "")
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def evaluate(snap: dict, t: dict[str, float]) -> tuple[str, list[str]]:
    """Overall status + the reasons, worst first. Pure function (tests pin the thresholds)."""
    reasons: list[tuple[str, str]] = []
    for d in snap["disk"]:
        if d["used_pct"] >= t["disk_crit"]:
            reasons.append(("critical", f"disk {d['used_pct']:.0f}% full"))
        elif d["used_pct"] >= t["disk_warn"]:
            reasons.append(("warning", f"disk {d['used_pct']:.0f}% full"))
    m = snap["memory"]
    if m["available_pct"] <= t["mem_free_crit"]:
        reasons.append(("critical", f"memory almost out ({m['available_pct']:.0f}% free)"))
    elif m["available_pct"] <= t["mem_free_warn"]:
        reasons.append(("warning", f"memory low ({m['available_pct']:.0f}% free)"))
    s = snap["swap"]
    if s["total_bytes"] and s["used_pct"] >= t["swap_warn"]:
        reasons.append(("warning", f"swap {s['used_pct']:.0f}% used"))
    c = snap["cpu"]
    if c["load_per_core"] >= t["load_warn"]:
        reasons.append(("warning", f"CPU overloaded (load {c['load_1m']:.1f} on {c['cores']} cores)"))
    bad = [j["name"] for j in snap["cron"]["jobs"] if j["enabled"] and (j["failure_streak"] >= 3)]
    if bad:
        reasons.append(("warning", f"{len(bad)} scheduled job{'s' if len(bad) > 1 else ''} failing ({', '.join(bad[:3])})"))
    g = snap.get("guard") or {}
    gs = str(g.get("status") or "").lower()
    if gs in ("warning", "critical"):
        reasons.append((gs, f"disk guard reports {gs}"))
    reasons.sort(key=lambda r: -RANK[r[0]])
    return _worst(*(r[0] for r in reasons)) if reasons else "ok", [r[1] for r in reasons]


def summary_line(status: str, reasons: list[str], snap: dict) -> str:
    """One sentence for voice mode, e.g. 'Healthy. Disk 66% used, memory 48% free.'"""
    disk = max(snap["disk"], key=lambda d: d["used_pct"]) if snap["disk"] else None
    facts = []
    if disk:
        facts.append(f"disk {disk['used_pct']:.0f}% used")
    facts.append(f"memory {snap['memory']['available_pct']:.0f}% free")
    lead = {"ok": "Healthy", "warning": "Needs attention", "critical": "Critical"}[status]
    if reasons:
        return f"{lead}: {'; '.join(reasons)}. " + (", ".join(facts)).capitalize() + "."
    return f"{lead}. " + ", ".join(facts).capitalize() + "."


# --------------------------------------------------------------------------------------------- monitor
class HostHealth:
    def __init__(self, cfg: HealthConfig, clock: Callable[[], float] = time.time,
                 runner: Optional[Callable[..., Any]] = None):
        self.cfg = cfg
        self.clock = clock
        self._runner = runner  # tests inject a fake cleanup runner (async, returns (rc, stdout))
        self._cleanup_task: Optional[asyncio.Task] = None
        self._sampler: Optional[asyncio.Task] = None
        self._last_cpu: Optional[tuple[int, int]] = None

    def _ensure_state(self) -> None:
        os.makedirs(self.cfg.state_dir, exist_ok=True)

    # --- files
    @property
    def samples_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "samples.jsonl")

    @property
    def cleanup_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "last_cleanup.json")

    # --- snapshot
    async def cpu_pct(self, sample_s: float = 0.25) -> Optional[float]:
        try:
            a = cpu_times(self.cfg.proc)
            await asyncio.sleep(sample_s)
            b = cpu_times(self.cfg.proc)
        except (OSError, IndexError, ValueError):
            return None
        busy, total = b[0] - a[0], b[1] - a[1]
        return round(busy * 100.0 / total, 1) if total > 0 else 0.0

    def static_snapshot(self) -> dict:
        p = self.cfg.proc
        mi = meminfo(p)
        cg = container_memory(self.cfg.cgroup, mi.get("MemTotal", 0))
        scope = "container" if cg else "host"
        if cg:
            mi = {**mi, **cg}
        mt, ma = mi.get("MemTotal", 0), mi.get("MemAvailable", mi.get("MemFree", 0))
        st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        load = [float(x) for x in _read(f"{p}/loadavg").split()[:3]]
        up = float(_read(f"{p}/uptime").split()[0])
        cores = cpu_count()
        disks = []
        for path in self.cfg.disks:
            try:
                u = shutil.disk_usage(path)
            except OSError:
                continue
            disks.append({"path": path, "total_bytes": u.total, "used_bytes": u.used, "free_bytes": u.free,
                          "used_pct": _pct(u.used, u.total), "total_gb": _gb(u.total), "free_gb": _gb(u.free)})
        jobs = cron_jobs(self.cfg.hermes_home)
        return {
            "disk": disks,
            "memory": {"total_bytes": mt, "available_bytes": ma, "used_pct": _pct(mt - ma, mt),
                       "available_pct": _pct(ma, mt), "total_gb": _gb(mt), "available_gb": _gb(ma),
                       "guard": "on" if memory_guard_running(p) else "off", "limit": scope},
            "swap": {"total_bytes": st, "used_bytes": st - sf, "used_pct": _pct(st - sf, st), "total_gb": _gb(st)},
            "cpu": {"cores": cores, "load_1m": load[0], "load_5m": load[1], "load_15m": load[2],
                    "load_per_core": round(load[0] / cores, 2), "busy_pct": None},
            "uptime": {"seconds": int(up), "text": human_duration(up)},
            "cron": {"total": len(jobs), "enabled": sum(1 for j in jobs if j["enabled"]),
                     "failing": sum(1 for j in jobs if j["enabled"] and j["failure_streak"] >= 3),
                     "jobs": jobs},
            "guard": _load_json(self.cfg.guard_status_file),
        }

    def last_cleanup(self, jobs: list[dict]) -> Optional[dict]:
        """Newest of: our own cleanup run record, and the last run of any cleanup-type scheduled job."""
        cands = []
        own = _load_json(self.cleanup_path)
        if own and own.get("finished_at"):
            cands.append({**own, "source": "cleanup tool"})
        for j in jobs:
            if j["last_run_at"] and CLEANUP_JOB_RX.search(str(j["name"])):
                cands.append({"finished_at": j["last_run_at"], "ok": j["last_status"] in ("ok", "success", None),
                              "source": f"scheduled job: {j['name']}"})
        return max(cands, key=lambda c: _ts(c["finished_at"])) if cands else None

    async def snapshot(self, sessions: Optional[dict] = None, with_cpu: bool = True) -> dict:
        snap = self.static_snapshot()
        if with_cpu:
            snap["cpu"]["busy_pct"] = await self.cpu_pct()
        snap["sessions"] = sessions or {"live": None}
        snap["last_cleanup"] = self.last_cleanup(snap["cron"]["jobs"])
        status, reasons = evaluate(snap, self.cfg.thresholds)
        snap["status"], snap["reasons"] = status, reasons
        snap["summary"] = summary_line(status, reasons, snap)
        snap["checked_at"] = _now_iso(self.clock())
        snap["thresholds"] = self.cfg.thresholds
        snap["cleanup_running"] = bool(self._cleanup_task and not self._cleanup_task.done())
        return snap

    # --- history (24 h of 5-minute samples)
    def history(self) -> list[dict]:
        try:
            lines = _read(self.samples_path).splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-self.cfg.sample_keep:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def record_sample(self, snap: dict) -> dict:
        disk = max(snap["disk"], key=lambda d: d["used_pct"])["used_pct"] if snap["disk"] else None
        row = {"t": int(self.clock()), "disk": disk, "mem": snap["memory"]["used_pct"],
               "swap": snap["swap"]["used_pct"], "load": snap["cpu"]["load_1m"], "cpu": snap["cpu"].get("busy_pct"),
               "status": snap["status"]}
        rows = self.history()[-(self.cfg.sample_keep - 1):] + [row]
        self._ensure_state()
        tmp = self.samples_path + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        os.replace(tmp, self.samples_path)
        return row

    async def sample_once(self) -> dict:
        snap = await self.snapshot(with_cpu=True)
        return self.record_sample(snap)

    async def _sample_loop(self) -> None:
        while True:
            try:
                await self.sample_once()
            except Exception:  # noqa: BLE001 - a bad read must never kill the box
                pass
            await asyncio.sleep(self.cfg.sample_interval_s)

    def start_sampler(self) -> None:
        if self._sampler is None or self._sampler.done():
            self._sampler = asyncio.get_running_loop().create_task(self._sample_loop())

    async def stop(self) -> None:
        if self._sampler:
            self._sampler.cancel()
            with _suppress():
                await self._sampler

    # --- cleanup
    def cleanup_wait_s(self) -> int:
        own = _load_json(self.cleanup_path) or {}
        started = _ts(own.get("started_at")) if own.get("started_at") else 0.0
        left = self.cfg.cleanup_min_interval_s - (self.clock() - started)
        return max(0, int(left + 0.999)) if started else 0

    async def _run_command(self) -> tuple[int, str]:
        if self._runner is not None:
            return await self._runner(self.cfg.cleanup_command)
        cmd = list(self.cfg.cleanup_command or [])
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.cleanup_timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return -9, "timed out"
        return proc.returncode or 0, out.decode(errors="replace")

    def _disk_used(self) -> dict[str, float]:
        out = {}
        for p in self.cfg.disks:
            with _suppress():
                u = shutil.disk_usage(p)
                out[p] = u.used
        return out

    async def _cleanup_job(self, started: float) -> dict:
        before = self._disk_used()
        try:
            rc, out = await self._run_command()
        except Exception as e:  # noqa: BLE001
            rc, out = -1, f"{type(e).__name__}: {e}"
        after = self._disk_used()
        freed = sum(max(0, before[p] - after.get(p, before[p])) for p in before)
        rec = {"started_at": _now_iso(started), "finished_at": _now_iso(self.clock()), "ok": rc == 0,
               "exit_code": rc, "freed_bytes": int(freed), "freed_gb": _gb(freed), "output": out[-2000:]}
        self._write_cleanup(rec)
        return rec

    def _write_cleanup(self, rec: dict) -> None:
        self._ensure_state()
        tmp = self.cleanup_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, self.cleanup_path)

    async def cleanup(self, wait_s: float = 25.0) -> tuple[int, dict]:
        """Run the fixed safe cleanup. Returns (http_status, body). 429 inside the 10-minute window, 409 while one
        runs, 202 if it is still running after ``wait_s`` (it keeps going; ``health`` shows the result later)."""
        if self._cleanup_task and not self._cleanup_task.done():
            return 409, {"status": "running", "message": "A cleanup is already running. Check health in a minute."}
        wait = self.cleanup_wait_s()
        if wait:
            return 429, {"status": "too_soon", "retry_after_s": wait,
                         "message": f"Cleanup ran less than {self.cfg.cleanup_min_interval_s // 60} minutes ago. "
                                    f"Try again in {human_duration(wait) if wait >= 60 else f'{wait} s'}."}
        started = self.clock()
        self._write_cleanup({"started_at": _now_iso(started), "finished_at": None, "ok": None})
        self._cleanup_task = asyncio.get_running_loop().create_task(self._cleanup_job(started))
        done, _ = await asyncio.wait({self._cleanup_task}, timeout=max(0.0, wait_s))
        if not done:
            return 202, {"status": "running", "message": "Cleanup started and is still running. Check health in a few minutes."}
        rec = self._cleanup_task.result()
        msg = (f"Cleanup done. Freed {rec['freed_gb']} GB." if rec["ok"] else
               f"Cleanup finished with an error (exit {rec['exit_code']}).")
        return 200, {"status": "done" if rec["ok"] else "error", "message": msg, **rec}


# --------------------------------------------------------------------------------------------- helpers
class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], (Exception, asyncio.CancelledError))


def _now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _ts(iso: Any) -> float:
    if not isinstance(iso, str) or not iso:
        return 0.0
    s = iso.replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def live_sessions(rows: list[dict], now: float, window_s: int = LIVE_WINDOW_S) -> dict:
    """Count sessions active in the last 15 minutes from Hermes ``/api/sessions`` rows."""
    def active(r: dict) -> bool:
        la = r.get("last_active")
        t = float(la) if isinstance(la, (int, float)) else _ts(la)
        return bool(t) and now - t <= window_s and not r.get("ended_at") and not r.get("archived")
    live = [r for r in rows if active(r)]
    by_source: dict[str, int] = {}
    for r in live:
        k = str(r.get("source") or "other")
        by_source[k] = by_source.get(k, 0) + 1
    return {"live": len(live), "window_min": window_s // 60, "by_source": by_source}
