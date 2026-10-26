"""Per-add-on entitlement gate: every tool stays listed, a paid tool the Son does not own answers with an upsell.

Rules (owner 2026-10-14, docs/ENTITLEMENTS.md):
  * Nothing is hidden. Claude/ChatGPT can discover and call every tool.
  * A call to a tool that needs a paid add-on the Son does not own returns a NORMAL tool result (isError false) with
    the add-on name, its monthly price and the dashboard link. The tool does not run. Not an error, not a no-op.
  * The free base is the MCP control surface: connecting (Claude or ChatGPT, voice included), reading, coordinating
    the Son, Telegram chat and Telegram topic control, GitHub. None of it is gated.
  * Shared-memory tools are not gated (yet).
  * Owner exemption: a complimentary Son (plugs.json ``comp: true``) or a caller whose verified email is in
    ``AITEAM_GATE_EXEMPT`` is never gated.
  * Live only when the fleet switch is on: plugs.json ``enforce: true`` (FLEET_ENFORCE_PLUGS). No plugs.json, no
    ``addons`` list, or ``enforce`` false = nothing gated, so old Sons and hand-built boxes never break.

Source of truth: Stripe -> aiteam.space ``aiteam.sons.addons`` -> fleet ``PUT /tenants/{id}/addons`` ->
``<HERMES_HOME>/plugs.json`` (``addons``: the Son's owned slugs). Read on every call, re-parsed only when the file's
mtime/size changes, so a purchase lands within seconds of the fleet sync and no network call sits on the hot path.
"""
from __future__ import annotations

import contextvars
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

import mcp.types as types

DASHBOARD = os.environ.get("AITEAM_DASHBOARD_URL", "https://aiteam.space").rstrip("/")


@dataclass(frozen=True)
class Addon:
    slug: str
    name: str        # what the agent says to the user
    price_usd: int   # per month, per Son (aiteam.space catalogue)


# Paid add-ons that can gate something here. Names and prices mirror aiteam.space app/lib/addons.ts + plug-rules.ts
# (the site's check-script compares them, so a price change there fails CI until this map follows).
ADDONS: dict[str, Addon] = {a.slug: a for a in (
    Addon("calls", "Calls (Meet, Zoom and Discord voice)", 20),
    Addon("discord", "Discord thread management — two-way sync", 5),
    Addon("seo-growth", "SEO growth agent", 5),
    Addon("social-content", "Social photo posts and content machine", 5),
    Addon("cold-email", "Cold email", 5),
    Addon("cinematic-ads", "Cinematic video ads and reels", 20),
    Addon("explainer-videos", "Explainer and infographic video ads", 10),
    Addon("trading-signals", "Trading signals: crypto", 15),
    Addon("trading-signals-stocks", "Trading signals: stocks", 15),
    Addon("security-audit-loop", "Security audit loop", 20),
)}

# Tool -> add-on that unlocks it.
CALLS_TOOLS = ("voice_meet_join", "voice_meet_leave", "voice_meet_mode", "voice_meet_status",       # hermes-talk
               "meet_join", "meet_leave", "meet_say", "meet_status", "meet_transcript", "meet_command")  # google_meet
# Paid: CONTROLLING Discord threads (create, edit/rename/archive/lock, add/remove members) and the raw Discord
# tools. Free: reading thread state (rule 0 status sweeps call discord_threads_list) and plain chat via messages_send.
DISCORD_TOOLS = ("discord", "discord_admin", "discord_thread_edit", "discord_thread_create",
                 "discord_thread_member_add", "discord_thread_member_remove", "discord_api")
DISCORD_FREE_READ = ("discord_threads_list", "discord_thread_get", "discord_thread_members",
                     "discord_read", "discord_threads", "discord_channels")
TOOL_ADDON: dict[str, str] = {**{t: "calls" for t in CALLS_TOOLS}, **{t: "discord" for t in DISCORD_TOOLS}}

# Paid add-ons that ship skills only (no tools of their own): opening one of their skills is the gated action.
# Only skills NO free add-on and no base image ships (FREE_ELSEWHERE below stays open). A skill shipped by several
# paid add-ons: owning any of them unlocks it, and the upsell names the cheapest.
SKILL_ADDONS: dict[str, tuple[str, ...]] = {
    "seo-growth": ("seo-growth",), "seo-geo": ("seo-growth",), "blog-playbook": ("seo-growth",),
    "social-content": ("social-content",), "social-creative": ("social-content",),
    "cold-email": ("cold-email",), "lead-research": ("cold-email",),
    "cinematic-ads": ("cinematic-ads",), "creative-direction": ("cinematic-ads",), "logo-ident": ("cinematic-ads",),
    "explainer-videos": ("explainer-videos",),
    "video-reels": ("explainer-videos", "cinematic-ads"),
    "trading-signals": ("trading-signals",), "hyperliquid-trading": ("trading-signals",),
    "trading-signals-stocks": ("trading-signals-stocks",),
    "security-audit-loop": ("security-audit-loop",), "security-audit": ("security-audit-loop",),
    "calls": ("calls",), "google-meet": ("calls",), "zoom-calls": ("calls",), "discord-voice": ("calls",),
    "aiteam-google-meet": ("calls",), "zoom-voice": ("calls",),
    "content-creator": ("seo-growth",), "social-media-manager": ("social-content",), "sales-agent": ("cold-email",),
    "infographic": ("explainer-videos",),
    # Shared skills that only paid add-ons ship (registry `shared:`): owning any of them unlocks it.
    "x-posting": ("seo-growth", "social-content", "cinematic-ads", "explainer-videos"),
    "content-calendar": ("social-content", "cinematic-ads", "explainer-videos"),
}
# Skills a paid add-on lists but that stay open, with the reason (pinned by tests/test_entitlements.py drift test).
FREE_ELSEWHERE: dict[str, str] = {
    "humanizer": "bundled with every Son (base image)",
    "competitor-news-monitor": "bundled with every Son (base image)",
    "manim-video": "bundled with every Son (base image)",
    "meeting-notes": "also shipped by the free Calendar add-on",
    "claims-check": "also shipped by free add-ons (CMO pack, Docs and PDFs, Websites)",
}
SKILL_TOOLS = ("skill_view",)

