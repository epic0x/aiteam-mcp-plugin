"""Live E2E: an outside MCP client drives ONE Son directly through the connector (no coordinator).

Usage: python e2e_live.py http://127.0.0.1:8720/mcp [son] [bearer-token-file]
Prints a JSON report; exit 1 on any failed check. The throwaway conversation names are random, so the
run never touches the owner's chat sessions.
"""
import asyncio
import json
import secrets
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str, son: str, token_file: str = "") -> int:
    conv = "e2e-" + secrets.token_hex(3)
    checks, rep = [], {"url": url, "son": son, "conversation": conv}

    def ok(name, cond, detail=""):
        checks.append({"check": name, "pass": bool(cond), "detail": str(detail)[:200]})

    progress = []

    async def on_progress(p, total, message):
        progress.append(message)

    headers = {"Authorization": "Bearer " + open(token_file).read().strip()} if token_file else None
    async with streamablehttp_client(url, headers=headers, timeout=30, sse_read_timeout=900) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            ok("initialize", init.serverInfo.name == "aiteam-connector", init.serverInfo)
            tools = {t.name: t for t in (await s.list_tools()).tools}
            rep["tools"] = sorted(tools)
            ok("native tools", {"son_send", "son_list", "son_conversations", "son_conversation_history"} <= set(tools))
            rep["tool_count"] = len(tools)
            ok("session tools folded in", {"sessions_list", "session_status", "sessions_history", "sessions_search", "sessions_send"} <= set(tools))
            ok("annotations", all(t.annotations and t.title for t in tools.values()))
            res = await s.call_tool("son_list", {})
            ok("son online", json.loads(res.content[0].text)["sons"][0]["online"], res.content[0].text)

            t0 = time.time()
            word = "kiwi-" + secrets.token_hex(2)
            res = await s.call_tool("son_send", {"message": f"E2E connector test. Remember the codeword {word}. Reply with exactly: READY {word}",
                                                 "conversation": conv}, progress_callback=on_progress)
            t1 = res.content[0].text
            rep["turn1"] = {"secs": round(time.time() - t0, 1), "reply": t1[:200]}
            ok("turn 1 reply", (not res.isError) and f"READY {word}" in t1, t1)

            t0 = time.time()
            res = await s.call_tool("son_send", {"message": "What was the codeword I gave you? Reply with only the codeword.",
                                                 "conversation": conv})
            t2 = res.content[0].text
            rep["turn2"] = {"secs": round(time.time() - t0, 1), "reply": t2[:200]}
            ok("turn 2 remembers (same session)", word in t2, t2)

            res = await s.call_tool("son_conversation_history", {"conversation": conv, "limit": 10})
            h = json.loads(res.content[0].text)
            rep["session_id"] = h.get("session_id")
            ok("history via box", len(h.get("messages", [])) >= 4 and h["session_id"].startswith("box_"), len(h.get("messages", [])))

            res = await s.call_tool("sessions_list", {"limit": 50, "search": h["session_id"][:12]})
            ok("sessions_list (OpenClaw tool) sees the box session", h["session_id"] in res.content[0].text, res.content[0].text[:200])
            res = await s.call_tool("sessions_history", {"sessionKey": h["session_id"], "limit": 10})
            ok("sessions_history (OpenClaw tool)", word in res.content[0].text, res.content[0].text[:200])
            res = await s.call_tool("sessions_search", {"query": word, "limit": 5})
            ok("sessions_search (OpenClaw tool)", h["session_id"] in res.content[0].text, res.content[0].text[:200])
    rep["checks"] = checks
    rep["passed"] = sum(c["pass"] for c in checks)
    rep["total"] = len(checks)
    print(json.dumps(rep, indent=1))
    return 0 if rep["passed"] == rep["total"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8720/mcp",
                              sys.argv[2] if len(sys.argv) > 2 else "my-son",
                              sys.argv[3] if len(sys.argv) > 3 else "")))
