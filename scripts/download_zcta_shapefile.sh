#!/usr/bin/env bash
# scripts/download_zcta_shapefile.sh
# Download Census TIGER/Line 2023 national ZCTA shapefile.
# Required by pipeline/zip_drill_v2.py (--mode zip_v2).
#
# Usage: bash scripts/download_zcta_shapefile.sh
#
# Output: data/raw/national/tl_2023_us_zcta520/tl_2023_us_zcta520.shp
# Size:   ~68 MB compressed / ~250 MB unzipped

set -euo pipefail

DEST_DIR="data/raw/national/tl_2023_us_zcta520"
ZIP_URL="https://www2.census.gov/geo/tiger/TIGER2023/ZCTA520/tl_2023_us_zcta520.zip"
TMP_ZIP="/tmp/tl_2023_us_zcta520.zip"
SHP_FILE="${DEST_DIR}/tl_2023_us_zcta520.shp"

# Run from repo root
cd "$(dirname "$0")/.."

if [ -f "$SHP_FILE" ]; then
  echo "Shapefile already present: $SHP_FILE"
  echo "Delete it first if you want to re-download."
  exit 0
fi

echo "Creating destination directory: $DEST_DIR"
mkdir -p "$DEST_DIR"

echo "Downloading TIGER/Line 2023 ZCTA shapefile (~68 MB)..."
curl -L --fail --retry 3 --retry-delay 5 --progress-bar -o "$TMP_ZIP" "$ZIP_URL"

echo "Verifying zip integrity..."
unzip -t "$TMP_ZIP" || { echo "ERROR: zip file is corrupt or incomplete"; exit 1; }

echo "Unzipping to $DEST_DIR ..."
unzip -o "$TMP_ZIP" -d "$DEST_DIR"

echo "Removing zip archive..."
rm -f "$TMP_ZIP"

echo ""
echo "Done. Files:"
ls -lh "$DEST_DIR"
