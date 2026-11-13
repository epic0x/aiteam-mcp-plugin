"""Code Mode (RELAY_CODE_MODE=on, needs RELAY_MULTI_BOX=on): three tools on top of the multi-box relay.

  search(query)    find API methods and box tools by words; compact answer
  describe(names)  full input schema of the methods / tools asked for
  execute(code)    run one JavaScript snippet against the API below; only its return value comes back

The snippet runs in sandbox/runner.mjs: a QuickJS (WebAssembly) interpreter inside a Node process started with the
permission model on (reads only its own folder; network, writes, child processes and workers refused) and an empty
environment. Code Mode turns on only after a start-up check proves this on the installed Node (25 or later): the
sandbox process tries each of those and must be refused by Node itself. Otherwise the code tools are not offered and
the log says why. The snippet's only way out is the API: every method call comes back here and runs exactly like a
plain tool call: the grant's boxes are read live (targets.py), the chosen box must be in them, and the call goes with
that box's own exchanged token. Nothing in the sandbox ever holds a token.

Writes do not run from a snippet. tools.call only runs tools the box marks read-only; anything else answers
"needs approval" and the caller uses the plain tool, where the box's own approval rules apply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mcp.types as types

from .boxes import BoxError

if TYPE_CHECKING:  # pragma: no cover
    from .multibox import Caller, Router
    from .targets import Target, Targets

log = logging.getLogger("aiteam_relay.codemode")

CODE_TOOLS = ("search", "describe", "execute")
SANDBOX_DIR = Path(os.environ.get("RELAY_SANDBOX_DIR") or Path(__file__).resolve().parent.parent / "sandbox")
MIN_NODE = 25  # Node's permission model only covers network access from Node 25 on
PROBES = ("net", "fetch", "read", "write", "child", "worker")
CHECK_SECONDS = 15.0
RECHECK_SECONDS = 300.0  # while off, look again every 5 minutes (after npm ci, say)
NOT_READY = "Code Mode is not set up on this relay yet"
LIMITS = {"memoryBytes": 32 << 20, "stackBytes": 512 << 10, "cpuMs": 10_000, "maxCalls": 50, "maxLogs": 200,
          "maxOutBytes": 256 << 10}
WALL_SECONDS = 60.0  # whole run, waiting on boxes included
MAX_CODE = 20_000
MAX_LINE = 1 << 20  # one protocol line from the sandbox
RUNS_AT_ONCE = 4
NEEDS_APPROVAL = "needs approval: this tool changes something, so it can't run from execute(). Call it as a plain tool."

_BOX = {"type": "string", "description": "Box name from boxes.list()."}
_LIM = lambda d, hi: {"type": "integer", "minimum": 1, "maximum": hi, "default": d}  # noqa: E731


@dataclass(frozen=True)
class Method:
    name: str
    summary: str
    input: dict
    returns: str


METHODS: dict[str, Method] = {m.name: m for m in [
    Method("boxes.list", "Every box this connection reaches: name, kind, project, calendar. No health check.",
           {"type": "object", "properties": {}}, "[{box, kind, name, project_name, calendar}]"),
    Method("boxes.health", "Live health of one box (box_health, or a ping on older boxes).",
           {"type": "object", "required": ["box"], "properties": {"box": _BOX}}, "{ok, ...}"),
    Method("tools.list", "Tool names on one box, with whether each is read-only.",
           {"type": "object", "required": ["box"], "properties": {"box": _BOX}},
           "[{name, description, read_only}]"),
    Method("tools.call", "Run one read-only tool on one box. Tools that change something answer 'needs approval'.",
           {"type": "object", "required": ["box", "name"],
            "properties": {"box": _BOX, "name": {"type": "string"}, "args": {"type": "object"}}},
           "the tool's result (structured content, else its text)"),
    Method("kanban.list", "Cards on one box's task board.",
           {"type": "object", "required": ["box"], "properties": {
               "box": _BOX, "status": {"type": "string"}, "assignee": {"type": "string"}, "limit": _LIM(50, 200)}},
           "{count, cards: [...]}"),
    Method("kanban.read", "One card from one box's task board.",
           {"type": "object", "required": ["box", "id"], "properties": {"box": _BOX, "id": {"type": "string"}}},
           "{card}"),
    Method("memory.read", "Search one box's shared memory.",
           {"type": "object", "required": ["box"], "properties": {
               "box": _BOX, "query": {"type": "string", "maxLength": 200}, "limit": _LIM(20, 100)}},
           "{count, entries: [...]}"),
]}

INSTRUCTIONS = (
    "Code Mode: write one JavaScript snippet for execute() instead of many tool calls. The snippet is the body of an "
    "async function: use await, loops and filters, and `return` only what is needed. Globals: "
    + ", ".join(sorted({m.split('.')[0] for m in METHODS})) + " and console.log. Each method takes one object "
    "argument. No fetch, no require, no timers. Use search() and describe() to find methods and box tools first. "
    f"Limits per run: {LIMITS['maxCalls']} method calls, {LIMITS['cpuMs'] // 1000} s of CPU, {int(WALL_SECONDS)} s "
    "in total, 256 KB result. Tools that change something can't run from a snippet: call them as plain tools."
)


def code_tools() -> list[types.Tool]:
    ro = lambda title: types.ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,  # noqa: E731
                                             idempotentHint=True, openWorldHint=False)
    return [
        types.Tool(name="search", title="Find API methods and box tools",
                   description="Searches the Code Mode API and every box's tools by words. Returns names and one "
                               "line each; call describe() for the full input.",
                   inputSchema={"type": "object", "additionalProperties": False, "required": ["query"],
                                "properties": {"query": {"type": "string", "maxLength": 200},
                                               "limit": _LIM(20, 100)}},
                   annotations=ro("Find API methods and box tools")),
        types.Tool(name="describe", title="Describe API methods or box tools",
                   description="Full input schema of the methods (for example kanban.list) or box tools (box/tool, "
                               "for example box-a/kanban_list) named.",
                   inputSchema={"type": "object", "additionalProperties": False, "required": ["names"],
                                "properties": {"names": {"type": "array", "maxItems": 20,
                                                         "items": {"type": "string", "maxLength": 200}}}},
                   annotations=ro("Describe API methods or box tools")),
        types.Tool(name="execute", title="Run a snippet over your boxes",
                   description=INSTRUCTIONS,
                   inputSchema={"type": "object", "additionalProperties": False, "required": ["code"],
                                "properties": {"code": {"type": "string", "maxLength": MAX_CODE,
                                                        "description": "Body of an async function. Example: "
                                                        "const b = await boxes.list(); return b.map(x => x.box)"}}},
                   annotations=ro("Run a snippet over your boxes")),
    ]


def _result(data: Any, *, error: bool = False, head: str = "") -> types.CallToolResult:
    text = json.dumps(data, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=(head + "\n" + text) if head else text)],
                                structuredContent=data if isinstance(data, dict) else {"value": data}, isError=error)


def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) > 1}


class CodeMode:
    def __init__(self, router: "Router", *, node: str | None = None, sandbox_dir: Path = SANDBOX_DIR,
                 limits: dict | None = None, wall: float = WALL_SECONDS) -> None:
        self.router = router
        self.node = node or os.environ.get("RELAY_NODE") or shutil.which("node") or "node"
        self.sandbox_dir = sandbox_dir
        self.limits = {**LIMITS, **(limits or {})}
        self.wall = wall
        self.runs = asyncio.Semaphore(RUNS_AT_ONCE)
        self._ready: tuple[bool, str] | None = None
        self._checked = 0.0
        self._check_lock = asyncio.Lock()

    # ------------------------------------------------------------------ start-up check
    def _argv(self, *, locked: bool = True) -> list[str]:
        d = str(self.sandbox_dir)
        flags = ["--permission", f"--allow-fs-read={d}", "--disallow-code-generation-from-strings"] if locked else []
        return [self.node, *flags, "--max-old-space-size=96", f"{d}/runner.mjs"]

    async def selfcheck(self, port: int, *, locked: bool = True) -> dict:
        """Run the sandbox's own probes against 127.0.0.1:port. `locked=False` exists only for the test that shows the
        probes do see an open network when the flags are missing."""
        proc = await asyncio.create_subprocess_exec(*self._argv(locked=locked), stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                    env={}, cwd=str(self.sandbox_dir), limit=MAX_LINE)
        try:
            job = {"selfcheck": {"port": port, "path": str(Path(__file__).resolve())}}
            proc.stdin.write((json.dumps(job) + "\n").encode())
            await proc.stdin.drain()
            async with asyncio.timeout(CHECK_SECONDS):
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        return {"error": "the sandbox did not answer the check"}
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    sc = (msg.get("done") or {}).get("selfcheck") if isinstance(msg, dict) else None
                    if isinstance(sc, dict):
                        return sc
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    async def check(self) -> tuple[bool, str]:
        """Code Mode turns on only when Node is new enough, the sandbox packages are installed, and the sandbox
        process is refused every probe by Node itself, with nothing reaching the local test port."""
        if not (Path(self.sandbox_dir) / "node_modules" / "quickjs-emscripten-core").is_dir():
            return False, "the sandbox packages are missing (run npm ci in relay/sandbox)"
        reached = 0

        async def on_conn(_r, w):
            nonlocal reached
            reached += 1
            w.close()

        server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            try:
                sc = await self.selfcheck(port)
            except FileNotFoundError:
                return False, f"Node {MIN_NODE} or later is not installed"
            except TimeoutError:
                return False, "the sandbox check timed out"
        finally:
            server.close()
            await server.wait_closed()
        if "error" in sc:
            return False, str(sc["error"])
        v = str(sc.get("node") or "")
        try:
            major = int(v.split(".")[0])
        except ValueError:
            return False, "could not read the Node version"
        if major < MIN_NODE:
            return False, f"Node {v} is too old: Code Mode needs Node {MIN_NODE} or later"
        open_ = [k for k in PROBES if sc.get(k) != "denied"]
        if open_ or reached:
            return False, "the sandbox was not refused: " + ", ".join(open_ or ["net"])
        return True, f"Node {v}, sandbox refused network, files, processes and workers"

    async def ready(self) -> tuple[bool, str]:
        now = time.monotonic()
        if self._ready is not None and (self._ready[0] or now < self._checked + RECHECK_SECONDS):
            return self._ready
        async with self._check_lock:
            if self._ready is not None and (self._ready[0] or now < self._checked + RECHECK_SECONDS):
                return self._ready
            try:
                got = await self.check()
            except Exception as e:  # never let the check break the relay
                got = (False, f"the sandbox check failed ({type(e).__name__})")
            if got[0]:
                log.info("Code Mode is on: %s", got[1])
            else:
                log.warning("Code Mode is off, code tools are not offered: %s", got[1])
            self._ready, self._checked = got, time.monotonic()
            return got

    # ------------------------------------------------------------------ dispatch
    async def call(self, c: "Caller", t: "Targets", name: str, args: dict) -> types.CallToolResult:
        ok, why = await self.ready()
        if not ok:
            return _result({"ok": False, "error": f"{NOT_READY}: {why}"}, error=True)
        if name == "search":
            return await self.search(c, t, args)
        if name == "describe":
            return await self.describe(c, t, args)
        return await self.execute(c, args)

    async def _box_tools(self, c: "Caller", t: "Targets") -> list[tuple["Target", types.Tool]]:
        got = await self.router.pool.fan_out(t.targets, lambda x: self.router.pool.tools(c.token, c.exp, x, c.legacy))
        return [(x, tl) for x, res in got if not isinstance(res, BaseException) for tl in res]

    async def search(self, c: "Caller", t: "Targets", args: dict) -> types.CallToolResult:
        q = _words(str(args.get("query") or ""))
        limit = args.get("limit") if isinstance(args.get("limit"), int) else 20
        rows: list[tuple[int, dict]] = []
        for m in METHODS.values():
            score = len(q & _words(m.name + " " + m.summary)) if q else 1
            if score:
                rows.append((score + 1, {"kind": "method", "name": m.name, "summary": m.summary}))
        by_tool: dict[str, dict] = {}
        for x, tl in await self._box_tools(c, t):
            row = by_tool.setdefault(tl.name, {"kind": "box tool", "name": tl.name,
                                               "summary": (tl.description or "").split("\n")[0][:160], "boxes": [],
                                               "read_only": True})
            row["boxes"].append(x.key)
            row["read_only"] = row["read_only"] and bool(tl.annotations and tl.annotations.readOnlyHint)
        for row in by_tool.values():
            score = len(q & _words(row["name"] + " " + row["summary"])) if q else 1
            if score:
                rows.append((score, row))
        rows.sort(key=lambda r: (-r[0], r[1]["name"]))
        out = [r for _, r in rows[:max(1, min(limit, 100))]]
        return _result({"query": args.get("query"), "results": out}, head=f"{len(out)} matches.")

    async def describe(self, c: "Caller", t: "Targets", args: dict) -> types.CallToolResult:
        names = [n for n in (args.get("names") or []) if isinstance(n, str)][:20]
        need_boxes = any(n not in METHODS for n in names)
        tools = await self._box_tools(c, t) if need_boxes else []
        out = []
        for n in names:
            if n in METHODS:
                m = METHODS[n]
                out.append({"name": n, "kind": "method", "summary": m.summary, "input": m.input, "returns": m.returns})
                continue
            box, _, tool = n.rpartition("/")
            hits = [(x, tl) for x, tl in tools if tl.name == tool and (not box or x.key == box)]
            if not hits:
                out.append({"name": n, "error": "no such method or tool in this connection"})
                continue
            x, tl = hits[0]
            out.append({"name": n, "kind": "box tool", "boxes": [h[0].key for h in hits], "description": tl.description,
                        "input": tl.inputSchema,
                        "read_only": all(bool(h[1].annotations and h[1].annotations.readOnlyHint) for h in hits),
                        "call_with": f'tools.call({{ box: "{x.key}", name: "{tl.name}", args: {{...}} }})'})
        return _result({"results": out})

    # ------------------------------------------------------------------ the API, one method call at a time
    async def method(self, c: "Caller", name: str, args: Any) -> Any:
        if name not in METHODS:
            raise BoxError(f"unknown method {name}")
        if not isinstance(args, dict):
            raise BoxError("arguments must be one object")
        t = await self.router.targets(c)  # live grant every call (30 s cache, same as plain tool calls)
        if name == "boxes.list":
            return [{"box": x.key, "kind": x.kind, "name": x.name, "project_name": getattr(x, "project_name", None),
                     "calendar": x.calendar} for x in t.targets]
        box = args.get("box")
        target = t.find(box) if isinstance(box, str) else None
        if target is None:
            raise BoxError("box not in this connection" if box else "box is required")
        pool = self.router.pool
        if name == "boxes.health":
            tools = await pool.tools(c.token, c.exp, target, c.legacy)
            if any(tl.name == "box_health" for tl in tools):
                return await self.router._call_data(c, target, "box_health", {})
            await pool.run(c.token, c.exp, target, c.legacy, lambda s: s.send_ping(), timeout=pool.box_timeout)
            return {"ok": True, "note": "this box has no box_health tool yet"}
        if name == "tools.list":
            tools = await pool.tools(c.token, c.exp, target, c.legacy)
            return [{"name": tl.name, "description": (tl.description or "").split("\n")[0][:160],
                     "read_only": bool(tl.annotations and tl.annotations.readOnlyHint)} for tl in tools]
        if name == "tools.call":
            tool, targs = args.get("name"), args.get("args") or {}
            if not isinstance(tool, str) or not isinstance(targs, dict):
                raise BoxError("tools.call needs { box, name, args: {...} }")
            tools = {tl.name: tl for tl in await pool.tools(c.token, c.exp, target, c.legacy)}
            tl = tools.get(tool)
            if tl is None:
                raise BoxError(f"{tool} is not available on {target.key}")
            if not (tl.annotations and tl.annotations.readOnlyHint):
                raise BoxError(NEEDS_APPROVAL)
            return await self.router._call_data(c, target, tool, targs)
        tool = {"kanban.list": "kanban_list", "kanban.read": "kanban_read", "memory.read": "memory_read"}[name]
        fwd = {k: v for k, v in args.items() if k != "box"}
        tools = await pool.tools(c.token, c.exp, target, c.legacy)
        if not any(tl.name == tool for tl in tools):
            raise BoxError(f"{tool} is not available on {target.key}")
        return await self.router._call_data(c, target, tool, fwd)

    # ------------------------------------------------------------------ execute
    async def execute(self, c: "Caller", args: dict) -> types.CallToolResult:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return _result({"ok": False, "error": "code is required"}, error=True)
        if len(code) > MAX_CODE:
            return _result({"ok": False, "error": f"code is over {MAX_CODE} characters"}, error=True)
        async with self.runs:
            try:
                async with asyncio.timeout(self.wall):
                    done = await self._run(c, code)
            except TimeoutError:
                done = {"ok": False, "error": f"time limit reached ({self.wall:g} s in total)"}
        ok = bool(done.get("ok"))
        head = "Done." if ok else f"Failed: {done.get('error')}"
        return _result(done, error=not ok, head=head)

    async def _run(self, c: "Caller", code: str) -> dict:
        d = str(self.sandbox_dir)
        proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={}, cwd=d, limit=MAX_LINE)
        try:
            job = {"code": code, "methods": sorted(METHODS), "limits": self.limits}
            proc.stdin.write((json.dumps(job) + "\n").encode())
            await proc.stdin.drain()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    err = (await proc.stderr.read(2000)).decode(errors="replace")
                    log.warning("sandbox exited early: %s", err[:300])
                    return {"ok": False, "error": "the sandbox stopped unexpectedly"}
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "done" in msg:
                    return msg["done"] if isinstance(msg["done"], dict) else {"ok": False, "error": "bad answer"}
                call = msg.get("call")
                if not isinstance(call, dict):
                    continue
                try:
                    value = await self.method(c, str(call.get("method")), call.get("args"))
                    reply = {"id": call.get("id"), "ok": True, "value": value}
                except BoxError as e:
                    reply = {"id": call.get("id"), "ok": False, "error": str(e)[:500]}
                except Exception as e:  # never leak internals into the snippet
                    log.info("code mode method %s failed: %s", call.get("method"), type(e).__name__)
                    reply = {"id": call.get("id"), "ok": False, "error": "the box answered an error"}
                proc.stdin.write((json.dumps(reply, ensure_ascii=False, default=str) + "\n").encode())
                await proc.stdin.drain()
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()


__all__ = ["CODE_TOOLS", "CodeMode", "METHODS", "code_tools", "NEEDS_APPROVAL"]
