# CLIP — Charter Intel Platform: Streamlit wrapper + pipeline, one container.
#
# Platform-agnostic image used by BOTH Railway (active) and Render (fallback).
# It pins Python 3.11 (the pipeline requires 3.11+) and installs BOTH the
# pipeline's requirements.txt AND app/requirements.txt into the same interpreter,
# so the app shells out to main.py with every pipeline dependency present.
#
# Nothing under app/ is modified. Persistence (app/runs, outputs, data) is wired
# at container start by docker-entrypoint.sh via symlinks onto a mounted volume
# (Railway volume or Render disk).
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential: fallback for any sdist that lacks a wheel (shapely/pyproj/
# pyogrio ship manylinux wheels with bundled GEOS/PROJ/GDAL, so no system GDAL
# is required). curl: container HEALTHCHECK. tini: a real PID 1 that reaps the
# detached main.py subprocesses the app spawns, so long-lived job-runner
# containers never accumulate zombie processes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first for build caching: only re-installs when a
# requirements file changes, not on every code push.
COPY requirements.txt /app/requirements.txt
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /app/requirements.txt -r /app/app/requirements.txt

# Application + pipeline code.
COPY . /app

# Persistence wiring + launch.
RUN chmod +x /app/docker-entrypoint.sh /app/app/run.sh

# The interpreter that runs Streamlit also runs the pipeline (all deps installed
# here), so point the app's CLIP_PYTHON at it explicitly. The platform configs
# (railway.toml / render.yaml) leave this alone; it can still be overridden.
ENV CLIP_PYTHON=/usr/local/bin/python3 \
    REPO_ROOT=/app

# Railway/Render inject PORT; default to 8501 for local `docker run`.
ENV PORT=8501
EXPOSE 8501

# Streamlit exposes a built-in health endpoint; no app change needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/_stcore/health" || exit 1

# tini is PID 1; it execs the entrypoint, which wires persistence and then
# exec's app/run.sh (the existing, unmodified launcher).
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
