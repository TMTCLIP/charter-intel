#!/usr/bin/env bash
# Deployment entrypoint for the CLIP Streamlit wrapper.
# Binds 0.0.0.0 and honors $PORT (default 8501). Run from the repo root.
#
#   ./app/run.sh
#
# Local development can instead use:  streamlit run app/app.py
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec streamlit run "${HERE}/app.py" \
  --server.address=0.0.0.0 \
  --server.port="${PORT:-8501}"
