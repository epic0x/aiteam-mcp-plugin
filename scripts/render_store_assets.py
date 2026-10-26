"""Render the store assets (brand PNGs, voice-mode icons, listing screenshots) with headless Chromium.

    python scripts/render_store_assets.py      # needs playwright + chromium (run on the build box, not Actions)

Brand mark = aiteam.space app/icon.svg (dark rounded square, light dot). Voice-mode icons = the Claude / ChatGPT brand
mark (Simple Icons paths, unmodified, same as aiteam.space connector-hub.ts) plus the three-bar waveform from
plug-rules.ts VOICE_WAVE_D. Labels: "Claude \u2014 voice/live mode", "ChatGPT \u2014 voice mode".
Outputs: package/assets/{icon,icon-dark,logo,logo-dark,voice-claude,voice-chatgpt}.png and docs/store/screenshots/*.png
"""
from __future__ import annotations

import json
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "package" / "assets"
SHOTS = ROOT / "docs" / "store" / "screenshots"
LISTING = json.loads((ROOT / "package" / "listing.json").read_text())

INK, PAPER, BRAND = "#26251e", "#f7f7f4", "#f54e00"
CLAUDE_D = ("m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z")
CHATGPT_D = ("M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z")
WAVE_D = "M27 10h1.6v4H27zM30.2 6h1.6v12h-1.6zM33.4 9h1.6v6h-1.6z"
CLAUDE_LABEL = "Claude \u2014 voice/live mode"
CHATGPT_LABEL = "ChatGPT \u2014 voice mode"


def mark(bg: str, dot: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="{bg}"/><circle cx="16" cy="16" r="6" fill="{dot}"/></svg>'


def voice(d: str, color: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 24" style="color:{color}">'
            f'<path fill="currentColor" d="{d}"/><path fill="currentColor" d="{WAVE_D}"/></svg>')


def page(body: str, bg: str = PAPER) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>*{{margin:0;box-sizing:border-box}}"
            f"html,body{{background:{bg};font-family:Inter,-apple-system,'Segoe UI',Roboto,sans-serif;color:{INK}}}"
            "</style></head><body>" + body + "</body></html>")


def square(svg: str, px: int, bg: str = "transparent", pad: float = 0) -> str:
    inner = px - 2 * int(px * pad)
    return page(f"<div style='width:{px}px;height:{px}px;display:flex;align-items:center;justify-content:center;"
                f"background:{bg}'><div style='width:{inner}px'>{svg.replace('<svg ', '<svg width=\"100%\" ')}</div></div>",
                bg="transparent")


def voice_card(svg: str, label: str, line: str) -> str:
    return (f"<div style='display:flex;align-items:center;gap:22px;padding:26px 30px;border:1px solid #e3e2dc;"
            f"border-radius:18px;background:#fff'><div style='width:84px'>{svg.replace('<svg ', '<svg width=\"84\" ')}</div>"
            f"<div><div style='font-size:26px;font-weight:600'>{label}</div>"
            f"<div style='font-size:18px;color:#6b6a63;margin-top:6px'>{line}</div></div></div>")


def shot_voice() -> str:
    return page(
        "<div style='width:1280px;height:800px;padding:64px 80px'>"
        f"<div style='display:flex;align-items:center;gap:16px'><div style='width:48px'>{mark(INK, PAPER).replace('<svg ', '<svg width=\"48\" ')}</div>"
        "<div style='font-size:30px;font-weight:700'>aiteam.space</div></div>"
        "<div style='font-size:52px;font-weight:700;line-height:1.1;margin-top:44px;max-width:1000px'>Talk to your Son out loud.</div>"
        "<div style='font-size:24px;color:#6b6a63;margin-top:16px;max-width:980px'>Your AI teammate runs on its own computer. "
        "Connect it once, then hand it work by voice from the app you already use.</div>"
        "<div style='display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:56px'>"
        + voice_card(voice(CLAUDE_D, "#D97757"), CLAUDE_LABEL, "Speak in the Claude app. Your Son does the work.")
        + voice_card(voice(CHATGPT_D, "#000"), CHATGPT_LABEL, "Speak in the ChatGPT app. Your Son reports back.")
        + "</div><div style='font-size:18px;color:#6b6a63;margin-top:40px'>The connection is free. Sign in with your aiteam.space account and approve access.</div></div>")


