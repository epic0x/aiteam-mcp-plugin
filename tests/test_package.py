"""Tests for scripts/build_package.py (the ChatGPT plugin submission ZIP). Stdlib + pytest.

Run from the repo root: python -m pytest -q tests/test_package.py
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_package.py"
sys.path.insert(0, str(ROOT / "scripts"))
import build_package  # noqa: E402

VERSION = "9.8.7"
STEM = f"aiteam-chatgpt-plugin-{VERSION}"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=ROOT)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    r = run("--version", VERSION, "--out", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    return out


@pytest.fixture
def src_copy(tmp_path):
    src = tmp_path / "package"
    shutil.copytree(ROOT / "package", src)
    return src


def edit_listing(src: pathlib.Path, fn) -> None:
    p = src / "listing.json"
    data = json.loads(p.read_text())
    fn(data)
    p.write_text(json.dumps(data, indent=2))


def no_placeholders(src: pathlib.Path) -> None:
    def fix(d):
        i = d["interface"]
        i["supportURL"] = "https://aiteam.space/support"
        i["privacyPolicyURL"] = "https://aiteam.space/privacy"
        i["termsOfServiceURL"] = "https://aiteam.space/terms"
        d["review"]["demo_recording_url"] = "https://aiteam.space/demo"
        d["publication"]["countries"] = []
    edit_listing(src, fix)
    for name in build_package.FINAL_PNGS.values():
        (src / "assets" / name).write_bytes(make_png(64, 64))
    edit_listing(src, lambda d: d["interface"].update({k: f"./assets/{n}" for k, n in build_package.FINAL_PNGS.items()}))


def make_png(w: int, h: int) -> bytes:
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------------------------- layout
def test_zip_has_single_plugin_root_at_archive_root(built):
    z = zipfile.ZipFile(built / f"{STEM}.zip")
    names = z.namelist()
    assert "plugin.json" in names and "mcp.json" in names
    assert "skills/get-started/SKILL.md" in names
    assert all(f"assets/{n}" in names for n in ("icon.png", "icon-dark.png", "logo.png", "logo-dark.png"))
    assert "skills/aiteam-mcp/SKILL.md" in names                              # connector skill bundled (bundleSkills)
    tops = {n.split("/")[0] for n in names}
    assert tops == {"plugin.json", "mcp.json", "skills", "assets"}, tops
    assert not any(part.startswith(".") for n in names for part in n.split("/")), names
    assert not any(n.endswith((".app.json", "hooks.json")) for n in names)
    assert all("\\" not in n and not n.startswith("/") and ".." not in n.split("/") for n in names)
    assert z.testzip() is None


def test_zip_matches_build_dir(built):
    z = zipfile.ZipFile(built / f"{STEM}.zip")
    files = sorted(p.relative_to(built / STEM).as_posix() for p in (built / STEM).rglob("*") if p.is_file())
    assert sorted(z.namelist()) == files


def test_manifests_valid_json_and_shape(built):
    z = zipfile.ZipFile(built / f"{STEM}.zip")
    plugin = json.loads(z.read("plugin.json"))
    mcp = json.loads(z.read("mcp.json"))
    assert plugin["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert plugin["name"] == "aiteam" and plugin["version"] == VERSION
    ext = plugin["extensions"]["com.openai"]
    assert set(ext) >= {"interface", "onboardingSkill", "review", "publication"}
    assert ext["onboardingSkill"] == "./skills/get-started/SKILL.md"
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert list(mcp["mcpServers"]) == ["aiteam"]
    s = mcp["mcpServers"]["aiteam"]
    assert s["type"] == "streamable-http" and s["url"] == "https://mcp.aiteam.space/mcp"
    assert s["extensions"]["com.openai"]["auth"] == {"type": "oauth", "client": {"mode": "dcr"}}
    # no notes, no PLACEHOLDER prefixes, no secrets in the shipped manifests
    blob = json.dumps(plugin) + json.dumps(mcp)
    assert "PLACEHOLDER:" not in blob and '"_' not in blob and "clientSecret" not in blob


def test_listing_limits(built):
    plugin = json.loads((built / STEM / "plugin.json").read_text())
    i = plugin["extensions"]["com.openai"]["interface"]
    assert len(i["displayName"]) <= 30 and len(i["shortDescription"]) <= 30
    assert len(i["longDescription"]) <= 4000 and len(i["developerName"]) <= 80
    assert 1 <= len(i["defaultPrompt"]) <= 3 and all(len(p) <= 128 for p in i["defaultPrompt"])
    for k in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert i[k].startswith("https://") and len(i[k]) <= 1024
    assert build_package.contrast(i["brandColor"], "#ffffff") >= 2.0
    cases = plugin["extensions"]["com.openai"]["review"]["test_cases"]
    assert len(cases["positive"]) == 5 and len(cases["negative"]) == 3
    assert "countries" not in plugin["extensions"]["com.openai"]["publication"]
    assert VERSION in plugin["extensions"]["com.openai"]["publication"]["release_notes"]


def test_icons_square_and_big_enough(built):
    for name in ("icon.png", "icon-dark.png", "logo.png", "logo-dark.png"):
        w, h = build_package.image_size(built / STEM / "assets" / name)
        assert w == h and w >= 48


def test_skill_frontmatter(built):
    meta = build_package.parse_frontmatter((built / STEM / "skills/get-started/SKILL.md").read_text())
    assert meta["name"] == "get-started" and meta["description"]


def test_placeholder_report_written(built):
    rep = (built / "PLACEHOLDERS.md").read_text()
    assert "review.demo_recording_url" in rep and "interface.supportURL" not in rep   # /support is live
    assert "interface.privacyPolicyURL" not in rep and "placeholder image" not in rep   # real pages + brand PNGs
    notes = (built / "RELEASE_NOTES.md").read_text()
    assert "Placeholders still in this build" in notes


# ------------------------------------------------------------------------------------------- strict / failures
def test_strict_fails_with_placeholders(tmp_path):
    r = run("--version", VERSION, "--out", str(tmp_path), "--strict")
    assert r.returncode == 1
    assert "placeholder" in r.stderr.lower()


def test_non_strict_passes_with_placeholders(tmp_path):
    r = run("--version", VERSION, "--out", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / f"{STEM}.zip").is_file()


def test_strict_passes_when_placeholders_replaced(src_copy, tmp_path):
    no_placeholders(src_copy)
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy), "--strict")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Placeholders: none" in r.stdout
    plugin = json.loads((tmp_path / "out" / STEM / "plugin.json").read_text())
    assert plugin["extensions"]["com.openai"]["publication"]["countries"] == []


def test_too_long_display_name_fails(src_copy, tmp_path):
    edit_listing(src_copy, lambda d: d["interface"].__setitem__("displayName", "x" * 31))
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1
    assert "displayName" in r.stderr
    assert not (tmp_path / "out" / f"{STEM}.zip").exists()


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["interface"].__setitem__("shortDescription", "y" * 31), "shortDescription"),
    (lambda d: d["interface"].__setitem__("defaultPrompt", ["a", "b", "c", "d"]), "defaultPrompt"),
    (lambda d: d["interface"].__setitem__("brandColor", "#ffff66"), "brandColor"),
    (lambda d: d["interface"].__setitem__("supportURL", "http://aiteam.space/support"), "supportURL"),
    (lambda d: d["review"]["test_cases"]["positive"].pop(), "positive"),
    (lambda d: d["review"]["test_cases"]["negative"].pop(), "negative"),
    (lambda d: d.__setitem__("name", "Not_Kebab"), "name"),
    (lambda d: d["interface"].__setitem__("category", "Things"), "category"),
])
def test_validation_rejects(src_copy, tmp_path, mutate, needle):
    edit_listing(src_copy, mutate)
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1 and needle in r.stderr, r.stderr


def test_non_square_icon_fails(src_copy, tmp_path):
    (src_copy / "assets" / "logo.png").write_bytes((src_copy / "assets" / "logo.png").read_bytes()[:16]
        + __import__("struct").pack(">II", 512, 256) + (src_copy / "assets" / "logo.png").read_bytes()[24:])
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1 and "square" in r.stderr


def test_png_size_read_via_struct(tmp_path):
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * 64 for _ in range(64))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    p = tmp_path / "i.png"
    p.write_bytes(png)
    assert build_package.image_size(p) == (64, 64)


def test_hidden_skill_dir_and_nested_skill_fail(src_copy, tmp_path):
    shutil.copytree(src_copy / "skills" / "get-started", src_copy / "skills" / ".hidden")
    nested = src_copy / "skills" / "get-started" / "inner"
    nested.mkdir()
    (nested / "SKILL.md").write_text("---\nname: inner\ndescription: x\n---\nbody\n")
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1
    assert "hidden" in r.stderr and "immediate children" in r.stderr


def test_skill_without_frontmatter_fails(src_copy, tmp_path):
    (src_copy / "skills" / "get-started" / "SKILL.md").write_text("# no front matter\n")
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1 and "front matter" in r.stderr


def test_two_mcp_servers_fail(tmp_path):
    plugin, mcp = build_package.make_manifests(
        build_package.strip_placeholders(json.loads((ROOT / "package/listing.json").read_text()))[0], VERSION)
    mcp["mcpServers"]["second"] = dict(mcp["mcpServers"]["aiteam"])
    errs = build_package.validate(plugin, mcp, tmp_path)
    assert any("exactly one MCP server" in e for e in errs)


def test_leak_in_package_fails(src_copy, tmp_path):
    p = src_copy / "skills" / "get-started" / "SKILL.md"
    p.write_text(p.read_text() + "\nContact someone@" + "gm" + "ail.com\n")  # split so the repo scan stays clean
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy))
    assert r.returncode == 1 and "leak scan" in r.stderr


def test_bad_version_fails(tmp_path):
    r = run("--version", "one", "--out", str(tmp_path))
    assert r.returncode == 1 and "semver" in r.stderr


def test_v_prefix_is_stripped(tmp_path):
    r = run("--version", "v1.2.3", "--out", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "aiteam-chatgpt-plugin-1.2.3.zip").is_file()


# ------------------------------------------------------------------------------------------- final PNG icons
@pytest.mark.parametrize("missing", sorted(build_package.FINAL_PNGS.values()))
def test_strict_fails_when_a_final_png_is_missing(src_copy, tmp_path, missing):
    no_placeholders(src_copy)
    (src_copy / "assets" / missing).unlink()
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy), "--strict")
    assert r.returncode == 1
    assert missing in r.stdout + r.stderr


def test_strict_fails_with_svg_icons_even_without_placeholder_text(src_copy, tmp_path):
    no_placeholders(src_copy)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512"/></svg>'
    (src_copy / "assets" / "logo.svg").write_text(svg)
    edit_listing(src_copy, lambda d: d["interface"].__setitem__("logo", "./assets/logo.svg"))
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy), "--strict")
    assert r.returncode == 1 and "logo.png" in r.stdout


def test_final_pngs_ship_in_the_zip(src_copy, tmp_path):
    no_placeholders(src_copy)
    r = run("--version", VERSION, "--out", str(tmp_path / "out"), "--src", str(src_copy), "--strict")
    assert r.returncode == 0, r.stdout + r.stderr
    names = set(zipfile.ZipFile(tmp_path / "out" / f"{STEM}.zip").namelist())
    assert {f"assets/{n}" for n in build_package.FINAL_PNGS.values()} <= names


# ------------------------------------------------------------------------------------------- tag on main guard
GUARD = ROOT / "scripts" / "tag_on_main.sh"


def _git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a").write_text("1")
    _git(tmp_path, "add", "a"); _git(tmp_path, "commit", "-qm", "one")
    _git(tmp_path, "checkout", "-qb", "side")
    (tmp_path / "a").write_text("2")
    _git(tmp_path, "commit", "-qam", "two")
    _git(tmp_path, "tag", "v0.0.2")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "tag", "v0.0.1")
    return tmp_path


def guard(repo, tag):
    return subprocess.run(["bash", str(GUARD), tag, "main"], cwd=repo, capture_output=True, text=True)


def test_tag_on_main_passes(repo):
    r = guard(repo, "v0.0.1")
    assert r.returncode == 0, r.stdout + r.stderr


def test_tag_off_main_fails(repo):
    r = guard(repo, "v0.0.2")
    assert r.returncode == 1 and "not on main" in r.stdout


def test_missing_tag_fails(repo):
    r = guard(repo, "v9.9.9")
    assert r.returncode == 1 and "does not exist" in r.stdout


def test_release_workflow_runs_the_guard_before_publishing():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "scripts/tag_on_main.sh" in wf
    assert wf.index("tag_on_main.sh") < wf.index("gh release create")


def test_leak_scan_product_phrase_only_in_bundled_skill(tmp_path, monkeypatch):
    # The bundled connector skill may say the product name; a bare deny term still fails (release v0.1.3 broke here).
    name = "Da" + "vis"                       # built in code so the repo id-scan never sees the bare term
    monkeypatch.setenv("ID_SCAN_DENY", name)
    (tmp_path / "skills" / "aiteam-mcp").mkdir(parents=True)
    (tmp_path / "skills" / "aiteam-mcp" / "SKILL.md").write_text(f"We are Sons of {name}.\nAsk {name}.\n")
    hits = build_package.leak_scan(tmp_path)
    assert len(hits) == 1 and ":2:" in hits[0]
