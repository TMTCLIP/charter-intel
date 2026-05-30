# CLIP Deployment

**Active platform: Railway** (Docker, always-on, persistent volume, GitHub auto-deploy).
**Fallback: Render** — a complete, inactive Blueprint (`render.yaml`) ready to switch to.

Both platforms use the **same** `Dockerfile` and `docker-entrypoint.sh`, so switching
is a dashboard action, not a rebuild.

---

## Railway vs Render — decision

| Requirement | Railway | Render |
|---|---|---|
| **1. Always-on (no sleep)** | ✅ Hobby/Pro don't sleep; just never enable "App Sleeping" | ✅ Paid instances (Starter+) never sleep; free tier sleeps so it's unusable |
| **2. Persistent disk** | ✅ Volume survives redeploys/restarts; **single-replica only** | ✅ Disk survives redeploys/restarts |
| **3. GitHub auto-deploy** | ✅ Push to `main` → build → deploy | ✅ Blueprint auto-deploys on push |
| **4. Docker / Py3.11 + GDAL** | ✅ `builder = "dockerfile"` | ✅ `runtime: docker` |
| **5. Single-maintainer ops** | ✅ Great CLI/DX, managed TLS | ✅ Managed TLS, fuller infra-as-code |

**Concrete pricing at current scale (1–5 scans/day, ~1 GB data):**
- **Railway:** Hobby plan **$5/mo** includes $5 of usage. An idle Streamlit container
  (≈0.2 GB RAM, ~0 CPU between scans) burns far less than $5/mo of compute, so compute
  is effectively covered by the membership. Volume storage ≈ **$0.25–0.50/mo** (~$0.25/GB).
  **Net ≈ $5–6/mo.** Billing is usage-based, so idle is cheap.
- **Render:** always-on needs a paid instance. **Starter $7/mo** (0.5 CPU / 512 MB — tight
  for batch/geopandas) or **Standard $25/mo** (1 CPU / 2 GB — comfortable). Disk **$0.25/GB/mo**.
  **Net ≈ $7.25/mo (risky RAM) to $25.25/mo (safe).** Flat, predictable.

**Railway-specific gotchas (designed around):**
- **Volume = single replica.** A Railway volume attaches to one instance only, so
  `numReplicas = 1` in `railway.toml`. Fine for this single-box, subprocess-on-disk design.
- **App Sleeping kills runs.** Sleeping would terminate an in-flight detached `main.py`.
  It's off by default; `railway.toml` documents *do not enable it*.
- **PID 1 / zombies.** The app spawns detached subprocesses; we run **`tini`** as PID 1
  in the Dockerfile to reap them so long-lived containers don't accumulate zombies.
- **SIGTERM on redeploy.** A scan running at deploy time is killed mid-run (true on any
  PaaS). Acceptable; the volume keeps all prior history/briefs.
- **Build:** Dockerfile build is wheel-only (no GDAL compilation) → fast, within limits.

**Render advantages Railway can't match at any price:**
- Fuller declarative infra-as-code: `render.yaml` defines the **disk** too, so the whole
  service (incl. storage) is reproducible from the repo. Railway volumes are
  dashboard/CLI-managed, not in `railway.toml`.
- **Flat, predictable billing** (no usage surprises) — valuable if ops is handed to a
  non-technical owner.

### Recommendation: **Railway first.**
It's cheaper at current/idle scale (~$5–6/mo vs Render's $7–25/mo floor), faster to set
up, and meets all five requirements. Render stays committed as a one-click fallback.

