"""The Claude plugin version and the changelog move together, so each release can be re-validated in the directory."""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_claude_plugin_version_matches_latest_changelog_release():
    plugin = json.loads((ROOT / "claude-plugin/.claude-plugin/plugin.json").read_text())
    m = re.search(r"^## v(\d+\.\d+\.\d+) ", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert m, "CHANGELOG.md needs a '## vX.Y.Z (date)' release heading"
    assert plugin["version"] == m.group(1)


def test_changelog_has_no_unreleased_sections_above_the_latest_release():
    text = (ROOT / "CHANGELOG.md").read_text()
    first = text.index("\n## v")
    assert "## Unreleased" not in text[:first]
