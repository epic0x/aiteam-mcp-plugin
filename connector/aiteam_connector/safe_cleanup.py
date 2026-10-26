"""Safe cleanup for a Son's computer: removes only files that can be rebuilt. Run by ``POST /v1/cleanup``.

What it removes (each only when untouched for ``--min-age-h`` hours, default 2):
  * build/cache dirs: node_modules, .next, .turbo, .pytest_cache, __pycache__, .parcel-cache, .vite
    (skipped when a live process works inside the project, or when git tracks the dir)
  * package caches: ~/.npm/_npx, ~/.npm/_cacache, ~/.cache/pip, ~/.cache/uv
  * old media, logs and archives in the scratch dirs (<root>/cache/scratch, <root>/tmp), older than 6 hours

Never touched: memory, sessions, skills, plugins, config, .env, git repos' tracked files, anything outside ``--root``
except the package caches above, and runtime installs: anything inside a virtual environment (a dir with
``pyvenv.cfg``, ``venv``, ``.venv``), ``site-packages`` / ``dist-packages``, a global npm prefix (``<prefix>/lib/
node_modules``, which holds npm itself and the global CLIs) and the ``node`` / ``hermes-agent`` installs. Prints one summary line, then up to 8 of the biggest removals.

  python -m aiteam_connector.safe_cleanup --root ~/.hermes [--min-age-h 2] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time

NAMES = {"node_modules", ".next", ".turbo", ".pytest_cache", "__pycache__", ".parcel-cache", ".vite"}
NEVER = ("skills", "plugins", "memories", "sessions", "lsp", "lazy-packages", "addons", "cron", "mcp-gateway",
         "credentials", "profiles", "home/.local", ".cache/huggingface", "home/.cache/huggingface",
         "home/.cache/typescript", "workspace/meetings", "node", "hermes-agent", "venv", ".venv")
# Runtime installs, pruned at ANY depth: deleting inside them breaks a live interpreter or npm.
RUNTIME_DIRS = {"venv", ".venv", "site-packages", "dist-packages", "hermes-agent"}
MEDIA = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".wav", ".mp3", ".log", ".tar", ".tgz",
         ".zip", ".gz")
SCRATCH = ("cache/scratch", "tmp")
MEDIA_AGE_H = 6.0


def size(p: str) -> int:
    t = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return t


def newest_mtime(p: str, limit: int = 4000) -> float:
    m, n = os.lstat(p).st_mtime, 0
    for d, ds, fs in os.walk(p):
        for x in ds + fs:
            n += 1
            try:
                m = max(m, os.lstat(os.path.join(d, x)).st_mtime)
            except OSError:
                pass
            if n > limit:
                return m
    return m


def in_use(project: str, proc: str = "/proc") -> bool:
    for pid in os.listdir(proc):
        if pid.isdigit():
            try:
                cwd = os.readlink(f"{proc}/{pid}/cwd")
            except OSError:
                continue
            if cwd == project or cwd.startswith(project.rstrip("/") + "/"):
                return True
    return False


def git_tracked(p: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", os.path.dirname(p), "ls-files", "--", os.path.basename(p)],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: keep it
    if r.returncode != 0:
        return False  # not a git repo
    return r.stdout.strip() != ""


def is_runtime(d: str, files: list[str]) -> bool:
    """A dir that belongs to an installed runtime: a virtualenv root, or a site/dist-packages tree."""
    parts = d.replace("\\", "/").split("/")
    return ("pyvenv.cfg" in files or "site-packages" in parts or "dist-packages" in parts
            or any(a == "lib" and b == "node_modules" for a, b in zip(parts, parts[1:])))


def plan(root: str, min_age_h: float, now: float) -> list[tuple[str, str]]:
    """[(kind, path)] that are safe to remove. Pure apart from filesystem reads (tests use a temp tree)."""
    root = os.path.abspath(root)
    never = tuple(os.path.join(root, n) for n in NEVER)
    cutoff = now - min_age_h * 3600
    out: list[tuple[str, str]] = []
    for d, ds, fs in os.walk(root):
        if d.startswith(never) or "/.git" in d or is_runtime(d, fs):
            ds[:] = []
            continue
        for x in list(ds):
            if x in RUNTIME_DIRS:
                ds.remove(x)
                continue
            if x == "node_modules" and os.path.basename(d) == "lib":
                ds.remove(x)  # <prefix>/lib/node_modules = a global npm prefix (npm itself), never a project's
                continue
            if x in NAMES:
                p = os.path.join(d, x)
                ds.remove(x)
                try:
                    if newest_mtime(p) <= cutoff and not in_use(d) and not git_tracked(p):
                        out.append(("build cache", p))
                except OSError:
                    continue
    home = os.path.expanduser("~")
    for c in (".npm/_npx", ".npm/_cacache", ".cache/pip", ".cache/uv"):
        for base in {home, os.path.join(root, "home")}:
            p = os.path.join(base, c)
            try:
                if os.path.isdir(p) and newest_mtime(p) <= cutoff:  # never under a running install
                    out.append(("package cache", p))
            except OSError:
                continue
    media_cut = now - MEDIA_AGE_H * 3600
    for s in SCRATCH:
        sd = os.path.join(root, s)
        for d, ds, fs in os.walk(sd):
            if "/.git" in d or "/node_modules" in d:
                ds[:] = []
                continue
            for f in fs:
                if f.lower().endswith(MEDIA):
                    p = os.path.join(d, f)
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    if st.st_mtime < media_cut and not os.path.islink(p):
                        out.append(("old scratch file", p))
    seen, uniq = set(), []
    for k, p in out:
        if p not in seen:
            seen.add(p)
            uniq.append((k, p))
    return uniq


def run(root: str, min_age_h: float = 2.0, dry: bool = False) -> tuple[int, list[tuple[int, str]]]:
    freed, removed = 0, []
    for _kind, p in plan(root, min_age_h, time.time()):
        try:
            n = size(p) if os.path.isdir(p) else os.lstat(p).st_size
            if not dry:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
        except OSError:
            continue
        freed += n
        removed.append((n, p))
    return freed, removed


def main(argv=None) -> None:
    a = argparse.ArgumentParser(prog="safe_cleanup")
    a.add_argument("--root", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    a.add_argument("--min-age-h", type=float, default=2.0)
    a.add_argument("--dry-run", action="store_true")
    o = a.parse_args(argv)
    freed, removed = run(o.root, o.min_age_h, o.dry_run)
    removed.sort(reverse=True)
    print(f"safe cleanup{' (dry run)' if o.dry_run else ''}: freed {freed / 1e9:.2f} GB from {len(removed)} items")
    for n, p in removed[:8]:
        print(f"- {n / 1048576:.0f} MB {p}")


if __name__ == "__main__":
    main()
