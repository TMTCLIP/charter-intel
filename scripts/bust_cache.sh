#!/usr/bin/env bash
# bust_cache.sh — clear pipeline cache for a single city
#
# Usage:
#   bash scripts/bust_cache.sh "Santa Fe"
#   bash scripts/bust_cache.sh "Carlsbad" --stages s3,s4
#   bash scripts/bust_cache.sh "Questa"   --stages s6
#
# Without --stages: deletes the entire community and synthesis cache directories
# (original full-bust behaviour, unchanged).
#
# With --stages <list>: deletes only the cache files for the named stages.
#   Recognised stage names and their cache locations:
#     s3  → data/cache/community/{STATE}/{SLUG}/s3_facts_raw.json
#     s4  → data/cache/community/{STATE}/{SLUG}/s4_verified.json
#     s5  → data/cache/community/{STATE}/{SLUG}/s5_scorecard.json
#     s6  → data/cache/synthesis/{STATE}/{SLUG}/s6_*.json  (all synthesis files)
#     s1  → data/cache/state/{STATE}/s1_community_list_*.json
#     s2  → data/cache/state/{STATE}/s2_state_context.json
#
# Run from repo root.

STATE="nm"  # change here when adding a second state

# ── Input validation ──────────────────────────────────────────────────────────
if [[ -z "${1-}" ]] || [[ "$1" == --* ]]; then
  echo "Usage: bash scripts/bust_cache.sh \"City Name\" [--stages s3,s4,...]"
  exit 1
fi

CITY="$1"
shift   # consume city name; remaining args may include --stages

STAGES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stages)
      shift
      STAGES="${1-}"
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: bash scripts/bust_cache.sh \"City Name\" [--stages s3,s4,...]"
      exit 1
      ;;
  esac
done

# ── Slug derivation: lowercase + spaces-to-hyphens + prepend state ────────────
SLUG="${STATE}-$(echo "$CITY" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"

# ── Resolve repo root so script works from any working directory ──────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMMUNITY_DIR="data/cache/community/${STATE}/${SLUG}"
SYNTHESIS_DIR="data/cache/synthesis/${STATE}/${SLUG}"
STATE_DIR="data/cache/state/${STATE}"

deleted=0

# ── Full bust (default: no --stages provided) ─────────────────────────────────
if [[ -z "$STAGES" ]]; then
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
  exit 0
fi

# ── Stage-selective bust ───────────────────────────────────────────────────────
IFS=',' read -ra STAGE_LIST <<< "$STAGES"
for stage in "${STAGE_LIST[@]}"; do
  stage="${stage// /}"   # strip any accidental whitespace
  case "$stage" in
    s1)
      target="${STATE_DIR}/s1_community_list_*.json"
      files=( $target )
      if [[ ${#files[@]} -gt 0 && -e "${files[0]}" ]]; then
        rm -f $target
        echo "Deleted: $target"
        deleted=1
      fi
      ;;
    s2)
      target="${STATE_DIR}/s2_state_context.json"
      if [[ -f "$target" ]]; then
        rm -f "$target"
        echo "Deleted: $target"
        deleted=1
      fi
      ;;
    s3)
      target="${COMMUNITY_DIR}/s3_facts_raw.json"
      if [[ -f "$target" ]]; then
        rm -f "$target"
        echo "Deleted: $target"
        deleted=1
      fi
      ;;
    s4)
      target="${COMMUNITY_DIR}/s4_verified.json"
      if [[ -f "$target" ]]; then
        rm -f "$target"
        echo "Deleted: $target"
        deleted=1
      fi
      ;;
    s5)
      target="${COMMUNITY_DIR}/s5_scorecard.json"
      if [[ -f "$target" ]]; then
        rm -f "$target"
        echo "Deleted: $target"
        deleted=1
      fi
      ;;
    s6)
      # All synthesis outputs for this community (scan + brief modes)
      files=( "${SYNTHESIS_DIR}"/s6_*.json )
      if [[ ${#files[@]} -gt 0 && -e "${files[0]}" ]]; then
        rm -f "${SYNTHESIS_DIR}"/s6_*.json
        echo "Deleted: ${SYNTHESIS_DIR}/s6_*.json"
        deleted=1
      fi
      ;;
    *)
      echo "Unknown stage: $stage (valid: s1,s2,s3,s4,s5,s6)"
      exit 1
      ;;
  esac
done

if [[ $deleted -eq 0 ]]; then
  echo "Nothing found for slug: $SLUG (stages: $STAGES)"
fi
