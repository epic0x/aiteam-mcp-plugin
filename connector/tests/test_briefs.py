"""Coordinator rule + brief templates: labelled four-part briefs, templates per team kept in editable YAML."""
import json
import re

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from aiteam_connector import brief_templates as bt
from aiteam_connector.server import Connector, SonLink, build_server
from aiteam_connector.usage_guide import GUIDE, INSTRUCTIONS

from .test_connector import stack  # noqa: F401  (fixture)

TEAMS = {"engineering", "design", "marketing", "video", "ops", "research", "sales", "general"}
LABELS = ("New feature request", "Improvement", "Bug found")
PARTS = ("Why", "The goal", "What needs to be done", "What I expect back")


def data(r):
    assert r.content and r.content[0].type == "text"
    return json.loads(r.content[0].text)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AITEAM_BRIEFS_DIR", str(tmp_path / "none"))


# ------------------------------------------------------------------------------------------- bundled templates
def test_bundled_templates_all_load_clean():
    t = bt.load()
    assert t["problems"] == [], t["problems"]
    assert set(t["teams"]) == TEAMS
    assert [x["title"] for x in t["core"]["labels"]] == list(LABELS)
    assert [p["heading"] for p in t["core"]["parts"]] == list(PARTS)
    assert t["core"]["experts_decide"] is True


def test_every_team_cites_a_standard_and_covers_bugs():
    for team in bt.load()["teams"].values():
        assert len(team["standards"]) >= 2, team["id"]
        assert all(s["url"].startswith("https://") for s in team["standards"]), team["id"]
        assert team["fields"].get("bug"), f"{team['id']}: a bug needs its own fields"
    assert bt.load()["core"]["ask_expert"]["standards"]


def test_engineering_bug_follows_the_bug_report_standard():
    keys = {f["key"] for f in bt.load()["teams"]["engineering"]["fields"]["bug"]}
    assert {"Steps to reproduce", "Expected", "Actual", "Severity", "How often", "Environment"} <= keys


def test_templates_name_no_people_ids_or_emails():
    for p in bt.BUNDLED.glob("*.yaml"):
        s = p.read_text()
        assert not re.search(r"[0-9]{17,20}|@[a-z0-9-]+\.[a-z]{2,}|<@\d", s), p.name


def test_skeleton_has_label_four_parts_and_team_fields():
    t = bt.load()
    s = bt.skeleton(t["core"], t["teams"]["engineering"], "bug")
    first = s.splitlines()[0]
    assert first.startswith("Bug found: ")
    for h in PARTS:
        assert f"**{h}:**" in s
    assert "Steps to reproduce" in s and "Expected" in s and s.rstrip().endswith("from <app>")
    assert s.index("**Why:**") < s.index("**The goal:**") < s.index("**What needs to be done:**") < s.index("**What I expect back:**")


