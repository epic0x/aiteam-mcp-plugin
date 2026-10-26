#!/usr/bin/env bash
# Release guard: tags ship only from main. Exits 1 unless the commit the tag points at is on origin/main.
# Usage: scripts/tag_on_main.sh <tag> [main-ref]   (main-ref default: origin/main; the caller fetches it first)
set -euo pipefail
tag="${1:?usage: tag_on_main.sh <tag> [main-ref]}"
main="${2:-origin/main}"
sha="$(git rev-parse --verify --quiet "refs/tags/${tag}^{commit}")" || { echo "::error::tag '${tag}' does not exist"; exit 1; }
git rev-parse --verify --quiet "${main}^{commit}" >/dev/null || { echo "::error::'${main}' not found: fetch main first"; exit 1; }
if git merge-base --is-ancestor "$sha" "$main"; then
  echo "tag ${tag} (${sha}) is on ${main}: ok"
else
  echo "::error::tag ${tag} points at ${sha}, which is not on ${main}. Tags ship only from main: tag a main commit."
  exit 1
fi
