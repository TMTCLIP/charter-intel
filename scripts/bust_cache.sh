#!/usr/bin/env bash
# bust_cache.sh — clear pipeline cache for a single city
#
# Usage: bash scripts/bust_cache.sh "Santa Fe"
#        bash scripts/bust_cache.sh "Carlsbad"
#
# Run from repo root.

STATE="nm"  # change here when adding a second state

# ── Input validation ──────────────────────────────────────────────────────────
if [[ -z "${1-}" ]]; then
  echo "Usage: bash scripts/bust_cache.sh \"City Name\""
  exit 1
fi

# ── Slug derivation: lowercase + spaces-to-hyphens + prepend state ────────────
SLUG="${STATE}-$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"

# ── Resolve repo root so script works from any working directory ──────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMMUNITY_DIR="data/cache/community/${STATE}/${SLUG}"
SYNTHESIS_DIR="data/cache/synthesis/${STATE}/${SLUG}"

deleted=0

if [[ -d "$COMMUNITY_DIR" ]]; then
  rm -rf "$COMMUNITY_DIR"
  echo "Deleted: $COMMUNITY_DIR"
  deleted=1
fi

if [[ -d "$SYNTHESIS_DIR" ]]; then
  rm -rf "$SYNTHESIS_DIR"
  echo "Deleted: $SYNTHESIS_DIR"
  deleted=1
fi

if [[ $deleted -eq 0 ]]; then
  echo "Nothing found for slug: $SLUG"
  echo "  Checked: $COMMUNITY_DIR"
  echo "           $SYNTHESIS_DIR"
fi
