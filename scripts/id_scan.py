#!/usr/bin/env python3
"""Repo hygiene scan: fail on host names, personal emails, Discord/Telegram IDs, home paths and secrets.

Runs in CI (.github/workflows/id-scan.yml) and locally: `python3 scripts/id_scan.py` from the repo root.
Scans every git-tracked text file. Exit 1 with a list of hits, 0 when clean.
`python3 scripts/id_scan.py --dir <path>` scans every file under <path> instead (the Son bundle gate,
scripts/build-bundle.sh): same rules, so nothing reaches a Son that CI would reject in the repo.

What fails:
  - deny terms: host / person / org names from the ID_SCAN_DENY secret (regexes, one per line)
  - email addresses, except placeholder domains (example.com/.org/.net, *.invalid, localhost,
    users.noreply.github.com) and the AI co-author trailer address
  - 17-20 digit numbers (Discord snowflakes), except one-digit-repeated placeholders (111..., 222...)
  - Telegram supergroup ids (-100 followed by 9+ digits)
  - /home/<user>/ paths and private IPv4 addresses
  - private key blocks and common token prefixes (ghp_, github_pat_, sk-..., xox[bp]-, AKIA...)

A line can opt out with the marker `id-scan: allow` (use for public upstream facts only, with a reason).
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL_DENY = ROOT / ".id-scan-deny.txt"   # git-ignored; CI uses the ID_SCAN_DENY secret
SELF = {"scripts/id_scan.py"}

# Product name, the one exception to the deny list (owner ruling: the connector introduces the Sons by name). Only these
# exact phrases (case-sensitive), only in the files below; they are blanked before the deny terms run, so the bare
# first name, any other team name, or the phrase in any other file still fails.
PRODUCT_PHRASES = ("Sons of Davis", "Son of Davis", "We are Davis,")
PRODUCT_FILES = {
    "README.md",
    "connector/aiteam_connector/intro.py",
    "connector/aiteam_connector/usage_guide.py",
    "connector/tests/test_intro.py",
    "scripts/build_skill.py",
    "skills/aiteam-mcp/SKILL.md",
    "claude-plugin/skills/aiteam-mcp/SKILL.md",
}

EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]*)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
EMAIL_OK_DOMAIN = re.compile(r"(^|\.)(example\.(com|org|net)|invalid|localhost|users\.noreply\.(github\.com|local))$", re.I)
EMAIL_OK_ADDR = {"noreply@anthropic.com"}
SNOWFLAKE = re.compile(r"(?<![\w.])\d{17,20}(?![\w.])")
TG_GROUP = re.compile(r"(?<![\w])-100\d{9,}\b")
HOME = re.compile(r"/home/(?!runner/)[a-z_][a-z0-9_-]*/")
PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
SECRETS = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
                     r"|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|\bxox[bp]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b")
ALLOW = "id-scan: allow"
# Test fixtures like 100000000000000009 / 200000000000000099: one digit, a run of zeros, a short suffix.
PLACEHOLDER_ID = re.compile(r"^[1-9]0{12,}[0-9]{0,3}$")
# Obvious test fixtures like "sk-canonical-default-must-never-escape": only lowercase words joined by hyphens.
FAKE_SECRET = re.compile(r"^sk-(?:[a-z]+-)+[a-z]+$")


def deny_terms() -> list[re.Pattern]:
    """Deny terms (one regex per line) come from the ID_SCAN_DENY Actions secret, never from a tracked file, so the
    list of names this scan blocks is not itself public. Local runs: export ID_SCAN_DENY, or put the lines in the
    git-ignored file .id-scan-deny.txt at the repo root. When ID_SCAN_DENY_REQUIRED=true (CI on main, tags and
    same-repo pull requests) a missing list fails the scan; fork pull requests get no secrets and run the generic
    rules only, with a warning."""
    raw = os.environ.get("ID_SCAN_DENY", "")
    if not raw.strip() and LOCAL_DENY.is_file():
        raw = LOCAL_DENY.read_text()
    if not raw.strip():
        if os.environ.get("ID_SCAN_DENY_REQUIRED", "").lower() == "true":
            print("id-scan: ID_SCAN_DENY is empty. Set the repo Actions secret ID_SCAN_DENY (one regex per line).",
                  file=sys.stderr)
            sys.exit(2)
        print("id-scan: warning: no deny list (ID_SCAN_DENY unset); generic rules only", file=sys.stderr)
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(re.compile(line, re.I))
    return out


def tracked() -> list[str]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True)
    return [p for p in r.stdout.split("\0") if p and p not in SELF]


def product_blank(line: str, rel: str) -> str:
    """The line with the product phrases blanked, in PRODUCT_FILES only. Same length, so columns stay put."""
    if rel not in PRODUCT_FILES:
        return line
    for ph in PRODUCT_PHRASES:
        line = line.replace(ph, " " * len(ph))
    return line


def scan_line(line: str, deny: list[re.Pattern], rel: str = "") -> list[str]:
    hits = []
    named = product_blank(line, rel)
    for pat in deny:
        m = pat.search(named)
        if m:
            hits.append(f"deny term {m.group(0)!r}")
    for m in EMAIL.finditer(line):
        addr, dom = m.group(0), m.group(2)
        if not EMAIL_OK_DOMAIN.search(dom) and addr.lower() not in EMAIL_OK_ADDR:
            hits.append(f"email {addr!r}")
    for m in SNOWFLAKE.finditer(line):
        s = m.group(0)
        if len(set(s)) > 1 and not PLACEHOLDER_ID.match(s):
            hits.append(f"17-20 digit id {s}")
    for rx, what in ((TG_GROUP, "telegram group id"), (HOME, "home path"), (PRIVATE_IP, "private IP")):
        m = rx.search(line)
        if m:
            hits.append(f"{what} {m.group(0)[:24]!r}")
    for m in SECRETS.finditer(line):
        if not FAKE_SECRET.match(m.group(0)):
            hits.append(f"secret-looking value {m.group(0)[:6]!r}...")
    return hits


def files(argv: list[str]) -> list[tuple[str, pathlib.Path]]:
    if len(argv) >= 2 and argv[0] == "--dir":
        base = pathlib.Path(argv[1]).resolve()
        return [(str(p.relative_to(base)), p) for p in sorted(base.rglob("*")) if p.is_file()]
    return [(rel, ROOT / rel) for rel in tracked()]


def main(argv: list[str] | None = None) -> int:
    deny = deny_terms()
    bad = 0
    for rel, p in files(sys.argv[1:] if argv is None else argv):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for h in scan_line(line, deny, rel):
                print(f"{rel}:{n}: {h}")
                bad += 1
    if bad:
        print(f"\nid-scan: {bad} hit(s). Replace with placeholders or env vars (see AGENTS.md).", file=sys.stderr)
        return 1
    print("id-scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