### Switch to Render when *either* trips (whichever comes first):
1. **Cost:** Railway's usage-based bill exceeds **~$25/mo for two consecutive months**
   (at that point Render Standard's flat $25 is cheaper *and* predictable), **or**
2. **Capacity/predictability:** you outgrow one always-on instance — need guaranteed
   RAM/CPU headroom for heavy concurrent batch scans, or want flat billing to hand ops off.

(The single-replica volume constraint is identical on both, so it is *not* a switch trigger.)

---

## One-time Railway setup

Prereqs: a GitHub repo for this project, a Railway account, and the Railway CLI
(`npm i -g @railway/cli`), or do the equivalent in the dashboard.

```bash
# 0. Make sure the deploy files are committed and pushed to GitHub.
git add Dockerfile railway.toml render.yaml .dockerignore docker-entrypoint.sh DEPLOY.md
git commit -m "Add Railway deploy (Render fallback)"
git push origin main

# 1. Log in and create the project, then link this repo.
railway login
railway init                      # create a new project (or: railway link <projectId>)

# 2. Connect the GitHub repo so pushes auto-deploy:
#    Railway dashboard → project → service → Settings → Source →
#    "Connect Repo" → pick this repo, branch = main. (Railway reads railway.toml.)

# 3. Add the PERSISTENT VOLUME mounted at /data:
#    Dashboard → service → Variables/Settings → "+ Volume" → Mount path: /data
#    (CLI equivalent:)
railway volume add --mount-path /data
#    The entrypoint auto-detects RAILWAY_VOLUME_MOUNT_PATH and symlinks
#    app/runs/, outputs/, data/cache/, data/raw/ onto it.

# 4. Set environment variables / secrets (see table below):
railway variables --set CLIP_PASSWORD=choose-a-strong-password
railway variables --set ANTHROPIC_API_KEY=sk-ant-...
railway variables --set CENSUS_API_KEY=...
#    REPO_ROOT and CLIP_PYTHON are baked into the image; PORT is injected by Railway.

# 5. Trigger the first deploy (or just push):
railway up        # or: git push origin main

# 6. Generate a public URL:
#    Dashboard → service → Settings → Networking → "Generate Domain".
```

Open the generated URL → password gate (CLIP_PASSWORD) → New Scan / Live Run / History.

---

## Every subsequent update

```bash
git push origin main
```

That's it. Railway detects the push, rebuilds the Docker image, runs the health check
on `/_stcore/health`, and rolls out. The volume persists `app/runs/` and `outputs/`, so
run history and briefs survive the deploy. No manual steps.

---

## Environment variables (set in Railway dashboard)

| Variable | Where it's set | Purpose |
|---|---|---|
| `CLIP_PASSWORD` | **Railway (secret)** | Password gate. App refuses to start if unset. |
| `ANTHROPIC_API_KEY` | **Railway (secret)** | Pipeline LLM calls. |
| `CENSUS_API_KEY` | **Railway (secret)** | Census SAIPE/ACS fetches. |
| `PORT` | Auto (Railway) | Injected; `app/run.sh` already reads it. |
| `RAILWAY_VOLUME_MOUNT_PATH` | Auto (Railway) | Injected when a volume is attached; entrypoint uses it. |
| `CLIP_PYTHON` | Baked in image (`/usr/local/bin/python3`) | Interpreter that runs `main.py`. |
| `REPO_ROOT` | Baked in image (`/app`) | Pipeline working root. |
| `DATA_DIR` | Optional override | Force the volume path (else `RAILWAY_VOLUME_MOUNT_PATH` → `/data`). |

---

## Activating the Render fallback

`render.yaml` is complete and deployable but **inert until connected**. To switch:

1. **Render Dashboard → New → Blueprint →** select this repo (it reads `render.yaml`,
   which already declares the Docker service + 5 GB disk at `/data` + env vars).
2. Set the three secrets (`CLIP_PASSWORD`, `ANTHROPIC_API_KEY`, `CENSUS_API_KEY`) in the
   Render dashboard (they're `sync:false`).
3. (Optional) Migrate data: copy the Railway volume's `runs/` and `outputs/` to the
   Render disk, or start fresh.
4. Pause/delete the Railway service to stop double-billing.

No code changes — same image, same entrypoint. From then on `git push` auto-deploys on
Render. (If you prefer the switch be literally a repo change, you can instead delete/rename
`railway.toml` so only `render.yaml` remains; but Railway and Render configs coexist
harmlessly since each platform only reads its own file.)

---

## Local development is unchanged

```bash
CLIP_PASSWORD=dev streamlit run app/app.py
```

No volume, no Docker required. The entrypoint/symlink logic only runs inside the container.

---

## Data note (NCES source CSVs)

`data/raw/` and `data/cache/` are gitignored and excluded from the image, so the container
starts without NCES source data. The pipeline auto-builds the NCES parquet cache on first
use **if** source CSVs are present. To enable that path in production, drop the NCES source
files onto the volume at `/data/data_raw/` (symlinked to `data/raw/`); the parquet cache
then persists under `/data/data_cache/`. Briefs that don't need population-trend data work
without this.