# Who is calling (verified upstream by the OAuth gate, forwarded as X-Aiteam-Sub; see patches/gate-aiteam-jwt.py).
CALLER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("aiteam_caller", default=None)


def required_addons(tool: str, args: Optional[dict]) -> tuple[str, ...]:
    """Add-on slugs that unlock this call (owning ANY of them is enough). Empty = free."""
    args = args or {}
    if tool in TOOL_ADDON:
        return (TOOL_ADDON[tool],)
    if tool in SKILL_TOOLS:
        name = str(args.get("name") or args.get("skill") or "").strip().strip("/")
        name = name.split("/")[-1] if name else ""
        return SKILL_ADDONS.get(name, ())
    return ()


def _default_plugs_path() -> pathlib.Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return pathlib.Path(os.environ.get("AITEAM_PLUGS_FILE") or pathlib.Path(home) / "plugs.json")


def _default_son_id() -> str:
    if os.environ.get("SON_ID"):
        return os.environ["SON_ID"]
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    try:
        return str(json.loads((pathlib.Path(home) / "mcp" / "son.json").read_text()).get("son_id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def exempt_emails(raw: Optional[str] = None) -> set[str]:
    raw = os.environ.get("AITEAM_GATE_EXEMPT", "") if raw is None else raw
    return {e.strip().lower() for e in raw.split(",") if "@" in e}


def add_link(slug: str, son_id: str = "") -> str:
    base = f"{DASHBOARD}/my-sons/{son_id}" if son_id else f"{DASHBOARD}/my-sons"
    return f"{base}?add={slug}"


def paywall_message(addon: Addon, link: str) -> str:
    return (f"This needs the {addon.name} add-on. Add it for ${addon.price_usd}/mo from your aiteam.space "
            f"dashboard: {link}")


def paywall_result(tool: str, slugs: tuple[str, ...], son_id: str = "") -> types.CallToolResult:
    """The upsell, as a normal (non-error) tool result. Names the cheapest add-on that unlocks the call."""
    addon = min((ADDONS[s] for s in slugs), key=lambda a: (a.price_usd, a.slug))
    link = add_link(addon.slug, son_id)
    msg = paywall_message(addon, link)
    data = {"paywall": True, "tool": tool, "message": msg, "url": link,
            "addon": {"slug": addon.slug, "name": addon.name, "price_usd": addon.price_usd, "period": "month"}}
    return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], structuredContent=data,
                                isError=False)


class Entitlements:
    """Reads plugs.json per call (mtime cache) and decides. ``check()`` returns the paywall result or None (= run)."""

    def __init__(self, path: Optional[pathlib.Path] = None, son_id: Optional[str] = None,
                 exempt: Optional[set[str]] = None):
        self.path = pathlib.Path(path) if path else _default_plugs_path()
        self.son_id = _default_son_id() if son_id is None else son_id
        self.exempt = exempt_emails() if exempt is None else {e.lower() for e in exempt}
        self._stamp: Any = None
        self._body: dict = {}

    def state(self) -> dict:
        try:
            st = self.path.stat()
        except OSError:
            self._stamp, self._body = None, {}
            return {}
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            try:
                body = json.loads(self.path.read_text())
            except (OSError, ValueError):
                body = {}
            self._stamp, self._body = stamp, body if isinstance(body, dict) else {}
        return self._body

    def owned(self) -> Optional[set[str]]:
        """Owned add-on slugs, or None when nothing is gated (no file, switch off, comp Son, old fleet)."""
        s = self.state()
        if not s or s.get("enforce") is not True or s.get("comp") is True:
            return None
        addons = s.get("addons")
        if not isinstance(addons, list):
            return None
        return {str(a) for a in addons}

    def check(self, tool: str, args: Optional[dict], caller: Optional[str] = None) -> Optional[types.CallToolResult]:
        need = required_addons(tool, args)
        if not need:
            return None
        who = (caller if caller is not None else CALLER.get()) or ""
        if who and who.strip().lower() in self.exempt:
            return None
        owned = self.owned()
        if owned is None or owned.intersection(need):
            return None
        return paywall_result(tool, need, self.son_id)
