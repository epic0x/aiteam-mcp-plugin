"""GitHub = the Son's own gh CLI: argv building (no shell, no flag injection), runner (stdout verbatim, missing
login, timeout, output cap), connector wiring (on by default, full surface count, write annotations)."""
import json
import os
import stat
import sys
import textwrap

import pytest

from aiteam_connector.github_gh import (GITHUB_TOOLS, NOT_LOGGED_IN, SPECS, WRITE_TOOLS, GhError, GhRunner, build_argv,
                                        github_tools)
from aiteam_connector.server import Connector, SonLink

R = "--repo=epic0x/aiteam"

# One valid call per tool (also proves every tool builds) -> expected gh argv.
CASES = {
    "github_auth_status": ({}, ["auth", "status"]),
    "github_repo_list": ({"owner": "epic0x", "limit": 5}, ["repo", "list", "epic0x", "--limit=5"]),
    "github_repo_view": ({"repo": "epic0x/aiteam", "json": "name,url"}, ["repo", "view", "epic0x/aiteam", "--json=name,url"]),
    "github_issue_list": ({"repo": "epic0x/aiteam", "state": "all", "label": ["bug"]},
                          ["issue", "list", R, "--state=all", "--label=bug"]),
    "github_issue_view": ({"repo": "epic0x/aiteam", "number": 3, "comments": True}, ["issue", "view", "3", R, "--comments"]),
    "github_pr_list": ({"repo": "epic0x/aiteam", "state": "merged", "limit": 2, "base": "main"},
                       ["pr", "list", R, "--state=merged", "--limit=2", "--base=main"]),
    "github_pr_view": ({"repo": "epic0x/aiteam", "number": 9}, ["pr", "view", "9", R]),
    "github_pr_diff": ({"repo": "epic0x/aiteam", "number": 9, "name_only": True}, ["pr", "diff", "9", R, "--name-only"]),
    "github_pr_checks": ({"repo": "epic0x/aiteam", "number": 9}, ["pr", "checks", "9", R]),
    "github_run_list": ({"repo": "epic0x/aiteam", "branch": "main"}, ["run", "list", R, "--branch=main"]),
    "github_run_view": ({"repo": "epic0x/aiteam", "run_id": 123, "log_failed": True}, ["run", "view", "123", R, "--log-failed"]),
    "github_workflow_list": ({"repo": "epic0x/aiteam"}, ["workflow", "list", R]),
    "github_search": ({"kind": "prs", "query": "--help is:open"}, ["search", "prs", "--", "--help is:open"]),
    "github_issue_create": ({"repo": "epic0x/aiteam", "title": "-x; rm -rf /", "body": "$(id)", "label": ["bug"]},
                            ["issue", "create", R, "--title=-x; rm -rf /", "--body=$(id)", "--label=bug"]),
    "github_issue_comment": ({"repo": "epic0x/aiteam", "number": 3, "body": "--repo=evil/x"},
                             ["issue", "comment", "3", R, "--body=--repo=evil/x"]),
    "github_issue_edit": ({"repo": "epic0x/aiteam", "number": 3, "add_label": ["p1"]}, ["issue", "edit", "3", R, "--add-label=p1"]),
    "github_issue_close": ({"repo": "epic0x/aiteam", "number": 3, "reason": "not planned"},
                           ["issue", "close", "3", R, "--reason=not planned"]),
    "github_issue_reopen": ({"repo": "epic0x/aiteam", "number": 3}, ["issue", "reopen", "3", R]),
    "github_pr_create": ({"repo": "epic0x/aiteam", "head": "feat/x", "title": "T", "base": "main", "draft": True},
                         ["pr", "create", R, "--head=feat/x", "--title=T", "--body=", "--base=main", "--draft"]),
    "github_pr_comment": ({"repo": "epic0x/aiteam", "number": 9, "body": "LGTM"}, ["pr", "comment", "9", R, "--body=LGTM"]),
    "github_pr_edit": ({"repo": "epic0x/aiteam", "number": 9, "title": "New"}, ["pr", "edit", "9", R, "--title=New"]),
    "github_pr_review": ({"repo": "epic0x/aiteam", "number": 9, "event": "approve", "body": "ok"},
                         ["pr", "review", "9", R, "--approve", "--body=ok"]),
    "github_pr_merge": ({"repo": "epic0x/aiteam", "number": 9, "method": "squash", "match_head_commit": "a" * 40,
                         "delete_branch": True}, ["pr", "merge", "9", R, "--squash", "--match-head-commit=" + "a" * 40, "--delete-branch"]),
    "github_pr_close": ({"repo": "epic0x/aiteam", "number": 9}, ["pr", "close", "9", R]),
    "github_run_rerun": ({"repo": "epic0x/aiteam", "run_id": 123, "failed": True}, ["run", "rerun", "123", R, "--failed"]),
    "github_workflow_run": ({"repo": "epic0x/aiteam", "workflow": "deploy.yml", "ref": "main", "inputs": {"env": "prod"}},
                            ["workflow", "run", R, "--ref=main", "--raw-field=env=prod", "--", "deploy.yml"]),
    "github_api": ({"method": "POST", "path": "epic0x/aiteam/issues", "fields": {"title": "-t x"}},
                   ["api", "--method=POST", "--raw-field=title=-t x", "--", "repos/epic0x/aiteam/issues"]),
}


