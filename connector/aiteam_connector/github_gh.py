"""GitHub tools that mirror the Son's own ``gh`` CLI (owner 2026-10-12: "github mcp should mirror agent gh cli
response ... same permissions as agents ... Everything in mcp should be default on").

How it works:
  * Every tool runs the Son's own ``gh`` binary with the Son's own login (``gh auth login`` on the box). No GitHub
    App, no aiteam.space token exchange, no separate OAuth scope: whatever ``gh`` can do on this Son, the MCP can
    do, and the answer is ``gh``'s own stdout, byte for byte.
  * Read AND write: issues, pull requests, comments, create, edit, close, merge, review, repos, Actions runs and
    workflows, plus ``github_api`` (a raw ``gh api`` passthrough) for anything without a typed tool.

Safety rules (review checklist, 2026-10-12):
  1. No shell, ever. ``asyncio.create_subprocess_exec(gh, *argv)`` with a list. Repo / ref / number inputs are
     validated by regex or int. Free text is always passed glued to its flag (``--title=<text>``) or on stdin, so
     nothing a user types can become a ``gh`` flag or a second command.
  2. No login on the Son -> "Connect GitHub to your Son: follow the guide at <connect_url>." with gh's own message
     underneath (plain words first for non-technical owners).
  3. No new consent: these tools need only the MCP access the connector already has (any token the gate accepts).
  4. Every call is capped (default 30 s, 60 000 chars). A cut answer says so and how to narrow it.

Config (connector JSON, optional; GitHub is ON unless ``"github": false``):
  "github": {"gh_bin": "/path/to/gh", "timeout_s": 30, "max_output_chars": 60000,
             "connect_url": "https://aiteam.space/my-sons/<son id>?connect=github",   # where the no-login message points
             "allowed_owners": ["epic0x"]}   # OFF by default (= full gh parity). Set it on Sons whose gh is
                                             # logged in as a person with wide access, to keep the MCP to those orgs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Any, Optional

import mcp.types as types

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
HEAD_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9-]{0,38}:)?[A-Za-z0-9._/][A-Za-z0-9._/-]{0,199}$")
JSON_FIELDS_RE = re.compile(r"^[A-Za-z]+(?:,[A-Za-z]+)*$")
FIELD_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\[\]-]{0,99}$")
API_PATH_RE = re.compile(r"^[A-Za-z0-9._~/{}:@!$&'()*+,;=%?-]{1,500}$")
API_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")
DEFAULT_CONNECT_URL = "https://aiteam.space/my-sons"
NOT_LOGGED_IN = "Connect GitHub to your Son: follow the guide at {url}."


class GhError(Exception):
    pass


# --------------------------------------------------------------------------------------------- schema helpers
_REPO = {"type": "string", "pattern": REPO_RE.pattern, "description": "Repository as owner/name, e.g. epic0x/aiteam."}
_NUM = {"type": "integer", "minimum": 1}
_STATE = {"type": "string", "enum": ["open", "closed", "all"], "description": "Default: open."}
_PR_STATE = {"type": "string", "enum": ["open", "closed", "merged", "all"], "description": "Default: open."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Max items (gh --limit). Default: gh's (30)."}
_JSON = {"type": "string", "pattern": JSON_FIELDS_RE.pattern,
         "description": "Optional comma-separated fields for gh --json (e.g. number,title,state). Omit for gh's text output."}
_TEXT = lambda d: {"type": "string", "maxLength": 65536, "description": d}  # noqa: E731
_REF = lambda d: {"type": "string", "pattern": REF_RE.pattern, "description": d}  # noqa: E731
_LIST = lambda d: {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 50, "description": d}  # noqa: E731


def _obj(req: list[str], props: dict) -> dict:
    return {"type": "object", "required": req, "additionalProperties": False, "properties": props}


def _ann(title: str, ro: bool, destructive: bool = False) -> types.ToolAnnotations:
    return types.ToolAnnotations(title=title, readOnlyHint=ro, destructiveHint=destructive, idempotentHint=ro,
                                 openWorldHint=True)


# name: (title, read_only, destructive, description, required, properties)
SPECS: dict[str, tuple] = {
    "github_auth_status": ("GitHub: who am I", True, False, "`gh auth status`: which GitHub account this Son uses.", [], {}),
    "github_repo_list": ("GitHub: list repos", True, False, "`gh repo list [owner]`.", [],
                         {"owner": {"type": "string", "pattern": OWNER_RE.pattern}, "limit": _LIMIT, "json": _JSON}),
    "github_repo_view": ("GitHub: view a repo", True, False, "`gh repo view <repo>`.", ["repo"], {"repo": _REPO, "json": _JSON}),
    "github_issue_list": ("GitHub: list issues", True, False, "`gh issue list -R <repo>`.", ["repo"],
                          {"repo": _REPO, "state": _STATE, "limit": _LIMIT, "label": _LIST("Filter by labels."),
                           "assignee": {"type": "string", "maxLength": 100}, "search": _TEXT("GitHub search query."),
                           "json": _JSON}),
    "github_issue_view": ("GitHub: view an issue", True, False, "`gh issue view <n> -R <repo>` (with --comments if asked).",
                          ["repo", "number"], {"repo": _REPO, "number": _NUM, "comments": {"type": "boolean"}, "json": _JSON}),
    "github_pr_list": ("GitHub: list pull requests", True, False, "`gh pr list -R <repo>`.", ["repo"],
                       {"repo": _REPO, "state": _PR_STATE, "limit": _LIMIT, "base": _REF("Base branch."),
                        "head": _REF("Head branch."), "author": {"type": "string", "maxLength": 100},
                        "search": _TEXT("GitHub search query."), "json": _JSON}),
    "github_pr_view": ("GitHub: view a pull request", True, False, "`gh pr view <n> -R <repo>` (with --comments if asked).",
                       ["repo", "number"], {"repo": _REPO, "number": _NUM, "comments": {"type": "boolean"}, "json": _JSON}),
    "github_pr_diff": ("GitHub: pull request diff", True, False, "`gh pr diff <n> -R <repo>` (or --name-only).",
                       ["repo", "number"], {"repo": _REPO, "number": _NUM, "name_only": {"type": "boolean"}}),
    "github_pr_checks": ("GitHub: pull request checks", True, False, "`gh pr checks <n> -R <repo>`.",
                         ["repo", "number"], {"repo": _REPO, "number": _NUM, "json": _JSON}),
    "github_run_list": ("GitHub: list Actions runs", True, False, "`gh run list -R <repo>`.", ["repo"],
                        {"repo": _REPO, "limit": _LIMIT, "branch": _REF("Branch."), "workflow": _REF("Workflow file or name."),
                         "json": _JSON}),
    "github_run_view": ("GitHub: view an Actions run", True, False, "`gh run view <id> -R <repo>` (--log-failed if asked).",
                        ["repo", "run_id"], {"repo": _REPO, "run_id": _NUM, "log_failed": {"type": "boolean"}, "json": _JSON}),
    "github_workflow_list": ("GitHub: list workflows", True, False, "`gh workflow list -R <repo>`.", ["repo"],
                             {"repo": _REPO, "json": _JSON}),
    "github_search": ("GitHub: search", True, False, "`gh search issues|prs|repos|code|commits <query>`.", ["kind", "query"],
                      {"kind": {"type": "string", "enum": ["issues", "prs", "repos", "code", "commits"]},
                       "query": _TEXT("Search query."), "limit": _LIMIT, "json": _JSON}),
    # ---- writes
    "github_issue_create": ("GitHub: create an issue", False, False, "`gh issue create -R <repo> --title --body`.",
                            ["repo", "title"], {"repo": _REPO, "title": _TEXT("Title."), "body": _TEXT("Body (markdown)."),
                                                "label": _LIST("Labels."), "assignee": _LIST("Assignees (logins or @me).")}),
    "github_issue_comment": ("GitHub: comment on an issue", False, False, "`gh issue comment <n> -R <repo> --body`.",
                             ["repo", "number", "body"], {"repo": _REPO, "number": _NUM, "body": _TEXT("Comment (markdown).")}),
    "github_issue_edit": ("GitHub: edit an issue", False, False, "`gh issue edit <n> -R <repo>`.", ["repo", "number"],
                          {"repo": _REPO, "number": _NUM, "title": _TEXT("New title."), "body": _TEXT("New body."),
                           "add_label": _LIST("Labels to add."), "remove_label": _LIST("Labels to remove."),
                           "add_assignee": _LIST("Assignees to add."), "remove_assignee": _LIST("Assignees to remove.")}),
    "github_issue_close": ("GitHub: close an issue", False, False, "`gh issue close <n> -R <repo>`.", ["repo", "number"],
                           {"repo": _REPO, "number": _NUM, "comment": _TEXT("Optional closing comment."),
                            "reason": {"type": "string", "enum": ["completed", "not planned"]}}),
    "github_issue_reopen": ("GitHub: reopen an issue", False, False, "`gh issue reopen <n> -R <repo>`.", ["repo", "number"],
                            {"repo": _REPO, "number": _NUM}),
    "github_pr_create": ("GitHub: open a pull request", False, False,
                         "`gh pr create -R <repo> --head --base --title --body` (the head branch must already be pushed).",
                         ["repo", "head", "title"], {"repo": _REPO, "head": {"type": "string", "maxLength": 240, "description": "Head branch (or owner:branch for a fork)."},
                                                     "base": _REF("Base branch. Default: the repo's default."),
                                                     "title": _TEXT("Title."), "body": _TEXT("Body (markdown)."),
                                                     "draft": {"type": "boolean"}}),
    "github_pr_comment": ("GitHub: comment on a pull request", False, False, "`gh pr comment <n> -R <repo> --body`.",
                          ["repo", "number", "body"], {"repo": _REPO, "number": _NUM, "body": _TEXT("Comment (markdown).")}),
    "github_pr_edit": ("GitHub: edit a pull request", False, False, "`gh pr edit <n> -R <repo>`.", ["repo", "number"],
                       {"repo": _REPO, "number": _NUM, "title": _TEXT("New title."), "body": _TEXT("New body."),
                        "base": _REF("New base branch."), "add_label": _LIST("Labels to add."),
                        "remove_label": _LIST("Labels to remove.")}),
    "github_pr_review": ("GitHub: review a pull request", False, False, "`gh pr review <n> -R <repo> --approve|--comment|--request-changes`.",
                         ["repo", "number", "event"], {"repo": _REPO, "number": _NUM,
                                                       "event": {"type": "string", "enum": ["approve", "comment", "request-changes"]},
                                                       "body": _TEXT("Review text.")}),
    "github_pr_merge": ("GitHub: merge a pull request", False, True, "`gh pr merge <n> -R <repo> --merge|--squash|--rebase`.",
                        ["repo", "number"], {"repo": _REPO, "number": _NUM,
                                             "method": {"type": "string", "enum": ["merge", "squash", "rebase"], "description": "Default: merge."},
                                             "match_head_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                                             "delete_branch": {"type": "boolean"}, "auto": {"type": "boolean"}}),
    "github_pr_close": ("GitHub: close a pull request", False, True, "`gh pr close <n> -R <repo>`.", ["repo", "number"],
                        {"repo": _REPO, "number": _NUM, "comment": _TEXT("Optional closing comment."),
                         "delete_branch": {"type": "boolean"}}),
    "github_run_rerun": ("GitHub: re-run an Actions run", False, False, "`gh run rerun <id> -R <repo>` (--failed if asked).",
                         ["repo", "run_id"], {"repo": _REPO, "run_id": _NUM, "failed": {"type": "boolean"}}),
    "github_workflow_run": ("GitHub: start a workflow", False, False, "`gh workflow run <workflow> -R <repo> [--ref]`.",
                            ["repo", "workflow"], {"repo": _REPO, "workflow": _REF("Workflow file or name."), "ref": _REF("Branch or tag."),
                                                   "inputs": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 4000}}}),
    "github_api": ("GitHub: raw API call", False, True,
                   "`gh api` passthrough for anything without a typed tool. method GET/POST/PATCH/PUT/DELETE. path starts "
                   "with / (e.g. /user, /orgs/epic0x/repos) or is relative to repos/ (e.g. epic0x/aiteam/pulls?state=all).",
                   ["path"], {"method": {"type": "string", "enum": list(API_METHODS), "description": "Default: GET."},
                              "path": {"type": "string", "pattern": API_PATH_RE.pattern},
                              "fields": {"type": "object", "description": "String parameters (gh api -f key=value)."},
                              "body": {"type": "object", "description": "Raw JSON request body (gh api --input -)."},
                              "paginate": {"type": "boolean"}, "jq": _TEXT("jq filter (gh api --jq).")}),
}
# Names of the 5 read-only tools before 2026-10-12 (github_read.py). Claude keeps a chat's old tool list, so those
# chats still call these names; route them to the gh tools instead of an "unknown tool" error. Not listed.
LEGACY_TOOLS = ("github_list_issues", "github_get_issue", "github_list_pull_requests", "github_get_pull_request",
                "github_list_commits")


def legacy_to_gh(name: str, a: dict) -> tuple[str, dict]:
    a = dict(a or {})
    keep = lambda *ks: {k: a[k] for k in ks if a.get(k) not in (None, "")}  # noqa: E731
    if name == "github_list_issues":
        out = keep("repo", "state", "limit")
        if a.get("labels"):
            out["label"] = [x.strip() for x in str(a["labels"]).split(",") if x.strip()]
        return "github_issue_list", out
    if name == "github_get_issue":
        return "github_issue_view", {**keep("repo", "number"), **({"comments": True} if a.get("comments", 10) else {})}
    if name == "github_list_pull_requests":
        return "github_pr_list", keep("repo", "state", "limit")
    if name == "github_get_pull_request":
        return "github_pr_view", keep("repo", "number")
    if name == "github_list_commits":
        repo = str(a.get("repo") or "")
        if not REPO_RE.match(repo):
            raise GhError("repo must be owner/name, e.g. epic0x/aiteam")
        q = [f"per_page={_int(a.get('limit'), 'limit', 1, 100, default=20)}"]
        if a.get("branch"):
            if not REF_RE.match(str(a["branch"])):
                raise GhError("branch has invalid characters")
            q.append(f"sha={a['branch']}")
        if a.get("path"):
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,300}", str(a["path"])):
                raise GhError("path has invalid characters")
            q.append(f"path={a['path']}")
        if a.get("since"):
            if not re.fullmatch(r"[0-9T:.Z+-]{10,40}", str(a["since"])):
                raise GhError("since must be ISO 8601, e.g. 2026-10-01T00:00:00Z")
            q.append(f"since={a['since']}")
        return "github_api", {"path": f"/repos/{repo}/commits?" + "&".join(q)}
    raise GhError(f"unknown GitHub tool {name}")


GITHUB_TOOLS = tuple(SPECS)
WRITE_TOOLS = tuple(n for n, s in SPECS.items() if not s[1])


def github_tools() -> list[types.Tool]:
    return [types.Tool(name=n, title=t, description=f"Runs the Son's own GitHub CLI login: {d}",
                       inputSchema=_obj(req, props), annotations=_ann(t, ro, destr))
            for n, (t, ro, destr, d, req, props) in SPECS.items()]


# --------------------------------------------------------------------------------------------- argv building
def _repo(a: dict) -> str:
    r = str(a.get("repo") or "")
    if not REPO_RE.match(r):
        raise GhError("repo must be owner/name, e.g. epic0x/aiteam")
    return r


def _num(a: dict, key: str = "number") -> str:
    try:
        n = int(a.get(key))
    except (TypeError, ValueError):
        raise GhError(f"{key} must be a positive integer") from None
    if n < 1:
        raise GhError(f"{key} must be a positive integer")
    return str(n)


def _ref(a: dict, key: str) -> Optional[str]:
    v = a.get(key)
    if v in (None, ""):
        return None
    v = str(v)
    if not REF_RE.match(v) or v.startswith("-"):
        raise GhError(f"{key} has invalid characters")
    return v


def _txt(v: Any) -> str:
    s = str(v)
    if "\x00" in s:
        raise GhError("text must not contain NUL bytes")
    return s


def _flag(argv: list, a: dict, key: str, flag: str) -> None:
    """Free text is glued to its flag (--flag=value), so it can never be read as another flag."""
    if a.get(key) not in (None, ""):
        argv.append(f"{flag}={_txt(a[key])}")


def _multi(argv: list, a: dict, key: str, flag: str) -> None:
    for v in a.get(key) or []:
        argv.append(f"{flag}={_txt(v)}")


def _int(v: Any, key: str, lo: int, hi: int, default: Optional[int] = None) -> int:
    if v is None or v == "":
        if default is None:
            raise GhError(f"{key} is required")
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise GhError(f"{key} must be a whole number between {lo} and {hi}, e.g. {key}=20") from None
    if not lo <= n <= hi:
        raise GhError(f"{key} must be between {lo} and {hi}")
    return n


def _limit(argv: list, a: dict) -> None:
    if a.get("limit") is not None:
        n = _int(a["limit"], "limit", 1, 1000)
        if not 1 <= n <= 1000:
            raise GhError("limit must be between 1 and 1000")
        argv.append(f"--limit={n}")


def _json(argv: list, a: dict) -> None:
    if a.get("json"):
        if not JSON_FIELDS_RE.match(str(a["json"])):
            raise GhError("json must be comma-separated field names, e.g. number,title,state")
        argv.append(f"--json={a['json']}")


def _bool(argv: list, a: dict, key: str, flag: str) -> None:
    if a.get(key) is True:
        argv.append(flag)


def _owner_ok(owner: str, allowed: Optional[set]) -> None:
    if allowed is not None and owner.lower() not in allowed:
        raise GhError(f"This connector only reaches GitHub owners: {', '.join(sorted(allowed))}. "
                      f"'{owner}' is outside that list (setting github.allowed_owners on the Son's connector).")


def _api_owner(path: str) -> Optional[str]:
    """Owner a /repos/<o>/... or /orgs/<o>/... API path targets; None for anything else."""
    m = re.match(r"^/?(?:repos|orgs)/([^/?#]+)", path)
    return m.group(1) if m else None


def build_argv(name: str, a: dict, allowed_owners: Optional[set] = None) -> tuple[list[str], Optional[bytes]]:
    argv, stdin = _build(name, a)
    if allowed_owners is not None:
        if name == "github_repo_list":
            if not a.get("owner"):
                raise GhError("Pass owner: this connector only reaches " + ", ".join(sorted(allowed_owners)) + ".")
            _owner_ok(str(a["owner"]), allowed_owners)
        elif name == "github_search":
            argv[2:2] = [f"--owner={o}" for o in sorted(allowed_owners)]
        elif name == "github_api":
            path = argv[-1]
            owner = _api_owner(path)
            if owner is None and not (path.rstrip("/") in ("/user", "/rate_limit") and argv[1] == "--method=GET"):
                raise GhError("This connector only reaches GitHub owners: " + ", ".join(sorted(allowed_owners)) +
                              ". github_api takes /repos/<owner>/... or /orgs/<owner>/... paths for them "
                              "(plus GET /user and /rate_limit).")
            if owner is not None:
                _owner_ok(owner, allowed_owners)
        elif "repo" in a:
            _owner_ok(str(a["repo"]).split("/")[0], allowed_owners)
    return argv, stdin


def _build(name: str, a: dict) -> tuple[list[str], Optional[bytes]]:
    """Tool call -> (gh argv without the binary, stdin bytes). Pure; unit-tested for every tool."""
    if name not in SPECS:
        raise GhError(f"unknown GitHub tool {name}")
    a = dict(a or {})
    allowed = set(SPECS[name][5])
    extra = set(a) - allowed
    if extra:
        raise GhError(f"unknown argument(s) for {name}: {', '.join(sorted(extra))}")
    stdin: Optional[bytes] = None
    if name == "github_auth_status":
        return ["auth", "status"], None
    if name == "github_repo_list":
        argv = ["repo", "list"]
        if a.get("owner"):
            if not OWNER_RE.match(str(a["owner"])):
                raise GhError("owner must be a GitHub user or org login")
            argv.append(str(a["owner"]))
        _limit(argv, a), _json(argv, a)
        return argv, None
    if name == "github_search":
        kind = a.get("kind")
        if kind not in ("issues", "prs", "repos", "code", "commits"):
            raise GhError("kind must be issues, prs, repos, code or commits")
        argv = ["search", kind]
        _limit(argv, a), _json(argv, a)
        return argv + ["--", _txt(a.get("query") or "")], None
    if name == "github_api":
        method = str(a.get("method") or "GET").upper()
        if method not in API_METHODS:
            raise GhError(f"method must be one of {', '.join(API_METHODS)}")
        path = str(a.get("path") or "")
        if not API_PATH_RE.match(path) or ".." in path or path.startswith("-"):
            raise GhError("path must start with / (e.g. /user) or be relative to repos/ (e.g. epic0x/aiteam/pulls)")
        if not path.startswith("/"):
            path = "repos/" + path
        argv = ["api", f"--method={method}"]
        for k, v in (a.get("fields") or {}).items():
            if not FIELD_KEY_RE.match(str(k)):
                raise GhError(f"field name {k!r} has invalid characters")
            argv.append(f"--raw-field={k}={_txt(v if isinstance(v, str) else json.dumps(v))}")
        if a.get("body") is not None:
            if not isinstance(a["body"], dict):
                raise GhError("body must be a JSON object")
            argv.append("--input=-")
            stdin = json.dumps(a["body"]).encode()
        _bool(argv, a, "paginate", "--paginate")
        _flag(argv, a, "jq", "--jq")
        return argv + ["--", path], stdin

    repo = _repo(a)
    R = [f"--repo={repo}"]
    if name == "github_repo_view":
        argv = ["repo", "view", repo]
        _json(argv, a)
        return argv, None
    if name == "github_issue_list":
        argv = ["issue", "list", *R]
        _flag(argv, a, "state", "--state"), _limit(argv, a), _multi(argv, a, "label", "--label")
        _flag(argv, a, "assignee", "--assignee"), _flag(argv, a, "search", "--search"), _json(argv, a)
        return argv, None
    if name == "github_pr_list":
        argv = ["pr", "list", *R]
        _flag(argv, a, "state", "--state"), _limit(argv, a)
        for k in ("base", "head"):
            if _ref(a, k):
                argv.append(f"--{k}={a[k]}")
        _flag(argv, a, "author", "--author"), _flag(argv, a, "search", "--search"), _json(argv, a)
        return argv, None
    if name == "github_run_list":
        argv = ["run", "list", *R]
        _limit(argv, a)
        for k in ("branch", "workflow"):
            if _ref(a, k):
                argv.append(f"--{k}={a[k]}")
        _json(argv, a)
        return argv, None
    if name == "github_workflow_list":
        argv = ["workflow", "list", *R]
        _json(argv, a)
        return argv, None
    if name == "github_run_view" or name == "github_run_rerun":
        rid = _num(a, "run_id")
        if name == "github_run_view":
            argv = ["run", "view", rid, *R]
            _bool(argv, a, "log_failed", "--log-failed"), _json(argv, a)
        else:
            argv = ["run", "rerun", rid, *R]
            _bool(argv, a, "failed", "--failed")
        return argv, None
    if name == "github_workflow_run":
        wf = _ref(a, "workflow")
        if not wf:
            raise GhError("workflow is required")
        argv = ["workflow", "run", *R]
        if _ref(a, "ref"):
            argv.append(f"--ref={a['ref']}")
        for k, v in (a.get("inputs") or {}).items():
            if not FIELD_KEY_RE.match(str(k)):
                raise GhError(f"input name {k!r} has invalid characters")
            argv.append(f"--raw-field={k}={_txt(v)}")
        return argv + ["--", wf], None
    if name == "github_issue_create":
        argv = ["issue", "create", *R, f"--title={_txt(a.get('title') or '')}", f"--body={_txt(a.get('body') or '')}"]
        _multi(argv, a, "label", "--label"), _multi(argv, a, "assignee", "--assignee")
        return argv, None
    if name == "github_pr_create":
        head = str(a.get("head") or "")
        if not HEAD_RE.match(head):
            raise GhError("head must be the pushed branch name, or owner:branch for a fork")
        argv = ["pr", "create", *R, f"--head={head}", f"--title={_txt(a.get('title') or '')}",
                f"--body={_txt(a.get('body') or '')}"]
        if _ref(a, "base"):
            argv.append(f"--base={a['base']}")
        _bool(argv, a, "draft", "--draft")
        return argv, None

    n = _num(a)
    kind = "issue" if name.startswith("github_issue_") else "pr"
    if name in ("github_issue_view", "github_pr_view"):
        argv = [kind, "view", n, *R]
        _bool(argv, a, "comments", "--comments"), _json(argv, a)
        return argv, None
    if name == "github_pr_diff":
        argv = ["pr", "diff", n, *R]
        _bool(argv, a, "name_only", "--name-only")
        return argv, None
    if name == "github_pr_checks":
        argv = ["pr", "checks", n, *R]
        _json(argv, a)
        return argv, None
    if name in ("github_issue_comment", "github_pr_comment"):
        if not str(a.get("body") or "").strip():
            raise GhError("body is required")
        return [kind, "comment", n, *R, f"--body={_txt(a['body'])}"], None
    if name in ("github_issue_edit", "github_pr_edit"):
        argv = [kind, "edit", n, *R]
        _flag(argv, a, "title", "--title"), _flag(argv, a, "body", "--body")
        if name == "github_pr_edit" and _ref(a, "base"):
            argv.append(f"--base={a['base']}")
        _multi(argv, a, "add_label", "--add-label"), _multi(argv, a, "remove_label", "--remove-label")
        _multi(argv, a, "add_assignee", "--add-assignee"), _multi(argv, a, "remove_assignee", "--remove-assignee")
        if len(argv) == 4:
            raise GhError("nothing to change: pass title, body, base or labels")
        return argv, None
    if name in ("github_issue_close", "github_pr_close"):
        argv = [kind, "close", n, *R]
        _flag(argv, a, "comment", "--comment")
        if name == "github_issue_close" and a.get("reason"):
            if a["reason"] not in ("completed", "not planned"):
                raise GhError("reason must be completed or not planned")
            argv.append(f"--reason={a['reason']}")
        _bool(argv, a, "delete_branch", "--delete-branch")
        return argv, None
    if name == "github_issue_reopen":
        return ["issue", "reopen", n, *R], None
    if name == "github_pr_review":
        ev = a.get("event")
        if ev not in ("approve", "comment", "request-changes"):
            raise GhError("event must be approve, comment or request-changes")
        argv = ["pr", "review", n, *R, f"--{ev}"]
        _flag(argv, a, "body", "--body")
        return argv, None
    if name == "github_pr_merge":
        method = a.get("method") or "merge"
        if method not in ("merge", "squash", "rebase"):
            raise GhError("method must be merge, squash or rebase")
        argv = ["pr", "merge", n, *R, f"--{method}"]
        if a.get("match_head_commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", str(a["match_head_commit"])):
                raise GhError("match_head_commit must be a full 40-char SHA")
            argv.append(f"--match-head-commit={a['match_head_commit']}")
        _bool(argv, a, "delete_branch", "--delete-branch"), _bool(argv, a, "auto", "--auto")
        return argv, None
    raise GhError(f"unknown GitHub tool {name}")  # pragma: no cover


# --------------------------------------------------------------------------------------------- runner
def _no_login(stderr: str, code: int) -> bool:
    s = stderr.lower()
    return code == 4 or "gh auth login" in s or "not logged into" in s or "authentication required" in s


def _hint(stderr: str, code: int) -> str:
    s = stderr.lower()
    if "http 404" in s or "could not resolve to a" in s or "not found" in s:
        return "Check the repo / number, and that this Son's GitHub account can see it (`github_auth_status`)."
    if "http 403" in s or "must have admin" in s or "permission" in s or "resource not accessible" in s:
        return "The Son's GitHub account lacks permission for this. Ask the repo owner for access, or run `gh auth refresh -s <scope>` on the Son."
    if "rate limit" in s:
        return "GitHub rate limit hit. Wait a few minutes and retry."
    if "http 422" in s or "validation failed" in s:
        return "GitHub rejected the input (validation). Check the fields named in the message above."
    if "not mergeable" in s or "merge conflict" in s:
        return "The PR can't be merged as is. Check github_pr_checks / conflicts, or pass auto=true."
    return "Fix the input named in gh's message above and retry. For anything without a typed tool, use github_api."


class GhRunner:
    def __init__(self, cfg: Optional[dict] = None, env: Optional[dict] = None):
        cfg = cfg if isinstance(cfg, dict) else {}
        env = dict(os.environ if env is None else env)
        home = env.get("HOME", "")
        extra = os.pathsep.join(p for p in (os.path.join(home, ".local/bin") if home else "", "/usr/local/bin", "/usr/bin") if p)
        self.gh = cfg.get("gh_bin") or shutil.which("gh", path=env.get("PATH", "") + os.pathsep + extra)
        self.timeout = float(cfg.get("timeout_s") or 30)
        self.max_chars = int(cfg.get("max_output_chars") or 60000)
        self.connect_url = str(cfg.get("connect_url") or DEFAULT_CONNECT_URL)
        ao = cfg.get("allowed_owners")
        self.allowed_owners = {str(o).lower() for o in ao} if ao else None
        env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_PAGER": "cat", "PAGER": "cat",
                    "NO_COLOR": "1", "CLICOLOR": "0", "GH_SPINNER_DISABLED": "1"})
        env.pop("GH_REPO", None)
        self.env = env

    async def run(self, argv: list[str], stdin: Optional[bytes] = None) -> tuple[int, str, str]:
        if not self.gh:
            raise GhError(NOT_LOGGED_IN.format(url=self.connect_url) + "\n\n(gh, the GitHub CLI, is not installed on this Son.)")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.gh, *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env, cwd="/")
        except (FileNotFoundError, PermissionError):
            raise GhError(NOT_LOGGED_IN.format(url=self.connect_url) +
                          "\n\n(gh, the GitHub CLI, is not installed on this Son.)") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GhError(f"gh took longer than {int(self.timeout)} s and was stopped. Narrow the request "
                          "(smaller --limit, a specific number, or a narrower API path) and retry.") from None
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _cap(self, s: str, what: str) -> str:
        if len(s) <= self.max_chars:
            return s
        return (s[:self.max_chars] + f"\n\n[cut: {what} was {len(s)} chars, showing the first {self.max_chars}. "
                "Ask for less: a smaller limit, json with fewer fields, a specific number, or a narrower API path / jq filter.]")

    async def call(self, name: str, args: dict) -> tuple[bool, str]:
        """Returns (ok, text). ok=True -> text is gh's stdout, unchanged (unless cut, which says so)."""
        if name in LEGACY_TOOLS:
            name, args = legacy_to_gh(name, args)
        argv, stdin = build_argv(name, args, self.allowed_owners)
        code, out, err = await self.run(argv, stdin)
        if code == 0:
            # `gh auth status` and a few others print to stderr on success; show it so the answer is never empty.
            return True, self._cap(out if out else err, "the output")
        msg = (err.strip() or out.strip() or f"gh exited with code {code}")
        if _no_login(err + out, code):
            return False, self._cap(f"{NOT_LOGGED_IN.format(url=self.connect_url)}\n\ngh said (exit {code}):\n{msg}", "the error")
        return False, self._cap(f"gh {' '.join(argv[:2])} failed (exit {code}):\n{msg}\n\nNext step: {_hint(err + out, code)}",
                                "the error")
