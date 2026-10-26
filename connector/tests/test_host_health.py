"""Computer health: /proc readers, thresholds, voice summary, 24 h samples, the cleanup throttle, box endpoints
(auth on every one), the connector's ping / health / cleanup tools, and the safe cleanup's keep-list."""
import asyncio
import json
import os
import time

import httpx
import pytest

from aiteam_connector import safe_cleanup
from aiteam_connector.box import Box, BoxConfig, Caller, SonTarget, sha
from aiteam_connector.bridge import HermesBridge
from aiteam_connector.host_health import (DEFAULT_THRESHOLDS, HealthConfig, HostHealth, evaluate, live_sessions,
                                          summary_line)
from aiteam_connector.server import HEALTH_TOOLS, Connector, SonLink

from .fake_hermes import KEY, FakeHermes
from .test_connector import ALICE, BOB, start

GB = 1024 ** 3


def fake_proc(root, mem_total_kb=16_000_000, mem_avail_kb=8_000_000, swap_total_kb=4_000_000, swap_free_kb=3_000_000,
              load="0.50 0.40 0.30 1/200 999", uptime="1728000.00 100.0", oom=True):
    p = root / "proc"
    p.mkdir(exist_ok=True)
    (p / "meminfo").write_text(f"MemTotal: {mem_total_kb} kB\nMemFree: 100 kB\nMemAvailable: {mem_avail_kb} kB\n"
                               f"SwapTotal: {swap_total_kb} kB\nSwapFree: {swap_free_kb} kB\n")
    (p / "loadavg").write_text(load + "\n")
    (p / "uptime").write_text(uptime + "\n")
    (p / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0 0 0\n")
    if oom:
        (p / "4242").mkdir(exist_ok=True)
        (p / "4242" / "comm").write_text("earlyoom\n")
    return str(p)


def make_home(root, jobs=None):
    home = root / "home"
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs or [
        {"id": "a1", "name": "disk-cleanup (node_modules, caches)", "enabled": True, "state": "scheduled",
         "schedule_display": "30 3 * * *", "last_run_at": "2026-10-25T03:30:00+00:00", "last_status": "ok",
         "failure_streak": 0},
        {"id": "b2", "name": "daily post", "enabled": True, "state": "scheduled", "schedule_display": "0 9 * * *",
         "last_run_at": "2026-10-25T09:00:00+00:00", "last_status": "error", "last_error": "boom",
         "failure_streak": 1},
        {"id": "c3", "name": "old job", "enabled": False, "state": "paused", "failure_streak": 9}]}))
    return str(home)


def snap_of(disk=50.0, avail=50.0, swap=10.0, swap_total=1, load_per_core=0.2, jobs=(), guard=None):
    return {"disk": [{"path": "/", "used_pct": disk}], "memory": {"available_pct": avail},
            "swap": {"total_bytes": swap_total, "used_pct": swap},
            "cpu": {"load_per_core": load_per_core, "load_1m": load_per_core * 8, "cores": 8},
            "cron": {"jobs": list(jobs)}, "guard": guard}


# ------------------------------------------------------------------------------------------- thresholds
@pytest.mark.parametrize("disk,expect", [(79.9, "ok"), (80, "warning"), (85, "warning"), (94.9, "warning"),
                                         (95, "critical")])
def test_disk_thresholds_match_the_fleet_guard(disk, expect):
    assert evaluate(snap_of(disk=disk), DEFAULT_THRESHOLDS)[0] == expect


def test_memory_swap_cpu_and_failing_jobs():
    t = DEFAULT_THRESHOLDS
    assert evaluate(snap_of(avail=4), t)[0] == "critical"
    assert evaluate(snap_of(avail=12), t)[0] == "warning"
    assert evaluate(snap_of(swap=85), t)[0] == "warning"
    assert evaluate(snap_of(swap=99, swap_total=0), t)[0] == "ok"  # no swap at all is not a warning
    assert evaluate(snap_of(load_per_core=2.5), t)[0] == "warning"
    bad = {"name": "x", "enabled": True, "failure_streak": 3}
    st, why = evaluate(snap_of(jobs=[bad]), t)
    assert st == "warning" and "failing" in why[0]
    assert evaluate(snap_of(jobs=[{**bad, "enabled": False}]), t)[0] == "ok"
    assert evaluate(snap_of(guard={"status": "critical"}), t)[0] == "critical"