def test_every_tool_has_a_case_and_count_is_pinned():
    assert set(CASES) == set(GITHUB_TOOLS)
    assert len(GITHUB_TOOLS) == 27 and len(WRITE_TOOLS) == 14


@pytest.mark.parametrize("name", list(CASES))
def test_argv(name):
    args, want = CASES[name]
    argv, stdin = build_argv(name, args)
    assert argv == want
    assert all(isinstance(x, str) for x in argv)


def test_user_text_never_becomes_a_flag_or_positional():
    """Every free-text value is glued to its flag (--flag=value) or after `--`; never a bare argv item."""
    evil = "--repo=attacker/x"
    for name, (args, _) in CASES.items():
        for k, v in args.items():
            if isinstance(v, str) and SPECS[name][5].get(k, {}).get("type") == "string" and "pattern" not in SPECS[name][5][k]:
                if "enum" in SPECS[name][5][k]:
                    continue
                try:
                    argv, _ = build_argv(name, {**args, k: evil})
                except GhError:
                    continue  # refused outright is also safe
                assert evil not in argv or argv[argv.index(evil) - 1] == "--", (name, k, argv)


@pytest.mark.parametrize("name,args,msg", [
    ("github_issue_list", {"repo": "../../x"}, "owner/name"),
    ("github_issue_list", {"repo": "a b/c"}, "owner/name"),
    ("github_issue_list", {"repo": "--help/x"}, "owner/name"),
    ("github_issue_view", {"repo": "epic0x/aiteam", "number": "3; rm"}, "positive integer"),
    ("github_issue_view", {"repo": "epic0x/aiteam", "number": 0}, "positive integer"),
    ("github_pr_list", {"repo": "epic0x/aiteam", "base": "--exec"}, "invalid characters"),
    ("github_pr_list", {"repo": "epic0x/aiteam", "json": "a;b"}, "comma-separated"),
    ("github_pr_merge", {"repo": "epic0x/aiteam", "number": 1, "method": "force"}, "merge, squash or rebase"),
    ("github_pr_merge", {"repo": "epic0x/aiteam", "number": 1, "match_head_commit": "abc"}, "40-char"),
    ("github_pr_create", {"repo": "epic0x/aiteam", "head": "--force", "title": "t"}, "pushed branch"),
    ("github_api", {"method": "TRACE", "path": "/user"}, "method must be"),
    ("github_api", {"path": "-X"}, "path must start"),
    ("github_api", {"path": "/repos/../../etc"}, "path must start"),
    ("github_api", {"path": "/user", "fields": {"--x": "1"}}, "invalid characters"),
    ("github_issue_list", {"repo": "epic0x/aiteam", "shell": "true"}, "unknown argument"),
    ("github_issue_edit", {"repo": "epic0x/aiteam", "number": 3}, "nothing to change"),
    ("github_issue_comment", {"repo": "epic0x/aiteam", "number": 3, "body": " "}, "body is required"),
])
def test_bad_input_refused(name, args, msg):
    with pytest.raises(GhError, match=msg):
        build_argv(name, args)


