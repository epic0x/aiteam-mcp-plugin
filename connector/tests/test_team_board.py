"""Team board + team map tools: env-gated, fixed transport, compare-and-set conflicts, no token in any output."""
import json
import logging

import httpx
import pytest

from aiteam_connector.entitlements import Entitlements, required_addons
from aiteam_connector.server import Connector, SonLink
from aiteam_connector.team_board import CONFLICT, NOT_IN_TEAM, TEAM_BOARD_TOOLS, TeamBoard, build_body

URL = "https://board.example.test/v1/team-board"
TOKEN = "tb_" + "s3cret" * 6
TEAM = "team_alpha"
TASK = "tt_" + "a1" * 12
ROW = {"id": TASK, "title": "Ship the pricing page", "status": "doing", "assignee": {"kind": "son", "id": "son-1"},
       "link": None, "version": 3, "createdAt": "2026-11-01T00:00:00.000Z", "updatedAt": "2026-11-02T00:00:00.000Z"}


class FakeSite:
    """Records each request; answers with a canned (status, body) per op, or the default."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def h(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        self.calls.append({"method": req.method, "url": str(req.url), "auth": req.headers.get("authorization"),
                           "body": body})
        if body["op"] in self.answers:
            status, out = self.answers[body["op"]]
            return httpx.Response(status, json=out)
        if body["op"] == "map":
            return httpx.Response(200, json={"ok": True, "map": {"teams": [{"id": TEAM, "name": "Alpha"}],
                                                                  "nodes": [], "edges": []}})
        if body["op"] == "list":
            return httpx.Response(200, json={"ready": True, "tasks": [ROW], "open": 1})
        if body["op"] == "create":
            return httpx.Response(201, json={"ok": True, "task": {**ROW, "version": 1, **body["task"]}})
        return httpx.Response(200, json={"ok": True, "task": {**ROW, "version": ROW["version"] + 1}})


def conn(site=None, monkeypatch=None, **kw):
    site = site or FakeSite()
    board = TeamBoard(URL, TOKEN, transport=httpx.MockTransport(site.h))
    return Connector([SonLink("my-son", "http://box.invalid", "t")], passthrough="none", github=False,
                     team_board=board, **kw), site


def all_text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content) + json.dumps(res.structuredContent or {})


async def test_not_listed_without_env(monkeypatch):
    for env in ({}, {"TEAM_BOARD_URL": URL}, {"TEAM_BOARD_TOKEN": TOKEN}):
        monkeypatch.delenv("TEAM_BOARD_URL", raising=False)
        monkeypatch.delenv("TEAM_BOARD_TOKEN", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        c = Connector([SonLink("my-son", "http://box.invalid", "t")], passthrough="none", github=False)
        assert not set(TEAM_BOARD_TOOLS) & {t.name for t in await c.list_tools()}
        res = await c.call("board_list", {"team_id": TEAM})
        assert "not enabled" in res.content[0].text


async def test_listed_with_env_and_annotated(monkeypatch):
    monkeypatch.setenv("TEAM_BOARD_URL", URL)
    monkeypatch.setenv("TEAM_BOARD_TOKEN", TOKEN)
    c = Connector([SonLink("my-son", "http://box.invalid", "t")], passthrough="none", github=False)
    tools = {t.name: t for t in await c.list_tools()}
    assert set(TEAM_BOARD_TOOLS) <= set(tools)
    for n in ("team_map", "board_list"):
        assert tools[n].annotations.readOnlyHint is True
    for n in ("board_create", "board_update"):
        assert tools[n].annotations.readOnlyHint is False and tools[n].annotations.destructiveHint is False
    for n in TEAM_BOARD_TOOLS:
        assert tools[n].title and tools[n].inputSchema["additionalProperties"] is False
    assert "no messages, memories or task contents" in tools["team_map"].description


async def test_each_op_sends_the_right_body_and_bearer():
    c, site = conn()
    await c.call("team_map", {})
    await c.call("board_list", {"team_id": TEAM, "son": "my-son"})
    await c.call("board_create", {"team_id": TEAM, "title": "Write the FAQ", "status": "todo",
                                  "assignee": {"kind": "box", "id": "box-1"}, "link": "https://example.com/pr/1"})
    await c.call("board_update", {"team_id": TEAM, "task_id": TASK, "version": 3, "status": "done",
                                  "assignee": None, "link": None})
    assert [x["body"] for x in site.calls] == [
        {"op": "map"},
        {"op": "list", "team_id": TEAM},
        {"op": "create", "team_id": TEAM, "task": {"title": "Write the FAQ", "status": "todo",
                                                   "assignee": {"kind": "box", "id": "box-1"},
                                                   "link": "https://example.com/pr/1"}},
        {"op": "update", "team_id": TEAM, "task": {"id": TASK, "version": 3, "status": "done",
                                                   "assignee": None, "link": None}},
    ]
    for x in site.calls:
        assert x["method"] == "POST" and x["url"] == URL and x["auth"] == f"Bearer {TOKEN}"


async def test_son_id_is_never_sent():
    c, site = conn()
    res = await c.call("board_list", {"team_id": TEAM, "son_id": "11111111-1111-1111-1111-111111111111"})
    assert res.isError and not site.calls          # unknown field refused by the strict schema
    await c.call("team_map", {})
    await c.call("board_create", {"team_id": TEAM, "title": "x"})
    for x in site.calls:
        assert "son_id" not in json.dumps(x["body"])


async def test_results_pass_site_json_through():
    c, _ = conn()
    res = await c.call("board_list", {"team_id": TEAM})
    assert not res.isError and res.structuredContent["tasks"][0]["id"] == TASK
    res = await c.call("team_map", {})
    assert res.structuredContent["map"]["teams"][0]["id"] == TEAM
    res = await c.call("board_create", {"team_id": TEAM, "title": "Write the FAQ"})
    assert not res.isError and res.content[0].text.startswith("Task added.")


async def test_conflict_is_a_normal_result_with_the_current_row():
    latest = {**ROW, "version": 5, "status": "blocked"}
    c, _ = conn(FakeSite({"update": (409, {"ok": False, "error": "version_conflict",
                                            "message": "Someone changed this task.", "task": latest})}))
    res = await c.call("board_update", {"team_id": TEAM, "task_id": TASK, "version": 3, "status": "done"})
    assert res.isError is False
    assert res.content[0].text.startswith(CONFLICT)
    assert res.structuredContent == {"conflict": True, "task": latest}


@pytest.mark.parametrize("status,body,expect", [
    (401, {"ok": False, "error": "unauthorized"}, NOT_IN_TEAM),
    (403, {"ok": False, "error": "forbidden"}, NOT_IN_TEAM),
    (404, {"ok": False, "error": "not_found"}, "Unknown team."),
    (422, {"ok": False, "error": "assignee_not_in_team", "message": "That Son or box is not in this team."},
     "assignee_not_in_team"),
    (428, {"ok": False, "error": "if_match_required"}, "if_match_required"),
])
async def test_error_mapping(status, body, expect):
    c, _ = conn(FakeSite({"create": (status, body)}))
    res = await c.call("board_create", {"team_id": TEAM, "title": "Write the FAQ",
                                        "assignee": {"kind": "son", "id": "someone-else"}})
    assert res.isError is True and expect in res.content[0].text


async def test_network_and_5xx_say_retry():
    c, _ = conn(FakeSite({"list": (502, {"error": "bad_gateway"})}))
    res = await c.call("board_list", {"team_id": TEAM})
    assert res.isError and "try again" in res.content[0].text

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {TOKEN}")
    b = TeamBoard(URL, TOKEN, transport=httpx.MockTransport(boom))
    res = await b.call("team_map", {})
    assert res.isError and "try again" in res.content[0].text and TOKEN not in all_text(res)


@pytest.mark.parametrize("args", [
    {"team_id": TEAM, "title": "x" * 141},
    {"team_id": TEAM, "title": ""},
    {"team_id": TEAM, "title": "two\nlines"},
    {"team_id": TEAM, "title": "ok", "link": "http://example.com/pr/1"},
    {"team_id": TEAM, "title": "ok", "link": "javascript:alert(1)"},
    {"team_id": TEAM, "title": "ok", "status": "later"},
    {"team_id": TEAM, "title": "ok", "assignee": {"kind": "human", "id": "x"}},
    {"team_id": "a", "title": "ok"},
    {"team_id": "../etc", "title": "ok"},
    {"title": "ok"},
])
async def test_schema_rejects_bad_create(args):
    c, site = conn()
    res = await c.call("board_create", args)
    assert res.isError and not site.calls


@pytest.mark.parametrize("args", [
    {"team_id": TEAM, "task_id": TASK, "version": 3},                      # nothing to change
    {"team_id": TEAM, "task_id": TASK, "version": 0, "status": "done"},
    {"team_id": TEAM, "task_id": "42", "version": 1, "status": "done"},
    {"team_id": TEAM, "task_id": TASK, "status": "done"},
    {"team_id": TEAM, "task_id": TASK, "version": 1, "link": "http://x.test"},
])
async def test_schema_rejects_bad_update(args):
    c, site = conn()
    res = await c.call("board_update", args)
    assert res.isError and not site.calls


def test_140_char_title_is_fine():
    assert build_body("board_create", {"team_id": TEAM, "title": "x" * 140})["task"]["title"] == "x" * 140


async def test_token_never_in_results_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    answers = {"map": (200, {"ok": True, "map": {}}), "list": (403, {"ok": False, "error": "forbidden"}),
               "create": (500, {"error": "boom"}), "update": (409, {"ok": False, "task": ROW})}
    c, _ = conn(FakeSite(answers))
    outs = [await c.call("team_map", {}), await c.call("board_list", {"team_id": TEAM}),
            await c.call("board_create", {"team_id": TEAM, "title": "x"}),
            await c.call("board_update", {"team_id": TEAM, "task_id": TASK, "version": 1, "title": "y"}),
            await c.call("board_create", {"team_id": TEAM, "title": "x" * 200})]
    for r in outs:
        assert TOKEN not in all_text(r)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(c.team_board)


async def test_board_is_free_with_enforcement_on(tmp_path):
    plugs = tmp_path / "plugs.json"
    plugs.write_text(json.dumps({"version": 1, "enforce": True, "comp": False, "addons": []}))
    c, site = conn(entitlements=Entitlements(plugs, son_id="s"))
    for n in TEAM_BOARD_TOOLS:
        assert required_addons(n, {}) == ()
    res = await c.call("board_list", {"team_id": TEAM})
    assert not res.isError and site.calls