# ------------------------------------------------------------------------------------------- tool
async def test_tool_is_listed_read_only_and_serves_core_team_and_skeleton(stack):  # noqa: F811
    _, box_url = stack
    conn = Connector([SonLink("my-son", box_url, "alice-token")], passthrough="none")
    async with create_connected_server_and_client_session(build_server(conn)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        t = tools["brief_templates"]
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False and t.title
        assert list(tools)[:2] == ["usage_guide", "brief_templates"]
        core = data(await client.call_tool("brief_templates", {}))
        assert {x["id"] for x in core["teams"]} == TEAMS and core["core"]["labels"][2]["title"] == "Bug found"
        assert "AITEAM_BRIEFS_DIR" in core["edit"]
        vid = data(await client.call_tool("brief_templates", {"team": "video"}))
        assert vid["team"]["id"] == "video" and vid["source"] == "bundled"
        sk = data(await client.call_tool("brief_templates", {"team": "marketing", "label": "new-feature"}))
        assert sk["skeleton"].startswith("New feature request: ") and "Key message" in sk["skeleton"]
        bad = await client.call_tool("brief_templates", {"team": "nope"})
        assert bad.isError and "engineering" in data(bad)["teams"]
        lone = await client.call_tool("brief_templates", {"label": "bug"})
        assert lone.isError


async def test_tool_is_free_and_ignores_son_argument():
    r = bt.call({"son": "my-son"})
    assert not r.isError and data(r)["core"]["version"] == 1


# ------------------------------------------------------------------------------------------- owner overrides
def test_owner_override_replaces_and_adds_teams_without_code(tmp_path, monkeypatch):
    d = tmp_path / "briefs"
    d.mkdir()
    eng = yaml.safe_load((bt.BUNDLED / "engineering.yaml").read_text())
    eng["name"] = "Our dev team"
    (d / "engineering.yaml").write_text(yaml.safe_dump(eng))
    (d / "legal.yaml").write_text(yaml.safe_dump({
        "id": "legal", "name": "Legal", "use_for": ["contract"], "standards": [{"name": "House rules", "url": "https://example.com"}],
        "fields": {"all": [{"key": "Counterparty", "ask": "Who signs."}], "bug": [{"key": "Clause", "ask": "Which clause."}]}}))
    monkeypatch.setenv("AITEAM_BRIEFS_DIR", str(d))
    t = bt.load()
    assert t["problems"] == []
    assert t["teams"]["engineering"]["name"] == "Our dev team" and t["source"]["engineering.yaml"] == "custom"
    assert "legal" in t["teams"] and data(bt.call({"team": "legal"}))["source"] == "custom"


def test_hermes_home_briefs_folder_is_the_default_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AITEAM_BRIEFS_DIR")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "briefs").mkdir()
    core = yaml.safe_load((bt.BUNDLED / "core.yaml").read_text())
    core["signature"] = "— the boss"
    (tmp_path / "briefs" / "core.yaml").write_text(yaml.safe_dump(core))
    assert bt.load()["core"]["signature"] == "— the boss"


def test_bad_custom_file_falls_back_and_is_reported(tmp_path, monkeypatch):
    d = tmp_path / "briefs"
    d.mkdir()
    (d / "core.yaml").write_text("labels: [unclosed\n")
    (d / "video.yaml").write_text(yaml.safe_dump({"id": "video", "name": "x"}))
    (d / "junk.yaml").write_text("- just a list\n")
    monkeypatch.setenv("AITEAM_BRIEFS_DIR", str(d))
    t = bt.load()
    assert t["core"]["labels"][0]["title"] == "New feature request"           # bundled core kept
    assert t["teams"]["video"]["standards"] and t["source"]["video.yaml"] == "bundled"
    assert "junk" not in t["teams"]
    joined = " ".join(t["problems"])
    assert "core.yaml (custom) skipped: YAML error at line" in joined and "video.yaml" in joined and "junk.yaml" in joined
    assert data(bt.call({}))["problems"] == t["problems"]


def test_core_override_cannot_drop_or_rename_the_three_labels(tmp_path, monkeypatch):
    d = tmp_path / "briefs"
    d.mkdir()
    core = yaml.safe_load((bt.BUNDLED / "core.yaml").read_text())
    core["labels"] = core["labels"][:2]
    (d / "core.yaml").write_text(yaml.safe_dump(core))
    monkeypatch.setenv("AITEAM_BRIEFS_DIR", str(d))
    t = bt.load()
    assert len(t["core"]["labels"]) == 3 and any("labels" in p for p in t["problems"])


def test_oversized_custom_file_is_skipped(tmp_path, monkeypatch):
    d = tmp_path / "briefs"
    d.mkdir()
    (d / "ops.yaml").write_text("x: " + "a" * (bt.MAX_FILE + 10))
    monkeypatch.setenv("AITEAM_BRIEFS_DIR", str(d))
    t = bt.load()
    assert t["source"]["ops.yaml"] == "bundled" and any("larger than" in p for p in t["problems"])


# ------------------------------------------------------------------------------------------- the rule in the guide
def test_coordinator_rule_is_in_instructions_and_guide():
    i = INSTRUCTIONS
    assert "COORDINATOR RULE" in i and i.index("STANDING RULE 0") < i.index("COORDINATOR RULE") < i.index("call usage_guide")
    for s in LABELS + ("Why", "The goal", "What needs to be done", "What I expect back", "brief_templates",
                       "never stall", "they decide how"):
        assert s in i, s
    g = GUIDE
    sec = g[g.index("## Coordinator: every request is a brief"):g.index("## 1.")]
    for s in ("Never relay the owner's words as a few bare lines", "`New feature request:`", "`Improvement:`",
              "`Bug found:`", "**Why:**", "**The goal:**", "**What needs to be done:**", "**What I expect back:**",
              "The experts decide the tech", "`brief_templates`", "Ask the right expert, then carry on",
              "Until you answer, I'll assume", "Nothing stays only in the chat", "editable YAML"):
        assert s in sec, s


def test_hand_off_example_is_a_full_labelled_brief():
    ex = GUIDE[GUIDE.index("Example (`messages_send`):"):GUIDE.index("## 2.")]
    assert "message: New feature request: " in ex
    for h in PARTS:
        assert f"**{h}:**" in ex, h
    assert "Task:" not in ex


def test_chatgpt_onboarding_skill_carries_the_rule():
    import pathlib
    s = (pathlib.Path(__file__).resolve().parents[2] / "package" / "skills" / "get-started" / "SKILL.md").read_text()
    for x in ("`New feature request:`", "`Bug found:`", "What I expect back", "`brief_templates`", "coordinator"):
        assert x in s, x