def test_api_paths_and_body():
    assert build_argv("github_api", {"path": "/user"})[0] == ["api", "--method=GET", "--", "/user"]
    argv, stdin = build_argv("github_api", {"method": "patch", "path": "/repos/o/r/issues/1", "body": {"state": "closed"},
                                            "paginate": True, "jq": ".id"})
    assert argv == ["api", "--method=PATCH", "--input=-", "--paginate", "--jq=.id", "--", "/repos/o/r/issues/1"]
    assert json.loads(stdin) == {"state": "closed"}


def test_annotations_reads_and_writes():
    ts = {t.name: t for t in github_tools()}
    assert len(ts) == 27
    for n, t in ts.items():
        assert t.title and t.annotations.openWorldHint is True
        assert t.annotations.readOnlyHint is (n not in WRITE_TOOLS)
    assert ts["github_pr_merge"].annotations.destructiveHint is True
    assert ts["github_api"].annotations.readOnlyHint is False


# --------------------------------------------------------------------------------------------- runner, fake gh
def fake_gh(tmp_path, body: str) -> str:
    p = tmp_path / "gh"
    p.write_text(f"#!{sys.executable}\nimport sys, json, os, time\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


ECHO = """
data = sys.stdin.read() if "--input=-" in sys.argv else ""
print(json.dumps({"argv": sys.argv[1:], "stdin": data, "prompt": os.environ.get("GH_PROMPT_DISABLED")}))
"""


async def test_runner_returns_gh_stdout_verbatim(tmp_path):
    r = GhRunner({"gh_bin": fake_gh(tmp_path, ECHO)})
    ok, out = await r.call("github_api", {"method": "POST", "path": "/x", "body": {"a": 1}})
    d = json.loads(out)
    assert ok and d["argv"] == ["api", "--method=POST", "--input=-", "--", "/x"] and json.loads(d["stdin"]) == {"a": 1}
    assert d["prompt"] == "1"


async def test_missing_login_says_next_step(tmp_path):
    gh = fake_gh(tmp_path, """
sys.stderr.write("You are not logged into any GitHub hosts. To log in, run: gh auth login\\n"); sys.exit(4)
""")
    ok, out = await GhRunner({"gh_bin": gh, "connect_url": "https://aiteam.space/my-sons/abc?connect=github"}).call(
        "github_auth_status", {})
    assert not ok and out.startswith("Connect GitHub to your Son: follow the guide at https://aiteam.space/my-sons/abc?connect=github.")
    assert "exit 4" in out and "not logged into any GitHub hosts" in out, "gh's own message stays underneath"
    ok, out = await GhRunner({"gh_bin": gh}).call("github_auth_status", {})
    assert out.startswith(NOT_LOGGED_IN.format(url="https://aiteam.space/my-sons"))


async def test_gh_missing_entirely(tmp_path, monkeypatch):
    import aiteam_connector.github_gh as G
    monkeypatch.setattr(G.shutil, "which", lambda *a, **k: None)  # CI runners ship gh in /usr/bin
    with pytest.raises(GhError, match="(?s)Connect GitHub to your Son.*not installed"):
        await GhRunner({}, env={"PATH": str(tmp_path), "HOME": str(tmp_path)}).call("github_auth_status", {})
    with pytest.raises(GhError, match="(?s)Connect GitHub to your Son.*not installed"):
        await GhRunner({"gh_bin": str(tmp_path / "nope" / "gh")}).call("github_auth_status", {})


async def test_gh_error_is_passed_through_with_hint(tmp_path):
    gh = fake_gh(tmp_path, """
sys.stderr.write("GraphQL: Could not resolve to a Repository with the name 'epic0x/nope'. (repository)\\n"); sys.exit(1)
""")
    ok, out = await GhRunner({"gh_bin": gh}).call("github_pr_list", {"repo": "epic0x/nope"})
    assert not ok and "Could not resolve to a Repository" in out and "Next step:" in out


async def test_timeout(tmp_path):
    gh = fake_gh(tmp_path, "time.sleep(5)\n")
    with pytest.raises(GhError, match="longer than 1 s.*Narrow"):
        await GhRunner({"gh_bin": gh, "timeout_s": 1}).call("github_auth_status", {})


async def test_output_cap_says_so(tmp_path):
    gh = fake_gh(tmp_path, "print('x' * 5000)\n")
    ok, out = await GhRunner({"gh_bin": gh, "max_output_chars": 100}).call("github_auth_status", {})
    assert ok and out.startswith("x" * 100) and "[cut:" in out and "smaller limit" in out


# --------------------------------------------------------------------------------------------- connector wiring
class StubRunner:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return (name != "github_pr_merge"), f"gh says {name}"


async def test_github_on_by_default_full_surface():
    """Both gateway model tools and secure AI sign-in tools remain available."""
    import mcp.types as types
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "all", gh_runner=StubRunner())

    class Up:
        async def tools(self):
            names = ["sessions_list", "session_status", "sessions_history", "sessions_search", "sessions_send",
                     "attachments_fetch", "channels_list", "conversation_get", "conversations_list", "events_poll",
                     "events_wait", "messages_read", "messages_send", "permissions_list_open", "permissions_respond"]
            return [types.Tool(name=n, inputSchema={"type": "object"}) for n in names]
    conn.upstreams["my-son"] = Up()
    names = [t.name for t in await conn.list_tools()]
    assert len(names) == 64 == len(set(names))
    assert set(GITHUB_TOOLS) <= set(names)


