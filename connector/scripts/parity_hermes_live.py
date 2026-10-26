"""Public E2E: call Hermes toolbox tools through https://<son>.aiteam.space/mcp (gate -> connector -> Son)."""
import asyncio, json, sys
import httpx
from mcp import ClientSession
from mcp.client import streamable_http as sh

URL = sys.argv[1]
TOK = open(sys.argv[2]).read().strip()
CALLS = [("hermes_toolbox_status", {}),
         ("terminal", {"command": "echo parity-ok; hostname"}),
         ("read_file", {"path": "SOUL.md", "limit": 1}),
         ("cronjob_manage", {"action": "list"}),
         ("kanban_list", {}),
         ("skills_list", {}),
         ("discord_admin", {"action": "list_guilds"}),
         ("mnemosyne_stats", {}),
         ("session_search", {"query": "MCP parity", "limit": 1}),
         ("web_search", {"query": "model context protocol", "limit": 1}),
         ("github_auth_status", {}),
         ("son_list", {})]


async def main():
    cm = sh.streamable_http_client(URL, http_client=httpx.AsyncClient(
        headers={"Authorization": "Bearer " + TOK}, timeout=httpx.Timeout(300, connect=20)))
    ok = 0
    async with cm as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print("TOOLS", len(tools))
            ann = {t.name: t.annotations for t in tools}
            print("terminal annotations:", ann.get("terminal"))
            for n, a in CALLS:
                r = await s.call_tool(n, a)
                err = getattr(r, "is_error", getattr(r, "isError", None))
                txt = r.content[0].text if r.content else ""
                if n == "hermes_toolbox_status":
                    txt = str(json.loads(txt)["exposed_count"]) + " exposed"
                ok += 0 if err else 1
                print(f"{'PASS' if not err else 'FAIL'} {n}: {txt[:160]!r}")
    print(f"{ok}/{len(CALLS)} passed")

asyncio.run(main())
