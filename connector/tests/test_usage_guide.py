"""The usage guide ships with the connector: MCP instructions + a usage_guide tool, no manual skill needed."""
import re

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector.github_gh import GITHUB_TOOLS
from aiteam_connector.server import Connector, SonLink, build_server
from aiteam_connector.usage_guide import GUIDE, GUIDE_TOOLS, INSTRUCTIONS

from .test_connector import stack  # noqa: F401  (fixture)

# Tools the Son's own `hermes mcp serve` adds upstream (standard session-tools set); not listed offline.
UPSTREAM = {"messages_send", "conversations_list", "sessions_list", "sessions_history", "sessions_search",
            "sessions_send", "messages_read"}


async def test_instructions_and_usage_guide_tool_are_served(stack):  # noqa: F811
    _, box_url = stack
    conn = Connector([SonLink("my-son", box_url, "alice-token")], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        init = getattr(client, "_server_instructions", None)
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "usage_guide" in tools and tools["usage_guide"].annotations.readOnlyHint is True
        assert list(tools)[0] == "usage_guide", "listed first so models see it"
        r = await client.call_tool("usage_guide", {})
        assert not r.isError and r.content[0].text == GUIDE
    srv = build_server(conn)
    assert srv.instructions == INSTRUCTIONS and "usage_guide" in INSTRUCTIONS


def test_guide_covers_the_four_rules():
    g = GUIDE.lower()
    assert "messages_send" in GUIDE and "<@user_id>" in GUIDE                     # new task: webhook post + tag
    assert "discord:<channel_id>:<thread_id>" in GUIDE and "never open a second thread" in g   # steer in thread
    assert "ask the user first" in g and "son_send" in GUIDE                      # unrelated task: ask, headless
    assert "last 12 hours" in g and "github_pr_list" in GUIDE                     # updates: 12 h + GitHub
    for s in (INSTRUCTIONS, GUIDE):
        assert "12 hours" in s


def test_guide_documents_webhook_hand_off():
    g = GUIDE.lower()
    assert "webhook" in g and "human user" in g and "several sons" in g        # one post can tag several Sons
    assert "opens a new thread" in g and "continues that thread" in g          # main channel vs thread target
    assert "replaces the older hand-off" in g and "example" in g
    assert "webhook" in INSTRUCTIONS and "human user" in INSTRUCTIONS


def test_guide_only_names_tools_that_exist():
    from aiteam_connector.server import AI_TOOLS, DISCORD_THREAD_TOOLS, HEALTH_TOOLS, TELEGRAM_TOOLS, native_tools
    known = ({t.name for t in native_tools(["my-son"])} | set(GITHUB_TOOLS) | UPSTREAM | {"usage_guide"}
             | set(DISCORD_THREAD_TOOLS) | set(TELEGRAM_TOOLS) | set(HEALTH_TOOLS) | set(AI_TOOLS))
    ARGS = {"setup_aiteam", "topic_id", "chat_id", "reply_to_message_id", "add_user_ids", "member_id", "member_of", "is_member", "include_members"}
    named = (set(re.findall(r"`([a-z]+_[a-z_]+)`", GUIDE)) - ARGS) | set(GUIDE_TOOLS)
    assert named <= known, f"guide names unknown tools: {sorted(named - known)}"


def test_guide_has_no_ids_or_emails():
    # Names of people and orgs are checked repo-wide by scripts/id_scan.py (ID_SCAN_DENY secret) in CI.
    for s in (INSTRUCTIONS, GUIDE):
        assert not re.search(r"[0-9]{17,20}|@[a-z0-9-]+\.[a-z]{2,}", s), s[:80]


def test_rule_zero_updates_status_is_first():
    # Standing rule 0: check live sources first, four-part summary, live beats stale notes. Must load before rule 1.
    assert GUIDE.index("## 0. Standing rule: updates and status") < GUIDE.index("## 1.")
    assert INSTRUCTIONS.index("STANDING RULE 0") < INSTRUCTIONS.index("call usage_guide")
    g = GUIDE.lower()
    for phrase in ("nothing is running", "one sweep", "sessions_list", "sessions_search", "discord_threads_list",
                   "github_pr_list", "github_run_list", "**done**", "**in progress**", "**waiting**", "**next**",
                   "live sources are the truth", "verify before you report"):
        assert phrase in g, phrase
    for part in ("Done", "In progress", "Waiting", "Next", "never override"):
        assert part in INSTRUCTIONS, part


def test_guide_covers_computer_health_tools():
    g = GUIDE.lower()
    assert "## 8. the son's computer" in g
    for name in ("`ping`", "`health`", "`cleanup`"):
        assert name in GUIDE
    assert "read out" in g and "once every 10 minutes" in g and "do not retry" in g
    assert "ping" in INSTRUCTIONS and "health" in INSTRUCTIONS and "cleanup" in INSTRUCTIONS


def test_guide_covers_ai_signin_and_never_asks_for_secrets_in_chat():
    g = GUIDE.lower()
    assert "## 9. ai sign-in and model" in g
    for name in ("`ai_status`", "`ai_signin_start`", "`ai_signin_status`", "`ai_model_set`", "`ai_fallback_set`"):
        assert name in GUIDE
    assert "never ask the user to paste a code" in g and "aiteam.space link" in g
    assert INSTRUCTIONS.index("ai_status") < INSTRUCTIONS.index("ai_signin_start")
    assert "Never ask the user to paste a code or an API key in the chat" in INSTRUCTIONS
    assert "aiteam.space link" in INSTRUCTIONS


def test_guide_keeps_each_project_in_its_own_workflow_and_scope():
    assert GUIDE.index("## Projects: own workflow, own scope") < GUIDE.index("## Always")
    g = GUIDE.lower()
    for phrase in ("the workflow belongs to the project, not to the son", "never carry approvals across projects",
                   "never counts for another project", "never carry context across projects",
                   "allow no merge by a son at all", "ask the user once"):
        assert phrase in g, phrase
    i = INSTRUCTIONS.lower()
    assert "never carry approvals across projects" in i and "own workflow" in i