async def test_connector_routes_to_gh_and_flags_errors():
    stub = StubRunner()
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "none", gh_runner=stub)
    r = await conn.call("github_pr_list", {"repo": "epic0x/aiteam", "son": "my-son"})
    assert not r.isError and r.content[0].text == "gh says github_pr_list"
    assert stub.calls == [("github_pr_list", {"repo": "epic0x/aiteam"})]
    r = await conn.call("github_pr_merge", {"repo": "epic0x/aiteam", "number": 1})
    assert r.isError


async def test_bad_input_is_an_error_result_not_a_crash():
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "none", github={"gh_bin": "/nonexistent/gh"})
    r = await conn.call("github_issue_list", {"repo": "../x"})
    assert r.isError and "owner/name" in r.content[0].text


def test_github_false_turns_it_off():
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "none", github=False)
    assert conn.github is None


# --------------------------------------------------------------------------------------------- org allowlist (off by default)
def test_allowlist_off_by_default():
    assert GhRunner({}).allowed_owners is None
    assert build_argv("github_pr_list", {"repo": "someone/else"})[0][:2] == ["pr", "list"]


@pytest.mark.parametrize("name,args", [
    ("github_pr_list", {"repo": "someone/private"}),
    ("github_pr_merge", {"repo": "other-org/x", "number": 1}),
    ("github_repo_list", {"owner": "other-org"}),
    ("github_repo_list", {}),
    ("github_api", {"path": "/repos/other-org/x/issues", "method": "POST"}),
    ("github_api", {"path": "other-org/x"}),
    ("github_api", {"path": "/orgs/other-org/members"}),
    ("github_api", {"path": "/user/repos", "method": "POST"}),
    ("github_api", {"path": "/graphql", "method": "POST"}),
])
def test_allowlist_refuses_other_owners(name, args):
    with pytest.raises(GhError, match="epic0x"):
        build_argv(name, args, {"epic0x"})


