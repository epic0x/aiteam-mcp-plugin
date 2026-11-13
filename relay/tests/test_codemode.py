"""Code Mode (RELAY_CODE_MODE=on): search / describe / execute through a real MCP session, the fake aiteam.space and
fake boxes of test_multibox, and the real sandbox process (sandbox/runner.mjs under Node's permission model)."""
import asyncio
import contextlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from aiteam_relay.app import Settings, create_app
from aiteam_relay.codemode import MIN_NODE, NEEDS_APPROVAL, PROBES, SANDBOX_DIR, CodeMode

from .test_multibox import SON_B, DeadAware, FakeJwks, V3, clock, data, session, world  # noqa: F401 (fixtures)

NODE = shutil.which("node")
HAVE_SANDBOX = NODE is not None and (Path(SANDBOX_DIR) / "node_modules" / "quickjs-emscripten-core").is_dir()
has_packages = pytest.mark.skipif(not HAVE_SANDBOX, reason="node or sandbox/node_modules missing (npm ci in sandbox/)")


def _node_major() -> int:
    try:
        return int(subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=10).stdout
                   .strip().lstrip("v").split(".")[0])
    except Exception:
        return 0


NODE_OK = NODE is not None and _node_major() >= MIN_NODE
# Code Mode only turns on with both (see CodeMode.check), so every run test needs both.
needs_sandbox = pytest.mark.skipif(not (HAVE_SANDBOX and NODE_OK),
                                   reason=f"needs Node {MIN_NODE}+ and sandbox/node_modules (npm ci in sandbox/)")


@contextlib.asynccontextmanager
async def relay(world, clock, monkeypatch, *, code="on", limits=None, wall=20.0, sandbox_dir=None):
    for k in ("RELAY_PUBLIC_URL", "AITEAM_ISSUER", "RELAY_MULTI_BOX", "RELAY_CODE_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RELAY_MULTI_BOX", "on")
    monkeypatch.setenv("RELAY_CODE_MODE", code)
    opts = {"limits": limits or {}, "wall": wall}
    if sandbox_dir is not None:
        opts["sandbox_dir"] = sandbox_dir
    app = create_app(Settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(world.site)),
                     jwks=FakeJwks(), box_transport=DeadAware(world), clock=clock, box_timeout=2.0,
                     code_mode_opts=opts)
    async with contextlib.AsyncExitStack() as st:
        for b in world.boxes.values():
            await st.enter_async_context(b.mgr.run())
        await st.enter_async_context(app.router.lifespan_context(app))
        yield app


async def run(s, code):
    r = await s.call_tool("execute", {"code": code})
    return r, data(r)


# ---------------------------------------------------------------------------------------------------------- tools
async def test_off_by_default_no_code_tools(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch, code="off") as app, session(app, V3()) as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert not {"search", "describe", "execute"} & names
    assert "boxes_list" in names


