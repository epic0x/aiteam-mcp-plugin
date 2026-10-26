"""Live parity test: the connector's GitHub tools vs the Son's own `gh`, byte for byte.

Usage: python parity_live.py <mcp-url> [bearer-file]
Runs each case twice: once as a tool call through MCP, once as the same `gh` argv locally (same user, same login),
and compares the text exactly. Prints one line per case plus a sha256 of each side. Exit 1 on any mismatch.
"""
import asyncio
import hashlib
import json
import subprocess
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from aiteam_connector.github_gh import NOT_LOGGED_IN, build_argv  # noqa: F401  (same argv the connector builds)

CASES = [
    ("github_auth_status", {}),
    ("github_pr_list", {"repo": "epic0x/aiteam-mcp-plugin", "state": "all", "limit": 5}),
        ("github_issue_list", {"repo": "epic0x/aiteam-mcp-plugin", "state": "all", "limit": 5}),
    ("github_run_list", {"repo": "epic0x/aiteam-mcp-plugin", "limit": 3, "json": "databaseId,headSha,conclusion,name"}),
    ("github_api", {"path": "/repos/epic0x/aiteam-mcp-plugin", "jq": "{full_name, private, default_branch}"}),
    ("github_api", {"path": "epic0x/aiteam-mcp-plugin/commits?per_page=3", "jq": "[.[].sha]"}),
    ("github_repo_view", {"repo": "epic0x/aiteam-mcp-plugin", "json": "name,visibility,defaultBranchRef"}),
]


def local(name, args):
    argv, stdin = build_argv(name, args)
    p = subprocess.run(["gh", *argv], input=stdin, capture_output=True, timeout=60,
                       env={**__import__("os").environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_PAGER": "cat"})
    out = p.stdout.decode() or p.stderr.decode()
    return p.returncode == 0, out, argv


async def main(url, bearer_file=None):
    headers = {"Authorization": "Bearer " + open(bearer_file).read().strip()} if bearer_file else None
    bad = 0
    async with streamablehttp_client(url, headers=headers, timeout=60) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            gh = [t.name for t in tools if t.name.startswith("github_")]
            print(f"tools: {len(tools)} total, {len(gh)} github_*")
            for name, args in CASES:
                res = await s.call_tool(name, args)
                mcp_text = res.content[0].text
                ok, gh_text, argv = local(name, args)
                same = (mcp_text == gh_text) and (not res.isError) == ok
                bad += not same
                h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
                print(f"{'SAME' if same else 'DIFF'}  gh {' '.join(argv)}  bytes={len(gh_text.encode())}  "
                      f"mcp={h(mcp_text)} gh={h(gh_text)}")
                if not same:
                    print("  MCP:", json.dumps(mcp_text[:300]), "\n  GH :", json.dumps(gh_text[:300]))
    print("RESULT:", "PASS" if not bad else f"FAIL ({bad} diffs)")
    return bad


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main(*sys.argv[1:])) else 0)