def test_allowlist_allows_and_scopes_search():
    A = {"epic0x"}
    assert build_argv("github_pr_list", {"repo": "Epic0x/aiteam"}, A)[0][:2] == ["pr", "list"]
    assert build_argv("github_api", {"path": "/repos/epic0x/aiteam"}, A)[0][-1] == "/repos/epic0x/aiteam"
    assert build_argv("github_api", {"path": "/user"}, A)[0][-1] == "/user"
    argv, _ = build_argv("github_search", {"kind": "prs", "query": "is:open"}, A)
    assert argv == ["search", "prs", "--owner=epic0x", "--", "is:open"]


async def test_allowlist_from_config(tmp_path):
    r = GhRunner({"gh_bin": fake_gh(tmp_path, ECHO), "allowed_owners": ["epic0x"]})
    with pytest.raises(GhError, match="outside that list"):
        await r.call("github_issue_list", {"repo": "other/x"})
    ok, _ = await r.call("github_issue_list", {"repo": "epic0x/x"})
    assert ok


# --------------------------------------------------------------------------------------------- old tool names
async def test_legacy_names_route_to_gh(tmp_path):
    """Chats that still hold the pre-2026-10-12 tool list call github_list_issues etc.: answer them via gh."""
    from aiteam_connector.github_gh import LEGACY_TOOLS
    r = GhRunner({"gh_bin": fake_gh(tmp_path, ECHO)})
    want = {
        "github_list_issues": ({"repo": "epic0x/aiteam", "labels": "bug, p1", "limit": 5},
                               ["issue", "list", R, "--limit=5", "--label=bug", "--label=p1"]),
        "github_get_issue": ({"repo": "epic0x/aiteam", "number": 3}, ["issue", "view", "3", R, "--comments"]),
        "github_list_pull_requests": ({"repo": "epic0x/aiteam", "state": "all"}, ["pr", "list", R, "--state=all"]),
        "github_get_pull_request": ({"repo": "epic0x/aiteam", "number": 9}, ["pr", "view", "9", R]),
        "github_list_commits": ({"repo": "epic0x/aiteam", "branch": "main", "limit": 2},
                                ["api", "--method=GET", "--", "/repos/epic0x/aiteam/commits?per_page=2&sha=main"]),
    }
    assert set(want) == set(LEGACY_TOOLS)
    for name, (args, argv) in want.items():
        ok, out = await r.call(name, args)
        assert ok and json.loads(out)["argv"] == argv, name
    with pytest.raises(GhError):
        await r.call("github_list_commits", {"repo": "epic0x/aiteam", "path": "a;b"})


async def test_legacy_names_not_listed_but_routed_by_connector():
    stub = StubRunner()
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "none", gh_runner=stub)
    assert "github_list_issues" not in [t.name for t in await conn.list_tools()]
    r = await conn.call("github_list_issues", {"repo": "epic0x/aiteam"})
    assert not r.isError and stub.calls == [("github_list_issues", {"repo": "epic0x/aiteam"})]


@pytest.mark.parametrize("name,args", [
    ("github_pr_list", {"repo": "epic0x/aiteam", "limit": "lots"}),
    ("github_issue_list", {"repo": "epic0x/aiteam", "limit": [3]}),
    ("github_list_commits", {"repo": "epic0x/aiteam", "limit": "ten"}),
    ("github_list_commits", {"repo": "epic0x/aiteam", "limit": 500}),
])
async def test_bad_limit_is_a_plain_message(name, args):
    """review of #14: a non-number limit must not crash with a raw ValueError."""
    conn = Connector([SonLink("my-son", "http://box.invalid", "t")], "none", gh_runner=GhRunner({"gh_bin": "/bin/false"}))
    r = await conn.call(name, args)
    assert r.isError and r.content[0].text.startswith("limit must be"), r.content[0].text