def test_summary_reads_well_aloud():
    s = snap_of(disk=66.2, avail=48.4)
    assert summary_line("ok", [], s) == "Healthy. Disk 66% used, memory 48% free."
    st, why = evaluate(snap_of(disk=91, avail=48), DEFAULT_THRESHOLDS)
    assert summary_line(st, why, snap_of(disk=91, avail=48)) == "Needs attention: disk 91% full. Disk 91% used, memory 48% free."


def test_live_sessions_counts_last_15_minutes():
    now = 1_000_000.0
    rows = [{"last_active": now - 60, "source": "discord"}, {"last_active": now - 3600, "source": "cli"},
            {"last_active": now - 10, "source": "api_server", "ended_at": now - 5},
            {"last_active": now - 30, "source": "discord", "archived": True}]
    assert live_sessions(rows, now) == {"live": 1, "window_min": 15, "by_source": {"discord": 1}}


# ------------------------------------------------------------------------------------------- snapshot
async def test_snapshot_from_proc_and_files(tmp_path):
    home = make_home(tmp_path)
    guard = tmp_path / "guard.json"
    guard.write_text(json.dumps({"status": "ok", "hosts": {"a": 66}}))
    h = HostHealth(HealthConfig(hermes_home=home, disks=[str(tmp_path)], proc=fake_proc(tmp_path), cgroup=str(tmp_path / "nocg"),
                                guard_status_file=str(guard)))
    s = await h.snapshot(sessions={"live": 2}, with_cpu=False)
    assert s["memory"]["total_gb"] == 16.4 and s["memory"]["available_pct"] == 50.0 and s["memory"]["guard"] == "on"
    assert s["swap"]["used_pct"] == 25.0
    assert s["uptime"]["text"] == "20 days" and s["uptime"]["seconds"] == 1728000
    assert s["cron"]["total"] == 3 and s["cron"]["enabled"] == 2 and s["cron"]["failing"] == 0
    assert s["last_cleanup"]["source"].startswith("scheduled job: disk-cleanup")
    assert s["guard"]["hosts"] == {"a": 66} and s["sessions"] == {"live": 2}
    assert s["status"] in ("ok", "warning", "critical") and s["summary"].endswith("free.")
    assert not os.path.exists(os.path.join(home, "health")), "a read never writes"


async def test_cpu_busy_pct_from_two_reads(tmp_path):
    proc = fake_proc(tmp_path)
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=proc, cgroup=str(tmp_path / "nocg")))
    stat = tmp_path / "proc" / "stat"

    async def bump():
        await asyncio.sleep(0.05)
        stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # +100 busy, +200 total
    asyncio.get_running_loop().create_task(bump())
    assert await h.cpu_pct(0.2) == 50.0


async def test_samples_keep_24h_of_5_minute_points(tmp_path):
    clock = [1_000_000.0]
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path), cgroup=str(tmp_path / "nocg")),
                   clock=lambda: clock[0])
    assert h.cfg.sample_interval_s == 300 and h.cfg.sample_keep == 288  # 24 h * 12 per hour
    for _ in range(300):
        s = await h.snapshot(with_cpu=False)
        h.record_sample(s)
        clock[0] += 300
    rows = h.history()
    assert len(rows) == 288 and rows[-1]["t"] - rows[0]["t"] == 287 * 300
    assert set(rows[-1]) == {"t", "disk", "mem", "swap", "load", "cpu", "status"}
    # survives a restart: a new monitor on the same state dir reads the same graph
    assert len(HostHealth(h.cfg).history()) == 288


