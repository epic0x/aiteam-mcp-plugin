#!/usr/bin/env python3
"""Build the ChatGPT plugin submission ZIP (portable Agent Plugins format) from package/.

    python3 scripts/build_package.py [--version X] [--strict] [--out dist] [--src package]

Input (owner-edited): package/listing.json, package/skills/<skill>/SKILL.md, package/assets/*.
Output:
  dist/aiteam-chatgpt-plugin-<version>/        the plugin root (plugin.json, mcp.json, skills/, assets/)
  dist/aiteam-chatgpt-plugin-<version>.zip     same tree, plugin root at the archive root, nothing else
  dist/PLACEHOLDERS.md                         values still marked PLACEHOLDER (and placeholder icons)
  dist/RELEASE_NOTES.md                        release body for the GitHub release

A listing value that starts with 'PLACEHOLDER:' ships without the prefix and is reported; 'PLACEHOLDER:omit' drops
the key. An asset file containing the word PLACEHOLDER is reported too. --strict exits 1 while any placeholder is
left. Validation follows OpenAI's plugin submission rules (deploy/submission + submission-errors); any violation
exits 1. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFIX = "PLACEHOLDER:"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
ZIP_STEM = "aiteam-chatgpt-plugin"
CATEGORIES = {"Productivity", "Creativity", "Developer Tools", "Business & Operations", "Data & Analytics",
              "Communication", "Education & Research", "Security", "Finance", "Healthcare", "Travel",
              "Entertainment", "Other"}
# The final (--strict) build ships PNG icons, one per listing key, as in OpenAI's package examples. Screenshots are
# left out on purpose: OpenAI asks for none when the plugin has no UI (this one has none).
FINAL_PNGS = {"composerIcon": "icon.png", "composerIconDark": "icon-dark.png", "logo": "logo.png", "logoDark": "logo-dark.png"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
MAX_IMAGE = 5 * 1024 * 1024
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


# ------------------------------------------------------------------------------------------- placeholders
def strip_placeholders(obj, path="", found=None):
    """Return (clean copy, [(path, value)]). Drops '_' note keys and 'PLACEHOLDER:omit' values."""
    found = [] if found is None else found
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.startswith("_"):
                continue
            sub = f"{path}.{k}" if path else k
            if isinstance(v, str) and v == PREFIX + "omit":
                found.append((sub, "(omitted)"))
                continue
            out[k] = strip_placeholders(v, sub, found)[0]
        return out, found
    if isinstance(obj, list):
        return [strip_placeholders(v, f"{path}[{i}]", found)[0] for i, v in enumerate(obj)], found
    if isinstance(obj, str) and obj.startswith(PREFIX):
        val = obj[len(PREFIX):]
        found.append((path, val))
        return val, found
    return obj, found


# ------------------------------------------------------------------------------------------- manifests
def make_manifests(listing: dict, version: str) -> tuple[dict, dict]:
    iface = dict(listing["interface"])
    review = listing.get("review")
    pub = dict(listing.get("publication") or {})
    if "release_notes" in pub and isinstance(pub["release_notes"], str):
        pub["release_notes"] = pub["release_notes"].replace("{version}", version)
    ext = {"interface": iface}
    if listing.get("onboardingSkill"):
        ext["onboardingSkill"] = listing["onboardingSkill"]
    if review is not None:
        ext["review"] = review
    if pub:
        ext["publication"] = pub
    plugin = {"$schema": PLUGIN_SCHEMA, "name": listing["name"], "version": version}
    for k in ("description", "author", "homepage", "repository", "license", "keywords"):
        if k in listing:
            plugin[k] = listing[k]
    plugin["extensions"] = {"com.openai": ext}

    m = listing["mcp"]
    server = {"type": "streamable-http", "url": m["url"]}
    if m.get("auth"):
        server["extensions"] = {"com.openai": {"auth": m["auth"]}}
    mcp = {"$schema": MCP_SCHEMA, "mcpServers": {m["server"]: server}}
    return plugin, mcp


# ------------------------------------------------------------------------------------------- validation
def one_line_text(s) -> bool:
    return isinstance(s, str) and s.strip() != "" and not any(unicodedata.category(c) == "Cc" for c in s)


def multi_line_text(s) -> bool:
    return isinstance(s, str) and s.strip() != "" and not any(
        unicodedata.category(c) == "Cc" and c != "\n" for c in s)


def https_url(s, limit=1024) -> bool:
    return (isinstance(s, str) and s.startswith("https://") and len(s) <= limit and len(s) > len("https://")
            and "@" not in s.split("/")[2] and not any(c.isspace() for c in s))


def luminance(hexcolor: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def image_size(p: pathlib.Path) -> tuple[float, float]:
    data = p.read_bytes()
    ext = p.suffix.lower()
    if ext == ".svg":
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(data.decode("utf-8"))
        except (ET.ParseError, UnicodeDecodeError) as e:
            raise BuildError(f"{p.name}: SVG is not valid UTF-8 XML ({e})")
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise BuildError(f"{p.name}: root element must be <svg>")
        vb = root.get("viewBox")
        if vb:
            parts = re.split(r"[\s,]+", vb.strip())
            try:
                w, h = float(parts[2]), float(parts[3])
            except (IndexError, ValueError):
                raise BuildError(f"{p.name}: viewBox must be numeric")
            return w, h
        try:
            return float(root.get("width", "")), float(root.get("height", ""))
        except ValueError:
            raise BuildError(f"{p.name}: SVG needs a numeric viewBox or numeric width/height")
    if ext == ".png":
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise BuildError(f"{p.name}: not a PNG file")
        return struct.unpack(">II", data[16:24])
    if ext in (".jpg", ".jpeg"):
        i = 2
        if data[:2] != b"\xff\xd8":
            raise BuildError(f"{p.name}: not a JPEG file")
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg
        raise BuildError(f"{p.name}: JPEG size not found")
    if ext == ".webp":
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise BuildError(f"{p.name}: not a WebP file")
        kind = data[12:16]
        if kind == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = int.from_bytes(data[21:25], "little")
            return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        raise BuildError(f"{p.name}: unknown WebP chunk")
    raise BuildError(f"{p.name}: image must be .png, .jpg, .jpeg, .webp or .svg")


def parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise BuildError("SKILL.md must start with YAML front matter between --- lines")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise BuildError("SKILL.md front matter is not closed with ---")
    meta = {}
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#") or ln.startswith((" ", "\t")):
            continue
        k, sep, v = ln.partition(":")
        if not sep:
            raise BuildError(f"SKILL.md front matter line is not 'key: value': {ln!r}")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        meta[k.strip()] = v
    if not "\n".join(lines[end + 1:]).strip():
        raise BuildError("SKILL.md body must not be empty")
    return meta


def norm_prompt(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def validate(plugin: dict, mcp: dict, root: pathlib.Path) -> list[str]:
    err: list[str] = []
    name = plugin.get("name")
    if not isinstance(name, str) or not NAME.match(name) or len(name) > 64:
        err.append("name: lowercase letters, digits and single hyphens, at most 64 characters")
    if not isinstance(plugin.get("version"), str) or not SEMVER.match(plugin["version"]) or len(plugin["version"]) > 64:
        err.append(f"version: must be semver, got {plugin.get('version')!r}")
    if plugin.get("$schema") != PLUGIN_SCHEMA:
        err.append("plugin.json $schema must be the Agent Plugins 1.0.0 schema")
    desc = plugin.get("description")
    if desc is not None and (not multi_line_text(desc) or len(desc) > 1024):
        err.append("description: non-empty, at most 1024 characters")
    author = plugin.get("author") or {}
    if author and (not one_line_text(author.get("name")) or len(author["name"]) > 120):
        err.append("author.name: required, one line, at most 120 characters")
    if author.get("url") and not https_url(author["url"], 2048):
        err.append("author.url: HTTPS, at most 2048 characters")
    if plugin.get("homepage") and not https_url(plugin["homepage"], 2048):
        err.append("homepage: HTTPS, at most 2048 characters")

    ext = (plugin.get("extensions") or {}).get("com.openai") or {}
    iface = ext.get("interface") or {}
    for key, limit in (("displayName", 30), ("shortDescription", 30), ("developerName", 80)):
        v = iface.get(key)
        if not one_line_text(v) or "\n" in v:
            err.append(f"interface.{key}: required, one line")
        elif len(v) > limit:
            err.append(f"interface.{key}: {len(v)} characters, at most {limit}")
    ld = iface.get("longDescription")
    if not multi_line_text(ld):
        err.append("interface.longDescription: required")
    elif len(ld) > 4000:
        err.append(f"interface.longDescription: {len(ld)} characters, at most 4000")
    if iface.get("category") not in CATEGORIES:
        err.append(f"interface.category: {iface.get('category')!r} is not a supported category")
    caps = iface.get("capabilities", [])
    if not isinstance(caps, list) or len(caps) > 20 or any(
            not one_line_text(c) or "\n" in c or len(c) > 120 for c in caps):
        err.append("interface.capabilities: at most 20, each one line and at most 120 characters")
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if not https_url(iface.get(key)):
            err.append(f"interface.{key}: required HTTPS URL, at most 1024 characters")
    prompts = iface.get("defaultPrompt", [])
    prompts = [prompts] if isinstance(prompts, str) else prompts
    if not isinstance(prompts, list) or len(prompts) > 3:
        err.append("interface.defaultPrompt: at most 3 prompts")
    else:
        for p in prompts:
            if not one_line_text(p) or "\n" in p or len(p) > 128:
                err.append(f"interface.defaultPrompt: one line, at most 128 characters: {p!r}")
            elif "@" in p:
                err.append(f"interface.defaultPrompt: no @mentions: {p!r}")
        if len({norm_prompt(p) for p in prompts if isinstance(p, str)}) != len(prompts):
            err.append("interface.defaultPrompt: prompts must be unique")
    for key, bg in (("brandColor", "#ffffff"), ("brandColorDark", "#212121")):
        c = iface.get(key)
        if c is None:
            continue
        if not isinstance(c, str) or not HEX.match(c):
            err.append(f"interface.{key}: must be #RRGGBB")
        elif contrast(c, bg) < 2.0:
            err.append(f"interface.{key}: contrast {contrast(c, bg):.2f}:1 against {bg}, needs at least 2:1")
    for key in ("logo", "composerIcon", "logoDark", "composerIconDark"):
        v = iface.get(key)
        if v is None:
            if key in ("logo", "composerIcon"):
                err.append(f"interface.{key}: required for submission")
            continue
        if not isinstance(v, str) or not v.startswith("./") or ".." in v.split("/") or v != v.strip():
            err.append(f"interface.{key}: must be a ./ path inside the plugin")
            continue
        p = root / v[2:]
        if not p.is_file():
            err.append(f"interface.{key}: {v} does not exist")
            continue
        if p.suffix.lower() not in IMAGE_EXT:
            err.append(f"interface.{key}: {v} must be png, jpg, webp or svg")
            continue
        if p.stat().st_size > MAX_IMAGE:
            err.append(f"interface.{key}: {v} is larger than 5 MiB")
        try:
            w, h = image_size(p)
        except BuildError as e:
            err.append(f"interface.{key}: {e}")
            continue
        if w != h:
            err.append(f"interface.{key}: {v} must be square, is {w:g}x{h:g}")
        if min(w, h) < 48:
            err.append(f"interface.{key}: {v} must be at least 48x48")
        if p.suffix.lower() != ".svg" and max(w, h) > 4096:
            err.append(f"interface.{key}: {v} must be at most 4096x4096")
    if "screenshots" in iface:
        err.append("interface.screenshots: only for MCP servers with custom UI; leave out")

    onb = ext.get("onboardingSkill")
    if onb is not None and (not isinstance(onb, str) or not onb.startswith("./skills/")
                            or not (root / onb[2:]).is_file()):
        err.append(f"onboardingSkill: {onb!r} must point at an included ./skills/<skill>/SKILL.md")

    review = ext.get("review") or {}
    for bad in ("test_credentials", "reviewer_instructions"):
        if bad in review:
            err.append(f"review.{bad}: not allowed in the package (enter it in the dashboard)")
    cases = review.get("test_cases") or {}
    pos, neg = cases.get("positive") or [], cases.get("negative") or []
    if len(pos) != 5:
        err.append(f"review.test_cases.positive: exactly 5 required, got {len(pos)}")
    if len(neg) != 3:
        err.append(f"review.test_cases.negative: exactly 3 required, got {len(neg)}")
    for kind, lst, need in (("positive", pos, ("description", "prompt", "tools_triggered", "expected_behavior")),
                            ("negative", neg, ("description", "prompt"))):
        for i, c in enumerate(lst):
            for f in need:
                if not multi_line_text(c.get(f)):
                    err.append(f"review.test_cases.{kind}[{i}].{f}: required")
    if review.get("demo_recording_url") is not None and not https_url(review["demo_recording_url"]):
        err.append("review.demo_recording_url: HTTPS URL")
    pub = ext.get("publication") or {}
    countries = pub.get("countries")
    if countries is not None and (not isinstance(countries, list) or any(
            not isinstance(c, str) or not re.match(r"^[A-Z]{2}$", c) for c in countries)):
        err.append("publication.countries: list of uppercase 2-letter country codes")
    if "commerce" in pub or "commerce_description" in pub:
        err.append("commerce fields belong in review, not publication")

    # mcp.json
    if mcp.get("$schema") != MCP_SCHEMA:
        err.append("mcp.json $schema must be the Agent Plugins 1.0.0 MCP schema")
    servers = mcp.get("mcpServers") or {}
    if len(servers) != 1:
        err.append(f"mcp.json: exactly one MCP server required, got {len(servers)}")
    for sname, s in servers.items():
        if not sname.strip():
            err.append("mcp.json: server name must be non-empty")
        if s.get("type") != "streamable-http" or not https_url(s.get("url")):
            err.append(f"mcp.json {sname}: type streamable-http with an HTTPS url")
        if s.get("headers"):
            err.append(f"mcp.json {sname}: headers must be empty for public submission")
        auth = ((s.get("extensions") or {}).get("com.openai") or {}).get("auth") or {}
        blob = json.dumps(auth)
        if "clientSecret" in blob or "client_secret\"" in blob:
            err.append(f"mcp.json {sname}: never include a client secret")
        client = auth.get("client") or {}
        if client.get("mode") in ("dcr", "cimd") and "clientId" in client:
            err.append(f"mcp.json {sname}: no clientId with {client['mode']} registration")
        if auth.get("type") not in ("oauth", "mixed", "none", "api_key"):
            err.append(f"mcp.json {sname}: auth.type must be oauth, mixed, none or api_key")

    # tree
    skills = root / "skills"
    if not skills.is_dir():
        err.append("skills/: at least one skill required")
    else:
        names = set()
        for d in sorted(skills.iterdir()):
            if d.is_file() or d.is_symlink():
                err.append(f"skills/{d.name}: only skill directories belong directly under skills/")
                continue
            if d.name.startswith("."):
                err.append(f"skills/{d.name}: skill directory must not be hidden")
                continue
            sk = d / "SKILL.md"
            if not sk.is_file():
                err.append(f"skills/{d.name}: missing SKILL.md")
                continue
            for nested in d.rglob("SKILL.md"):
                if nested != sk:
                    err.append(f"{nested.relative_to(root)}: skills must be immediate children of skills/")
            try:
                meta = parse_frontmatter(sk.read_text(encoding="utf-8"))
            except (BuildError, UnicodeDecodeError) as e:
                err.append(f"skills/{d.name}/SKILL.md: {e}")
                continue
            sn, sd = meta.get("name", ""), meta.get("description", "")
            if not sn:
                err.append(f"skills/{d.name}/SKILL.md: front matter name required")
            if not sd or len(sd) > 1024:
                err.append(f"skills/{d.name}/SKILL.md: front matter description required, at most 1024 characters")
            if sn in names:
                err.append(f"skills/{d.name}/SKILL.md: duplicate skill name {sn!r}")
            names.add(sn)
            if isinstance(name, str) and len(f"{name}:{sn}") > 64:
                err.append(f"skills/{d.name}: '{name}:{sn}' is longer than 64 characters")
        if not names:
            err.append("skills/: at least one skill required")
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            err.append(f"{rel}: hidden files are not shipped (.app.json, .mcp.json, .codex-plugin ...)")
        if p.name in ("hooks.json",) or (p.is_dir() and p.name == "hooks" and p.parent == root):
            err.append(f"{rel}: lifecycle hooks can't be submitted")
        if p.is_symlink():
            err.append(f"{rel}: symlinks are not allowed")
        if p.is_file() and p.stat().st_size > MAX_IMAGE:
            err.append(f"{rel}: larger than 5 MiB")
    return err


# ------------------------------------------------------------------------------------------- build
def detect_version() -> str:
    for cmd in (["git", "describe", "--tags", "--exact-match"], ["git", "describe", "--tags", "--abbrev=0"]):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().removeprefix("v")
    raise BuildError("no --version given and no git tag found")


def write_zip(src: pathlib.Path, dest: pathlib.Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            if p.is_dir():
                continue
            info = zipfile.ZipInfo(rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())


def leak_scan(*paths: pathlib.Path) -> list[str]:
    """Same rules as scripts/id_scan.py (deny list from ID_SCAN_DENY), over every file under each path."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import id_scan  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    deny = id_scan.deny_terms()
    hits = []
    for base in paths:
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if id_scan.ALLOW not in line:
                    rel = p.relative_to(base).as_posix()   # bundled skills/aiteam-mcp/SKILL.md gets the product-phrase rule
                    hits += [f"{base.name}/{rel}:{n}: {h}" for h in id_scan.scan_line(line, deny, rel)]
    return hits


