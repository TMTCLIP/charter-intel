#!/usr/bin/env bash
# bust_cache_state.sh — bust pipeline cache for every community in a state
#
# Usage:
#   bash scripts/bust_cache_state.sh
#   bash scripts/bust_cache_state.sh --stages s3,s4,s5,s6
#   bash scripts/bust_cache_state.sh --refresh-data
#
# Defaults to --refresh-data (s1,s3,s4,s5,s6) when no flags given.
# Delegates to bust_cache.sh for each city discovered in the roster.
#
# Run from repo root.

STATE="nm"
BUST_SCRIPT="scripts/bust_cache.sh"

# ── Resolve repo root ──────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Default to --refresh-data if no args provided ─────────────────────────────
if [[ $# -eq 0 ]]; then
  PASS_ARGS="--refresh-data"
else
  PASS_ARGS="$*"
fi

# ── Read city names from community_list.json ──────────────────────────────────
COMMUNITY_LIST="data/processed/${STATE}/community_list.json"
if [[ ! -f "$COMMUNITY_LIST" ]]; then
  echo "ERROR: $COMMUNITY_LIST not found. Run S1 first (python3 main.py --all --stages s1)."
  exit 1
fi

# Extract city names from community_id (strip state prefix, hyphens → spaces, title-case)
CITIES=()
while IFS= read -r cid; do
  # nm-santa-fe → Santa Fe, nm-w-taos → W Taos, nm-los-ranchos-de-albuquerque → Los Ranchos De Albuquerque
  city="${cid#${STATE}-}"                      # strip state prefix
  city="$(echo "$city" | tr '-' ' ')"          # hyphens to spaces
  # Title-case each word
  city="$(echo "$city" | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) tolower(substr($i,2)); print}')"
  CITIES+=("$city")
done < <(python3 -c "
import json, sys
with open('$COMMUNITY_LIST') as f:
    d = json.load(f)
for c in d.get('communities', []):
    print(c['community_id'])
")

if [[ ${#CITIES[@]} -eq 0 ]]; then
  echo "No communities found in $COMMUNITY_LIST"
  exit 1
fi

echo "Busting cache for ${#CITIES[@]} communities (args: $PASS_ARGS)"
echo ""

total_deleted=0
for city in "${CITIES[@]}"; do
  output=$(bash "$BUST_SCRIPT" "$city" $PASS_ARGS 2>&1)
  if echo "$output" | grep -q "^Deleted:"; then
    echo "$city: $(echo "$output" | grep "^Deleted:" | wc -l | tr -d ' ') file(s) deleted"
    total_deleted=$((total_deleted + 1))
  else
    echo "$city: nothing cached"
  fi
done

echo ""
echo "Done. Caches cleared for $total_deleted of ${#CITIES[@]} communities."
echo "Next: python3 main.py --all --depth standard"
