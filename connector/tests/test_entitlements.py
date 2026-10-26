"""Add-on entitlement gate (owner 2026-10-14): every tool stays listed; a paid tool the Son does not own answers with
the upsell as a NORMAL result (isError false), with add-on name, price and dashboard link. Owner never gated."""
import json

import mcp.types as types
import pytest

from aiteam_connector import entitlements as E
from aiteam_connector.server import Connector, SonLink

SON_ID = "son-123"
TG = "telegram:-100" + "1" * 10   # fake group, built at runtime (id scan)


def plugs(tmp_path, addons, enforce=True, comp=False, raw=None):
    p = tmp_path / "plugs.json"
    body = raw if raw is not None else {"version": 1, "enforce": enforce, "comp": comp, "addons": addons}
    p.write_text(json.dumps(body))
    return p


class FakeUp:
    def __init__(self):
        self.calls = []

    async def tools(self):
        return [types.Tool(name=n, inputSchema={"type": "object"}) for n in
                ("voice_meet_join", "meet_join", "skill_view", "messages_send", "discord", "terminal",
                 "mnemosyne_shared_recall")]

    async def call(self, name, args):
        self.calls.append((name, args))
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"ran {name}")], isError=False)


def conn(tmp_path, addons=None, exempt=None, **kw):
    path = plugs(tmp_path, addons, **kw) if addons is not None or kw.get("raw") is not None else tmp_path / "none.json"
    c = Connector([SonLink("my-son", "http://box", "t", "http://up/mcp")], github=False,
                  entitlements=E.Entitlements(path, son_id=SON_ID, exempt=set(exempt or ())))
    up = FakeUp()
    c.upstreams["my-son"] = up
    return c, up


def body(res):
    return res.content[0].text


async def test_owned_addon_runs(tmp_path):
    c, up = conn(tmp_path, ["calls"])
    res = await c.call("voice_meet_join", {"url": "https://meet.google.com/abc-defg-hij"})
    assert body(res) == "ran voice_meet_join" and up.calls


async def test_not_owned_returns_paywall_not_error(tmp_path):
    c, up = conn(tmp_path, ["seo-growth"])
    res = await c.call("voice_meet_join", {"url": "https://meet.google.com/abc-defg-hij"})
    assert res.isError is False
    assert up.calls == [], "the paid tool must not run"
    msg = body(res)
    assert msg == ("This needs the Calls (Meet, Zoom and Discord voice) add-on. Add it for $20/mo from your "
                   f"aiteam.space dashboard: https://aiteam.space/my-sons/{SON_ID}?add=calls")
    sc = res.structuredContent
    assert sc["paywall"] is True and sc["tool"] == "voice_meet_join"
    assert sc["addon"] == {"slug": "calls", "name": "Calls (Meet, Zoom and Discord voice)", "price_usd": 20,
                           "period": "month"}
    assert sc["url"].endswith("?add=calls") and sc["message"] == msg


async def test_owner_always_runs(tmp_path):
    c, up = conn(tmp_path, [], comp=True)                       # complimentary Son
    assert body(await c.call("meet_join", {})) == "ran meet_join"
    c, up = conn(tmp_path, [], exempt={"owner@example.com"})    # verified caller in AITEAM_GATE_EXEMPT
    assert body(await c.call("meet_join", {}, caller="Owner@Example.com")) == "ran meet_join"
    assert (await c.call("meet_join", {}, caller="someone@example.com")).structuredContent["paywall"]


async def test_switch_off_or_no_state_never_gates(tmp_path):
    for kw in ({"enforce": False}, {"raw": {"version": 1, "enforce": True}}):  # FLEET_ENFORCE_PLUGS off / old fleet
        c, up = conn(tmp_path, [], **kw) if "raw" not in kw else conn(tmp_path, None, **kw)
        assert body(await c.call("voice_meet_join", {})) == "ran voice_meet_join", kw
    c, up = conn(tmp_path, None)                                # no plugs.json at all
    assert body(await c.call("voice_meet_join", {})) == "ran voice_meet_join"


async def test_discord_gated_under_discord_addon_telegram_free(tmp_path):
    c, up = conn(tmp_path, ["calls"])
    # plain chat into a Discord channel or thread is free (owner split 2026-10-16); only thread control is paid
    assert body(await c.call("messages_send", {"target": "discord:111111111111111111", "message": "hi"})) == \
        "ran messages_send"
    res = await c.call("discord", {"action": "send"})
    assert res.structuredContent["addon"]["slug"] == "discord" and "$5/mo" in body(res)
    assert body(await c.call("messages_send", {"target": TG + ":5", "message": "hi"})) == \
        "ran messages_send"
    assert (await c.call("discord", {"action": "read"})).structuredContent["addon"]["slug"] == "discord"
    c, up = conn(tmp_path, ["discord"])
    assert body(await c.call("discord", {"action": "read"})) == "ran discord"


async def test_free_base_never_gated(tmp_path):
    c, up = conn(tmp_path, [])
    for name, args in (("terminal", {"command": "ls"}), ("mnemosyne_shared_recall", {"query": "x"}),
                       ("skill_view", {"name": "claims-check"}), ("skill_view", {"name": "meeting-notes"}),
                       ("skill_view", {"name": "humanizer"}),
                       ("messages_send", {"target": TG, "message": "x"})):
        assert body(await c.call(name, args)) == f"ran {name}", (name, args)
    assert body(await c.call("usage_guide", {})).startswith("#") or "Son" in body(await c.call("usage_guide", {}))


