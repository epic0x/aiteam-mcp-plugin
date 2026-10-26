"""Write skills/aiteam-mcp/SKILL.md (+ dist/aiteam-mcp.zip) from the connector's bundled usage guide.

One source of truth: connector/aiteam_connector/usage_guide.py. The skill is the same text, wrapped in skill front
matter, for clients that load skills (Claude: Settings > Capabilities > Skills > Upload skill). Run after editing
the guide; tests fail when SKILL.md is out of date.

    python scripts/build_skill.py          # rewrite SKILL.md
    python scripts/build_skill.py --zip    # also build dist/aiteam-mcp.zip (upload this to Claude)
"""
from __future__ import annotations

import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connector"))
from aiteam_connector.usage_guide import GUIDE, GUIDE_VERSION  # noqa: E402

SKILL = ROOT / "skills" / "aiteam-mcp" / "SKILL.md"
# Same skill, bundled in the Claude plugin (claude-plugin/: .claude-plugin/plugin.json + .mcp.json + skills/).
CLAUDE_PLUGIN_SKILL = ROOT / "claude-plugin" / "skills" / "aiteam-mcp" / "SKILL.md"
FRONT = (
    "---\n"
    "name: aiteam-mcp\n"
    "description: Use when connected to an aiteam.space Son through the aiteam MCP connector (Claude or ChatGPT). "
    "Who we are (the Sons of Davis, introduced on connect), then how to hand a task to Sons, keep one Discord thread per task, run headless work, and answer 'what are the "
    "updates?'.\n"
    f"version: 1.{GUIDE_VERSION}.0\n"
    "license: Apache-2.0\n"
    "---\n"
    "<!-- Generated from connector/aiteam_connector/usage_guide.py by scripts/build_skill.py. Edit the guide, not this file. -->\n\n"
)


def render() -> str:
    return FRONT + GUIDE.strip() + "\n"


def main() -> None:
    for path in (SKILL, CLAUDE_PLUGIN_SKILL):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render())
        print(f"wrote {path.relative_to(ROOT)}")
    if "--zip" in sys.argv:
        out = ROOT / "dist" / "aiteam-mcp.zip"
        out.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            info = zipfile.ZipInfo("aiteam-mcp/SKILL.md", date_time=(2026, 1, 1, 0, 0, 0))
            z.writestr(info, render())
        print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