async def test_code_tools_listed_and_read_only(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    for n in ("search", "describe", "execute"):
        assert tools[n].annotations.readOnlyHint is True
    assert "boxes" in tools["execute"].description and "plain tools" in tools["execute"].description


async def test_search_finds_methods_and_box_tools(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = data(await s.call_tool("search", {"query": "kanban cards"}))
        only = data(await s.call_tool("search", {"query": "only_b"}))
    names = [x["name"] for x in r["results"]]
    assert "kanban.list" in names and "kanban_list" in names
    row = next(x for x in only["results"] if x["name"] == "only_b")
    assert row["boxes"] == ["box-b"] and row["read_only"] is True


async def test_describe_method_and_box_tool(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r = data(await s.call_tool("describe", {"names": ["kanban.list", "box-a/only_a", "nope/zz"]}))
    m, t, bad = r["results"]
    assert m["kind"] == "method" and "box" in m["input"]["required"]
    assert t["kind"] == "box tool" and t["boxes"] == ["box-a"] and t["read_only"] is False
    assert "error" in bad


# ---------------------------------------------------------------------------------------------------------- execute
@needs_sandbox
async def test_execute_loops_over_boxes_one_result(world, clock, monkeypatch):
    code = """
    const out = {};
    for (const b of await boxes.list()) {
      const k = await kanban.list({ box: b.box });
      out[b.box] = k.count;
    }
    console.log("boxes", Object.keys(out).length);
    return out;
    """
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r, d = await run(s, code)
    assert not r.isError and d["ok"] and d["value"] == {"box-a": 1, "box-b": 2}
    assert d["logs"] == ["boxes 2"] and d["calls"] == 3
    # each box call went with that box's own exchanged token, never the relay token
    assert {e.get("target") for e in world.exchanges} >= {SON_B}
    assert all(a.startswith("Bearer box-token-") for b in world.boxes.values() for a in b.auth_seen)


@needs_sandbox
async def test_execute_box_outside_grant_refused(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r, d = await run(s, 'try { await kanban.list({ box: "calendar-team" }) } catch (e) { return e.message }')
    assert d["value"] == "box not in this connection"
    assert world.cal.calls == []


@needs_sandbox
async def test_execute_grant_shrinks_mid_life(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        _, before = await run(s, "return (await boxes.list()).map(b => b.box)")
        world.targets = world.targets[:1]
        clock.t += 31  # targets cache is 30 s: the grant is read again
        _, after = await run(s, 'try { await kanban.list({ box: "box-b" }) } catch (e) { return e.message }')
    assert before["value"] == ["box-a", "box-b"]
    assert after["value"] == "box not in this connection"


@needs_sandbox
async def test_execute_writes_need_approval(world, clock, monkeypatch):
    code = """
    const r = [];
    for (let i = 0; i < 3; i++) {
      try { await tools.call({ box: "box-a", name: "only_a" }) } catch (e) { r.push(e.message) }
    }
    r.push(JSON.stringify(await tools.call({ box: "box-b", name: "only_b" })));
    return r;
    """
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        _, d = await run(s, code)
    assert d["value"][:3] == [NEEDS_APPROVAL] * 3
    assert json.loads(d["value"][3]) == {"from": "b"}
    assert world.a.calls == []  # the write tool never reached the box
    assert world.b.calls == [("only_b", {})]


@needs_sandbox
async def test_execute_has_no_network_files_or_host_handles(world, clock, monkeypatch):
    code = """
    const seen = [typeof fetch, typeof require, typeof process, typeof __call, typeof setTimeout,
                  typeof WebAssembly, typeof sons];
    let ev; try { ev = new Function("return 1")() } catch (e) { ev = "blocked" }
    return { seen, ev };
    """
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        _, d = await run(s, code)
    assert d["value"]["seen"] == ["undefined"] * 7


@needs_sandbox
async def test_execute_limits(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch, limits={"cpuMs": 400, "maxCalls": 4}) as app, \
            session(app, V3()) as s:
        r1, spin = await run(s, "while (true) {}")
        _, cap = await run(s, "for (let i = 0; i < 20; i++) await boxes.list(); return 'no cap'")
        _, hang = await run(s, "await new Promise(() => {}); return 1")
        _, syn = await run(s, "return (")
        _, big = await run(s, "return 'x'.repeat(300000)")
    assert r1.isError and "time limit" in spin["error"]
    assert "call limit" in cap["error"] and cap["calls"] == 5
    assert "never ends" in hang["error"]
    assert syn["error"].startswith("SyntaxError")
    assert "result over" in big["error"]


@needs_sandbox
async def test_execute_wall_clock(world, clock, monkeypatch):
    world.a.slow = 3.0  # the box takes longer than the whole run may
    async with relay(world, clock, monkeypatch, wall=1.0) as app, session(app, V3()) as s:
        r, d = await run(s, 'return await kanban.list({ box: "box-a" })')
    assert r.isError and "1 s in total" in d["error"]


@needs_sandbox
async def test_execute_box_error_is_catchable_and_others_answer(world, clock, monkeypatch):
    world.dead.add("box-b.aiteam.space")
    code = """
    const out = {};
    for (const b of await boxes.list()) {
      try { out[b.box] = (await kanban.list({ box: b.box })).count } catch (e) { out[b.box] = "down" }
    }
    return out;
    """
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        _, d = await run(s, code)
    assert d["value"] == {"box-a": 1, "box-b": "down"}


async def test_execute_code_required_and_capped(world, clock, monkeypatch):
    async with relay(world, clock, monkeypatch) as app, session(app, V3()) as s:
        r1 = await s.call_tool("execute", {"code": "  "})
        r2 = await s.call_tool("execute", {"code": "x" * 20_001})
    assert r1.isError and r2.isError


# ---------------------------------------------------------------------------------------------------------- start-up
async def _listener():
    hits = []

    async def on_conn(_r, w):
        hits.append(1)
        w.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], hits


@needs_sandbox
async def test_runner_process_is_refused_network_files_processes():
    """The sandbox process itself (not the interpreter inside it) tries to open a socket to a live local port, fetch
    it, read a file outside its folder, write, spawn and start a worker: Node refuses every one, nothing connects."""
    cm = CodeMode(router=None)
    server, port, hits = await _listener()
    try:
        sc = await cm.selfcheck(port)
    finally:
        server.close()
        await server.wait_closed()
    assert {k: sc[k] for k in PROBES} == {k: "denied" for k in PROBES}, sc
    assert hits == []
    assert not (Path(SANDBOX_DIR) / ".selfcheck").exists()


@has_packages
async def test_probe_is_real_without_the_flags():
    """Control: the same probe started without the permission flags does reach the port, so a 'denied' above is Node
    refusing, not a probe that never tried."""
    cm = CodeMode(router=None)
    server, port, hits = await _listener()
    try:
        sc = await cm.selfcheck(port, locked=False)
    finally:
        server.close()
        await server.wait_closed()
        (Path(SANDBOX_DIR) / ".selfcheck").unlink(missing_ok=True)
    assert sc["net"] == "allowed" and hits


@needs_sandbox
async def test_check_passes_on_new_node():
    ok, why = await CodeMode(router=None).check()
    assert ok, why


async def test_check_refuses_old_node(monkeypatch):
    cm = CodeMode(router=None)

    async def old(port, locked=True):
        return {"node": "24.11.0", **{k: "denied" for k in PROBES}}

    monkeypatch.setattr(cm, "selfcheck", old)
    monkeypatch.setattr(cm, "sandbox_dir", _fake_sandbox())
    ok, why = await cm.check()
    assert not ok and "too old" in why and str(MIN_NODE) in why


async def test_check_refuses_when_a_probe_is_not_denied(monkeypatch):
    cm = CodeMode(router=None)

    async def leaky(port, locked=True):
        return {"node": "26.0.0", **{k: "denied" for k in PROBES}, "net": "allowed"}

    monkeypatch.setattr(cm, "selfcheck", leaky)
    monkeypatch.setattr(cm, "sandbox_dir", _fake_sandbox())
    ok, why = await cm.check()
    assert not ok and "net" in why


def _fake_sandbox():
    import tempfile
    d = Path(tempfile.mkdtemp()) / "sandbox"
    (d / "node_modules" / "quickjs-emscripten-core").mkdir(parents=True)
    return d


async def test_not_set_up_hides_code_tools_and_says_why(world, clock, monkeypatch, tmp_path):
    # tmp_path is an empty sandbox folder: no packages installed
    async with relay(world, clock, monkeypatch, sandbox_dir=tmp_path) as app, session(app, V3()) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        r = await s.call_tool("execute", {"code": "return 1"})
    assert not {"search", "describe", "execute"} & names
    assert "boxes_list" in names  # the rest of the relay still works
    assert r.isError and "not set up" in data(r)["error"] and "npm ci" in data(r)["error"]


async def test_missing_node_says_why(tmp_path):
    (tmp_path / "node_modules" / "quickjs-emscripten-core").mkdir(parents=True)
    ok, why = await CodeMode(router=None, node=str(tmp_path / "no-node"), sandbox_dir=tmp_path).check()
    assert not ok and f"Node {MIN_NODE}" in why