def build(src: pathlib.Path, out: pathlib.Path, version: str | None, strict: bool) -> dict:
    version = version or detect_version()
    version = version.removeprefix("v")
    raw = json.loads((src / "listing.json").read_text(encoding="utf-8"))
    listing, found = strip_placeholders(raw)
    plugin, mcp = make_manifests(listing, version)

    stem = f"{ZIP_STEM}-{version}"
    out.mkdir(parents=True, exist_ok=True)
    root = out / stem
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps(plugin, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps(mcp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copytree(src / "skills", root / "skills",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    # Bundle the connector's own skills (repo skills/<name>, generated from the usage guide) into the same package,
    # so one install carries MCP + skills. listing.json "bundleSkills" names them; never shipped as a manifest key.
    for name in listing.get("bundleSkills") or []:
        bsrc = ROOT / "skills" / name
        if not (bsrc / "SKILL.md").is_file():
            raise BuildError(f"bundleSkills: skills/{name}/SKILL.md not found")
        shutil.copytree(bsrc, root / "skills" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    iface = listing["interface"]
    for key in ("logo", "logoDark", "composerIcon", "composerIconDark"):
        v = iface.get(key)
        if isinstance(v, str) and v.startswith("./") and (src / v[2:]).is_file():
            dest = root / v[2:]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / v[2:], dest)
            if (src / v[2:]).suffix.lower() == ".svg" and "PLACEHOLDER" in (src / v[2:]).read_text("utf-8", "ignore"):
                found.append((f"interface.{key}", f"{v} is a placeholder image"))

    for key, name in FINAL_PNGS.items():
        v = iface.get(key)
        if v != f"./assets/{name}" or not (src / "assets" / name).is_file():
            found.append((f"interface.{key}", f"final PNG required: ./assets/{name} (square, 48 to 4096 px)"))

    errors = validate(plugin, mcp, root)
    leaks = leak_scan(src, root)
    errors += [f"leak scan: {h}" for h in leaks]

    review_notes = []
    if not plugin["extensions"]["com.openai"].get("review", {}).get("demo_recording_url"):
        review_notes = ["", "## MCP review prerequisite", "",
                        "A reviewer-accessible video walkthrough URL is still required before MCP review. "
                        "It is optional in the ZIP; provide a real URL in the review form or a later package. "
                        "Domain verification and a reviewer test account must also be completed in the portal."]
    report = ["# Placeholders", "",
              f"Package {stem}. Replace these in package/listing.json (or package/assets/) before submitting.", ""]
    report += [f"- `{p}`: {v}" for p, v in found] or ["None. The package is ready to upload."]
    report += review_notes
    (out / "PLACEHOLDERS.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    zpath = out / f"{stem}.zip"
    if zpath.exists():
        zpath.unlink()
    if not errors:
        write_zip(root, zpath)
    pub_notes = plugin["extensions"]["com.openai"].get("publication", {}).get("release_notes", "")
    notes = [f"ChatGPT plugin package {version}.", "",
             f"Download `{stem}.zip` and upload it at platform.openai.com > Plugins (see docs/SUBMISSION.md).", ""]
    if pub_notes:
        notes += [pub_notes, ""]
    if found:
        notes += ["## Placeholders still in this build", "", "Replace before submitting for review:", ""]
        notes += [f"- `{p}`: {v}" for p, v in found]
    else:
        notes += ["No placeholders: ready to upload."]
    notes += review_notes
    (out / "RELEASE_NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return {"version": version, "root": root, "zip": zpath, "placeholders": found, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", help="package version (default: current git tag without the v)")
    ap.add_argument("--strict", action="store_true", help="fail while any placeholder remains")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="output directory (default: dist)")
    ap.add_argument("--src", default=str(ROOT / "package"), help="package source directory (default: package)")
    a = ap.parse_args(argv)
    try:
        res = build(pathlib.Path(a.src), pathlib.Path(a.out), a.version, a.strict)
    except (BuildError, json.JSONDecodeError, KeyError) as e:
        print(f"build_package: error: {e}", file=sys.stderr)
        return 1
    if res["placeholders"]:
        print(f"Placeholders ({len(res['placeholders'])}):")
        for p, v in res["placeholders"]:
            print(f"  - {p}: {v}")
    else:
        print("Placeholders: none")
    if res["errors"]:
        print(f"\nValidation failed ({len(res['errors'])}):", file=sys.stderr)
        for e in res["errors"]:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"Built {res['zip']}")
    if a.strict and res["placeholders"]:
        print(f"--strict: {len(res['placeholders'])} placeholder(s) remain; see {pathlib.Path(a.out) / 'PLACEHOLDERS.md'}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
