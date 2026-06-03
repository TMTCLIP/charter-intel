#!/usr/bin/env bash
# CLIP container entrypoint.
#
# Wires the app's filesystem state onto a persistent volume WITHOUT editing any
# app/ file. The app hardcodes app/runs/ under app/ and outputs/ under REPO_ROOT;
# rather than change that, we symlink those directories (plus the pipeline's
# data caches) onto the volume mounted at $DATA_DIR.
#
# Mount-path resolution (first set wins):
#   1. DATA_DIR                    — explicit override
#   2. RAILWAY_VOLUME_MOUNT_PATH   — injected automatically by Railway
#   3. /data                       — Render disk mountPath / local default
# On a platform with no volume (e.g. plain `docker run`), this still works — the
# data just won't survive a container replacement, which is fine for local dev.
set -euo pipefail

DATA_DIR="${DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-/data}}"
APP_DIR="/app"

# Directories that must persist across deploys/restarts.
#   app/runs   — run metadata, status.json, stdout.log (History view)
#   outputs    — generated .md/.html briefs
#   data/cache — pipeline cache (NCES parquet, ACS/SAIPE/S2 caches)
#   data/raw   — large source data (NCES CSVs); drop files here on the disk
link_persistent() {
  local target="$1"      # path on the persistent disk
  local linkpath="$2"    # path the app/pipeline expects inside /app
  mkdir -p "$target"
  # If a real (non-symlink) dir already exists from the image, migrate its
  # contents onto the disk once, then replace it with a symlink.
  if [ -e "$linkpath" ] && [ ! -L "$linkpath" ]; then
    ( cd "$linkpath" && cp -Rn . "$target/" ) 2>/dev/null || true
    rm -rf "$linkpath"
  fi
  mkdir -p "$(dirname "$linkpath")"
  ln -sfn "$target" "$linkpath"
}

link_persistent "${DATA_DIR}/runs"       "${APP_DIR}/app/runs"
link_persistent "${DATA_DIR}/outputs"    "${APP_DIR}/outputs"
link_persistent "${DATA_DIR}/data_cache" "${APP_DIR}/data/cache"
link_persistent "${DATA_DIR}/data_raw"   "${APP_DIR}/data/raw"

# Seed static NM data files from the image to the volume on first deploy.
# Idempotent: skips any file already present on the volume (never overwrites).
SEEDED_SRC="${APP_DIR}/data/seeded"
SEEDED_DST="${DATA_DIR}/data_raw"
if [ -d "$SEEDED_SRC" ]; then
  echo "[entrypoint] seeding static data files to volume..."
  for state_dir in "$SEEDED_SRC"/*/; do
    state=$(basename "$state_dir")
    mkdir -p "${SEEDED_DST}/${state}"
    for f in "$state_dir"*; do
      [ -f "$f" ] || continue           # skip subdirectories (e.g. processed/)
      fname=$(basename "$f")
      dst_file="${SEEDED_DST}/${state}/${fname}"
      if [ ! -f "$dst_file" ]; then
        cp "$f" "$dst_file"
        echo "[entrypoint] seeded: ${state}/${fname}"
      fi
    done
  done
fi

# Seed derived files (parquet cache etc.) to container-local paths.
# data/processed/ is not on the volume, so this re-seeds on every container
# start from the image copy — fast (462 KB) and idempotent.
mkdir -p "${APP_DIR}/data/processed/nm"
_pq_src="${APP_DIR}/data/seeded/processed/nm/nces_membership_nm.parquet"
_pq_dst="${APP_DIR}/data/processed/nm/nces_membership_nm.parquet"
if [ ! -f "$_pq_dst" ]; then
  cp "$_pq_src" "$_pq_dst"
  echo "[entrypoint] seeded processed: nm/nces_membership_nm.parquet"
fi

if [ "${CLIP_UI}" = "flask" ]; then
    echo "[entrypoint] persistence wired under ${DATA_DIR}; launching Flask UI on port ${PORT:-8080}"
    exec bash "${APP_DIR}/app/ui/run.sh"
else
    echo "[entrypoint] persistence wired under ${DATA_DIR}; launching Streamlit on port ${PORT:-8501}"
    exec bash "${APP_DIR}/app/run.sh"
fi
