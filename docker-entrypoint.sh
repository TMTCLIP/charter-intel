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

echo "[entrypoint] persistence wired under ${DATA_DIR}; launching Streamlit on port ${PORT:-8501}"

# Hand off to the existing, unmodified launcher (binds 0.0.0.0, honors PORT).
exec bash "${APP_DIR}/app/run.sh"