# ------------------------------------------------------------------------------------------- cleanup throttle
async def test_cleanup_runs_fixed_command_once_per_10_minutes(tmp_path):
    clock = [1_000_000.0]
    seen = []

    async def runner(cmd):
        seen.append(cmd)
        return 0, "safe cleanup: freed 1.00 GB from 3 items"
    cfg = HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path), cgroup=str(tmp_path / "nocg"))
    h = HostHealth(cfg, clock=lambda: clock[0], runner=runner)
    code, body = await h.cleanup()
    assert code == 200 and body["status"] == "done" and body["ok"] is True
    assert seen == [cfg.cleanup_command] and "aiteam_connector.safe_cleanup" in cfg.cleanup_command
    clock[0] += 120
    code, body = await h.cleanup()
    assert code == 429 and body["retry_after_s"] == 480 and "try again in 8 min" in body["message"].lower()
    assert len(seen) == 1
    clock[0] += 481
    assert (await h.cleanup())[0] == 200 and len(seen) == 2
    last = (await h.snapshot(with_cpu=False))["last_cleanup"]
    assert last["source"] == "cleanup tool" or last["finished_at"]


async def test_cleanup_is_single_flight_and_reports_long_runs(tmp_path):
    gate = asyncio.Event()

    async def slow(cmd):
        await gate.wait()
        return 0, "done"
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path), cgroup=str(tmp_path / "nocg")),
                   runner=slow)
    code, body = await h.cleanup(wait_s=0.05)
    assert code == 202 and body["status"] == "running"
    assert (await h.cleanup(wait_s=0.05))[0] == 409
    gate.set()
    await asyncio.sleep(0.05)
    assert json.load(open(h.cleanup_path))["ok"] is True


def test_cleanup_command_must_be_argv():
    with pytest.raises(ValueError):
        HealthConfig(cleanup_command="rm -rf /")  # a shell string is refused


# ------------------------------------------------------------------------------------------- box endpoints
@pytest.fixture
async def hbox(tmp_path):
    fake = FakeHermes()
    r1, hermes_url = await start(fake.app())
    calls = []

    async def runner(cmd):
        calls.append(cmd)
        return 0, "ok"
    cfg = BoxConfig("test-box", "Test box", {"my-son": SonTarget("my-son", hermes_url, "X")},
                    [Caller("alice", "Alice", sha(ALICE), ["my-son"]), Caller("bob", "Bob", sha(BOB), ["other"])])
    host = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path), cgroup=str(tmp_path / "nocg")),
                      runner=runner)
    box = Box(cfg, bridges={"my-son": HermesBridge(hermes_url, KEY)}, host=host)
    r2, box_url = await start(box.app())
    yield box_url, calls
    await r2.cleanup()
    await r1.cleanup()


async def test_box_health_endpoints_need_a_caller_token(hbox):
    box_url, calls = hbox
    async with httpx.AsyncClient() as c:
        for m, path in (("GET", "/v1/ping"), ("GET", "/v1/health"), ("GET", "/v1/health/history"),
                        ("POST", "/v1/cleanup")):
            assert (await c.request(m, box_url + path)).status_code == 401
            assert (await c.request(m, box_url + path, headers={"Authorization": "Bearer nope"})).status_code == 401
        assert calls == [], "an unauthenticated cleanup never runs"
        h = {"Authorization": f"Bearer {ALICE}"}
        p = (await c.get(f"{box_url}/v1/ping", headers=h)).json()
        assert p["alive"] is True and p["uptime"] == "20 days"
        d = (await c.get(f"{box_url}/v1/health", headers=h)).json()
        for k in ("disk", "memory", "swap", "cpu", "uptime", "sessions", "cron", "last_cleanup", "status", "summary"):
            assert k in d
        assert d["box"]["id"] == "test-box" and d["sessions"]["live"] == 0
        assert "history" in (await c.get(f"{box_url}/v1/health?history=1", headers=h)).json()
        r = await c.post(f"{box_url}/v1/cleanup", headers=h, json={"command": "rm -rf /"})
        assert r.status_code == 200 and len(calls) == 1 and calls[0][-2:] != ["rm", "-rf"]
        r = await c.post(f"{box_url}/v1/cleanup", headers=h)
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and len(calls) == 1