async def test_skill_only_addons(tmp_path):
    c, up = conn(tmp_path, ["social-content"])
    res = await c.call("skill_view", {"name": "seo-geo"})
    assert res.structuredContent["addon"]["slug"] == "seo-growth" and not up.calls
    assert body(await c.call("skill_view", {"name": "social-creative"})) == "ran skill_view"
    res = await c.call("skill_view", {"name": "video-reels"})            # two paid add-ons ship it: cheapest named
    assert res.structuredContent["addon"]["slug"] == "explainer-videos" and "$10/mo" in body(res)
    c, up = conn(tmp_path, ["cinematic-ads"])
    assert body(await c.call("skill_view", {"name": "video-reels"})) == "ran skill_view"
    res = await c.call("skill_view", {"name": "zoom-calls"})
    assert res.structuredContent["addon"]["slug"] == "calls"


async def test_purchase_picked_up_next_call(tmp_path):
    c, up = conn(tmp_path, [])
    assert (await c.call("meet_join", {})).structuredContent["paywall"]
    p = tmp_path / "plugs.json"
    p.write_text(json.dumps({"version": 1, "enforce": True, "comp": False, "addons": ["calls", "telegram"]}))
    assert body(await c.call("meet_join", {})) == "ran meet_join"


async def test_every_tool_still_listed(tmp_path):
    c, up = conn(tmp_path, [])
    names = {t.name for t in await c.list_tools()}
    assert {"voice_meet_join", "meet_join", "discord", "skill_view"} <= names


def test_map_is_consistent():
    paid = set(E.ADDONS)
    assert set(E.TOOL_ADDON.values()) <= paid
    assert {s for v in E.SKILL_ADDONS.values() for s in v} <= paid
    assert all(a.price_usd > 0 for a in E.ADDONS.values()), "a free add-on never gates anything"
    for free in E.FREE_ELSEWHERE:
        assert free not in E.SKILL_ADDONS, free
    assert not any(t.startswith(("mnemosyne", "telegram_", "github_", "son_", "session")) for t in E.TOOL_ADDON)


def test_documented_tools_match_docs():
    import pathlib
    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "ENTITLEMENTS.md").read_text()
    for t in E.TOOL_ADDON:
        assert f"`{t}`" in doc, t
    for slug, a in E.ADDONS.items():
        assert f"`{slug}`" in doc and f"${a.price_usd}" in doc, slug


def test_catalogue_drift_every_paid_only_skill_is_gated():
    """Fixture = aiteam.space catalogue (app/lib/addons.ts). Every skill listed ONLY by paid add-ons must be gated
    by exactly those add-ons, unless FREE_ELSEWHERE says why it stays open. Fails when the two drift apart."""
    import pathlib
    cat = json.loads((pathlib.Path(__file__).parent / "fixtures" / "catalogue.json").read_text())["addons"]
    free = {k for a in cat if a["price"] == 0 for k in a["skills"]}
    paid: dict[str, set] = {}
    for a in cat:
        if a["price"] > 0:
            for k in a["skills"]:
                paid.setdefault(k, set()).add(a["slug"])
    missing, wrong = [], []
    for skill, slugs in sorted(paid.items()):
        if skill in free or skill in E.FREE_ELSEWHERE:
            assert skill not in E.SKILL_ADDONS, f"{skill} is free elsewhere but gated"
            continue
        if skill not in E.SKILL_ADDONS:
            missing.append(skill)
        elif not slugs <= set(E.SKILL_ADDONS[skill]):
            wrong.append((skill, sorted(slugs), E.SKILL_ADDONS[skill]))
    assert not missing, f"paid-only catalogue skills not gated: {missing}"
    assert not wrong, f"gated under the wrong add-ons: {wrong}"
    prices = {a["slug"]: a["price"] for a in cat}
    for slug, a in E.ADDONS.items():
        if slug in prices:                      # catalogue add-ons (discord is a plug, priced in plug-rules.ts)
            assert prices[slug] == a.price_usd, (slug, prices[slug], a.price_usd)


async def test_review_skills_now_gated(tmp_path):
    c, up = conn(tmp_path, [])
    for skill, slug in (("aiteam-google-meet", "calls"), ("zoom-voice", "calls"), ("content-creator", "seo-growth"),
                        ("social-media-manager", "social-content"), ("sales-agent", "cold-email"),
                        ("infographic", "explainer-videos")):
        res = await c.call("skill_view", {"name": skill})
        assert res.isError is False and res.structuredContent["addon"]["slug"] == slug, skill
    assert not up.calls
    res = await c.call("skill_view", {"name": "x-posting"})          # paid-only shared skill: cheapest pack named
    assert res.structuredContent["addon"]["price_usd"] == 5


def test_discord_thread_reads_free_control_paid():
    # Rule 0 status sweeps read threads: reading is free. Create/edit/archive/members add-remove need the add-on.
    for t in ("discord_threads_list", "discord_thread_get", "discord_thread_members"):
        assert E.required_addons(t, {}) == (), t
    for t in ("discord_thread_create", "discord_thread_edit", "discord_thread_member_add",
              "discord_thread_member_remove", "discord", "discord_admin"):
        assert E.required_addons(t, {}) == ("discord",), t
    assert E.required_addons("messages_send", {"target": "discord:1:2"}) == ()
    assert E.required_addons("messages_send", {"target": "telegram:1"}) == ()