def shot_tasks() -> str:
    rows = [("You", "Ask my Son what it is working on right now."),
            ("Son", "Three things. Done: pricing FAQ PR merged. In progress: weekly report PDF, about 10 minutes. "
                    "Waiting: your OK on the landing copy."),
            ("You", "Schedule a summary of open tasks every weekday at 9am."),
            ("Son", "Scheduled: weekdays 9:00, my timezone. Say \u201ccancel the 9am summary\u201d to stop it.")]
    bub = "".join(
        f"<div style='display:flex;justify-content:{'flex-end' if w == 'You' else 'flex-start'};margin-top:18px'>"
        f"<div style='max-width:820px;padding:18px 22px;border-radius:18px;font-size:22px;line-height:1.4;"
        f"background:{'#ecebe5' if w == 'You' else '#fff'};border:1px solid #e3e2dc'><b>{w}</b><br>{t}</div></div>"
        for w, t in rows)
    return page("<div style='width:1280px;height:800px;padding:56px 80px'>"
                "<div style='font-size:40px;font-weight:700'>Hand off work. Ask for status. Any time.</div>"
                "<div style='font-size:20px;color:#6b6a63;margin-top:10px'>Illustrative conversation. Your Son keeps "
                "working after the chat ends.</div>" + bub + "</div>")


def shot_plans() -> str:
    free = ["Connect from Claude or ChatGPT, voice mode included", "Hand tasks to your Son, read sessions and status",
            "Chat with your Son on Telegram (topics too), WhatsApp, Discord and the web",
            "GitHub read: pull requests, CI, issues"]
    paid = ["Discord thread control: create, rename, archive, members", "Two-way sync add-on, $5/mo on your monthly aiteam.space bill"]
    li = lambda xs: "".join(f"<li style='margin-top:12px'>{x}</li>" for x in xs)
    col = lambda title, xs, tag: (f"<div style='padding:30px;border:1px solid #e3e2dc;border-radius:18px;background:#fff'>"
                                  f"<div style='font-size:15px;font-weight:700;color:{BRAND};letter-spacing:.06em'>{tag}</div>"
                                  f"<div style='font-size:30px;font-weight:700;margin-top:6px'>{title}</div>"
                                  f"<ul style='font-size:21px;line-height:1.35;padding-left:22px;margin-top:8px'>{li(xs)}</ul></div>")
    return page("<div style='width:1280px;height:800px;padding:60px 80px'>"
                "<div style='font-size:40px;font-weight:700'>Free to connect. Pay only for add-ons you use.</div>"
                "<div style='display:grid;grid-template-columns:1.2fr 1fr;gap:28px;margin-top:40px'>"
                + col("Included", free, "FREE") + col("Discord thread management (two-way sync)", paid, "PAID ADD-ON, $5/MO") +
                "</div><div style='font-size:18px;color:#6b6a63;margin-top:34px'>One monthly bill with the card on "
                "file at aiteam.space. Without the add-on, a thread tool answers with how to add it and does not run.</div></div>")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()

        def snap(html: str, path: pathlib.Path, w: int, h: int, transparent: bool = False) -> None:
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.set_content(html)
            pg.screenshot(path=str(path), omit_background=transparent, clip={"x": 0, "y": 0, "width": w, "height": h})
            pg.close()
            print("wrote", path.relative_to(ROOT))

        snap(square(mark(INK, PAPER), 512), ASSETS / "icon.png", 512, 512, True)
        snap(square(mark(PAPER, INK), 512), ASSETS / "icon-dark.png", 512, 512, True)
        snap(square(mark(INK, PAPER), 1024), ASSETS / "logo.png", 1024, 1024, True)
        snap(square(mark(PAPER, INK), 1024), ASSETS / "logo-dark.png", 1024, 1024, True)
        snap(square(voice(CLAUDE_D, "#D97757"), 512, "#ffffff", 0.12), ASSETS / "voice-claude.png", 512, 512)
        snap(square(voice(CHATGPT_D, "#000000"), 512, "#ffffff", 0.12), ASSETS / "voice-chatgpt.png", 512, 512)
        snap(shot_voice(), SHOTS / "1-voice-modes.png", 1280, 800)
        snap(shot_tasks(), SHOTS / "2-hand-off-and-status.png", 1280, 800)
        snap(shot_plans(), SHOTS / "3-free-and-add-ons.png", 1280, 800)
        b.close()


if __name__ == "__main__":
    main()
