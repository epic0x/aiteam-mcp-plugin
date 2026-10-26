#!/usr/bin/env bash
# Run aiteam-mcp-plugin's CHECK jobs locally (same checks as the GitHub Actions workflows).
# TESTS ONLY: no release, no GitHub release upload, no secrets, no network writes.
#
#   scripts/box-test.sh   # id-scan (self-test + scan), connector tests + CLI entry points, relay tests + CLI entry point,
#                         # package builder tests + non-strict package build (into a temp dir)
#
# id-scan deny terms come from $ID_SCAN_DENY or the git-ignored .id-scan-deny.txt (same as a local run of
# scripts/id_scan.py). With neither, the generic rules still run and the summary says the deny terms were not checked.
# Skipped always: release.yml (tag-on-main check + GitHub release upload).
# Python envs live under ~/.cache/box-test (uv when present); installs hold the shared npm-install.lock.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
for a in "$@"; do
  case "$a" in
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done
LOCK_DIR=${BOX_TEST_LOCK_DIR:-${TMPDIR:-/tmp}}
mkdir -p "$LOCK_DIR" 2>/dev/null || LOCK_DIR=${TMPDIR:-/tmp}
VENV_ROOT=${BOX_TEST_VENV_ROOT:-$HOME/.cache/box-test/aiteam-mcp-plugin}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
RESULTS=()
FAILED=0
run() { # run NAME CMD...
  local name=$1; shift
  if ( "$@" ) >"$WORK/$name.log" 2>&1; then
    RESULTS+=("PASS  $name")
  else
    RESULTS+=("FAIL  $name")
    FAILED=1
    echo "---- $name (last 40 lines) ----" >&2
    tail -n 40 "$WORK/$name.log" >&2
  fi
}
skip() { RESULTS+=("SKIP  $1   ($2)"); }

mkvenv() { # mkvenv DIR PIP-ARGS...  (editable installs, like `pip install -e ".[test]"` in CI)
  local d=$1; shift
  if command -v uv >/dev/null; then
    [ -x "$d/bin/python" ] || uv venv -q --python 3.12 "$d" || uv venv -q "$d"
    flock "$LOCK_DIR/npm-install.lock" uv pip install -q --python "$d/bin/python" "$@"
  else
    [ -x "$d/bin/python" ] || python3 -m venv "$d"
    flock "$LOCK_DIR/npm-install.lock" "$d/bin/pip" install -q "$@"
  fi
}

# id-scan.yml
DENY_NOTE="deny terms checked"
if [ -z "${ID_SCAN_DENY:-}" ] && [ ! -f .id-scan-deny.txt ]; then DENY_NOTE="deny terms NOT checked: no ID_SCAN_DENY / .id-scan-deny.txt"; fi
run id-scan-selftest python3 scripts/test_id_scan.py
run id-scan env ID_SCAN_DENY_REQUIRED=false python3 scripts/id_scan.py
RESULTS[${#RESULTS[@]}-1]+="   ($DENY_NOTE)"

# connector-tests.yml
connector() {
  mkvenv "$VENV_ROOT/connector" -e "connector[test]"
  cd connector
  "$VENV_ROOT/connector/bin/python" -m pytest -q
  "$VENV_ROOT/connector/bin/aiteam-box" --help >/dev/null
  "$VENV_ROOT/connector/bin/aiteam-connector" --help >/dev/null
}
run connector-tests connector

# relay-tests.yml
relay() {
  mkvenv "$VENV_ROOT/relay" -e "relay[test]"
  cd relay
  "$VENV_ROOT/relay/bin/python" -m pytest -q
  "$VENV_ROOT/relay/bin/aiteam-relay" --help >/dev/null
}
run relay-tests relay

# package-check.yml
package() {
  mkvenv "$VENV_ROOT/package" pytest
  "$VENV_ROOT/package/bin/python" -m pytest -q tests/test_package.py
  PATH="$VENV_ROOT/package/bin:$PATH" python3 scripts/build_package.py --version 0.0.0-check --out "$WORK/dist"
}
run package-check package
skip release "never from this box (tag-on-main + GitHub release upload: release.yml on a v* tag)"

echo "==== box-test summary (aiteam-mcp-plugin @ $(git rev-parse --short HEAD)) ===="
printf '%s\n' "${RESULTS[@]}"
exit $FAILED
