"""skills/aiteam-mcp/SKILL.md must equal the bundled usage guide (scripts/build_skill.py keeps them in sync)."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_skill_md_matches_usage_guide():
    spec = importlib.util.spec_from_file_location("build_skill", ROOT / "scripts" / "build_skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    on_disk = (ROOT / "skills" / "aiteam-mcp" / "SKILL.md").read_text()
    assert on_disk == mod.render(), "SKILL.md is stale: run python scripts/build_skill.py"
    assert on_disk.startswith("---\nname: aiteam-mcp\n")
    bundled = (ROOT / "claude-plugin" / "skills" / "aiteam-mcp" / "SKILL.md").read_text()
    assert bundled == on_disk, "claude-plugin skill copy is stale: run python scripts/build_skill.py"