# ------------------------------------------------------------------------------------------- connector tools
async def test_connector_ping_health_cleanup_tools(hbox):
    box_url, calls = hbox
    conn = Connector([SonLink("my-son", box_url, ALICE)], passthrough="none", github=False)
    names = [t.name for t in await conn.list_tools()]
    assert set(HEALTH_TOOLS) <= set(names)
    tools = {t.name: t for t in await conn.list_tools()}
    assert tools["ping"].annotations.readOnlyHint and tools["health"].annotations.readOnlyHint
    assert tools["cleanup"].annotations.readOnlyHint is False and tools["cleanup"].annotations.destructiveHint is False
    r = await conn.call("ping", {})
    assert not r.isError and r.content[0].text == "Up for 20 days."
    r = await conn.call("health", {"son": "my-son"})
    first = r.content[0].text.splitlines()[0]
    assert not r.isError and first.startswith(("Healthy.", "Needs attention:", "Critical:")) and first.endswith("free.")
    assert json.loads(r.content[0].text.split("\n\n", 1)[1])["status"] in ("ok", "warning", "critical")
    r = await conn.call("cleanup", {})
    assert not r.isError and r.content[0].text.startswith("Cleanup done.")
    r = await conn.call("cleanup", {})
    assert not r.isError and "Try again in" in r.content[0].text, "too soon is an answer, not an error"


async def test_connector_reports_a_dead_box_plainly():
    conn = Connector([SonLink("my-son", "http://127.0.0.1:9", "t")], passthrough="none", github=False)
    r = await conn.call("ping", {})
    assert r.isError and "not reachable" in r.content[0].text


async def test_bad_token_on_health_is_an_error(hbox):
    box_url, _ = hbox
    conn = Connector([SonLink("my-son", box_url, "wrong")], passthrough="none", github=False)
    assert (await conn.call("health", {})).isError


# ------------------------------------------------------------------------------------------- safe cleanup
def _old(p, hours=48):
    t = time.time() - hours * 3600
    os.utime(p, (t, t))


def test_safe_cleanup_only_takes_rebuildable_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))  # never touch the real package caches
    root = tmp_path / "root"
    nm = root / "work" / "app" / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "i.js").write_text("x" * 1000)
    fresh = root / "work" / "new" / "node_modules"
    fresh.mkdir(parents=True)
    (fresh / "f.js").write_text("y")
    keep_skill = root / "skills" / "s" / "node_modules"
    keep_skill.mkdir(parents=True)
    (root / "memories").mkdir()
    (root / "memories" / "MEMORY.md").write_text("keep")
    (root / ".env").write_text("SECRET=1")
    scratch = root / "cache" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "old.mp4").write_text("v" * 500)
    (scratch / "new.mp4").write_text("v")
    (scratch / "notes.md").write_text("keep")
    for p in (nm / "i.js", nm, nm.parent, scratch / "old.mp4", scratch / "notes.md", keep_skill):
        _old(p)
    paths = {p for _, p in safe_cleanup.plan(str(root), 2.0, time.time())}
    assert str(nm.parent) in paths and str(scratch / "old.mp4") in paths
    for kept in (fresh, keep_skill, root / "memories" / "MEMORY.md", root / ".env", scratch / "new.mp4",
                 scratch / "notes.md"):
        assert str(kept) not in paths
    freed, removed = safe_cleanup.run(str(root), 2.0)
    assert freed >= 1500 and not nm.parent.exists() and not (scratch / "old.mp4").exists()
    assert (root / "memories" / "MEMORY.md").read_text() == "keep" and (root / ".env").exists()
    assert fresh.exists() and keep_skill.exists() and (scratch / "notes.md").exists()


def test_safe_cleanup_dry_run_changes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    root = tmp_path / "r"
    nm = root / "a" / "node_modules"
    nm.mkdir(parents=True)
    (nm / "x").write_text("z")
    _old(nm / "x")
    _old(nm)
    safe_cleanup.main(["--root", str(root), "--dry-run"])
    assert "dry run" in capsys.readouterr().out and nm.exists()


def test_safe_cleanup_never_touches_runtime_installs(tmp_path, monkeypatch):
    """Regression: a dry run on a real box listed the global npm prefix and __pycache__ inside live venvs."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    root = tmp_path / "root"
    keep = [
        root / "node" / "lib" / "node_modules" / "npm",                                  # global npm prefix
        root / "home" / ".npm-global" / "lib" / "node_modules" / "corepack",             # another global prefix
        root / "venv" / "lib" / "python3.11" / "site-packages" / "x" / "__pycache__",    # a venv's packages
        root / "mcp-gate" / "connector" / "venv" / "lib" / "python3.12" / "site-packages" / "y" / "__pycache__",
        root / "tools" / "env1" / "lib" / "python3.12" / "z" / "__pycache__",             # venv found by pyvenv.cfg
        root / "hermes-agent" / "web" / "node_modules",
        root / "work" / ".venv" / "lib" / "node_modules",
        root / "usr" / "lib" / "python3" / "dist-packages" / "w" / "__pycache__",
    ]
    for k in keep:
        k.mkdir(parents=True)
        (k / "f").write_text("x" * 100)
    (root / "tools" / "env1" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    gone = root / "work" / "app" / "node_modules"
    gone.mkdir(parents=True)
    (gone / "i.js").write_text("y" * 100)
    for p in [*keep, gone]:
        _old(p / "f" if (p / "f").exists() else p / "i.js")
        _old(p)
    paths = {p for _, p in safe_cleanup.plan(str(root), 2.0, time.time())}
    assert paths == {str(gone)}, sorted(paths)
    safe_cleanup.run(str(root), 2.0)
    assert not gone.exists() and all(k.exists() and (k / "f").exists() for k in keep)


def fake_cgroup(root, max_b="2147483648", current=1_500_000_000, inactive=500_000_000, swap_max="1073741824",
                swap_cur=268_435_456):
    cg = root / "cg"
    cg.mkdir(exist_ok=True)
    (cg / "memory.max").write_text(f"{max_b}\n")
    (cg / "memory.current").write_text(f"{current}\n")
    (cg / "memory.stat").write_text(f"anon 900000000\nfile 600000000\ninactive_file {inactive}\n")
    (cg / "memory.swap.max").write_text(f"{swap_max}\n")
    (cg / "memory.swap.current").write_text(f"{swap_cur}\n")
    return str(cg)


def test_memory_uses_the_container_limit_when_one_is_set(tmp_path):
    """A customer Son is capped by its plan (2 GB here) on a 16 GB host: health must report against the cap."""
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path),
                                cgroup=fake_cgroup(tmp_path)))
    m = h.static_snapshot()
    assert m["memory"]["limit"] == "container"
    assert m["memory"]["total_bytes"] == 2147483648
    assert m["memory"]["available_bytes"] == 2147483648 - (1_500_000_000 - 500_000_000)  # cache is reclaimable
    assert m["swap"]["total_bytes"] == 1073741824 and m["swap"]["used_bytes"] == 268_435_456


def test_memory_falls_back_to_host_without_a_limit(tmp_path):
    for kw in ({"max_b": "max"}, {"max_b": str(64 * GB)}):  # unlimited, or a limit above host RAM
        d = tmp_path / kw["max_b"]
        d.mkdir()
        h = HostHealth(HealthConfig(hermes_home=make_home(d), disks=[str(d)], proc=fake_proc(d),
                                    cgroup=fake_cgroup(d, **kw)))
        m = h.static_snapshot()["memory"]
        assert m["limit"] == "host" and m["total_bytes"] == 16_000_000 * 1024
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path),
                                cgroup=str(tmp_path / "missing")))
    assert h.static_snapshot()["memory"]["limit"] == "host"


def test_container_near_its_cap_is_flagged(tmp_path):
    """1.95 GB used of a 2 GB cap = critical, even though the host has 8 GB free."""
    cg = fake_cgroup(tmp_path, current=2_050_000_000, inactive=0)
    h = HostHealth(HealthConfig(hermes_home=make_home(tmp_path), disks=[str(tmp_path)], proc=fake_proc(tmp_path),
                                cgroup=cg))
    snap = asyncio.run(h.snapshot(sessions={"live": 0}, with_cpu=False))
    assert snap["status"] in ("warning", "critical") and "memory" in snap["summary"].lower()
