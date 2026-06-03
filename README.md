# CLIP — Charter Community Landscape Intelligence Platform

CLIP generates charter school market-intelligence briefs for cities, one community
at a time. You give it a city (say, "Santa Fe"), and it pulls together public data
— Census income figures, NCES enrollment, state charter-policy context, local
charter activity — runs it through a scoring model, and produces a readable brief
(Markdown + HTML) summarizing the opportunity and risks for opening or supporting a
charter school there.

 instead of a
person manually researching 25+ communities, CLIP drafts a first-pass landscape
read for each one in about a minute, at a few cents of API cost. The output is a
**starting point for human judgment**, not a decision engine — the scoring weights
are still being calibrated and should not be used for real strategic decisions yet
(see Known Limitations).

This README is for a TMT staff member or a developer picking the project up — not
for someone who already knows the pipeline internals. If you just want to see it
run, jump to [Quick start](#3-quick-start-local-development).

---

## 1. What CLIP is

Two pieces:

- **The pipeline** (`main.py` + `pipeline/`) — a command-line program that does the
  actual work: fetch data, extract facts, score, write briefs. This is where all the
  intelligence lives.
- **The app** (`app/`) — a small Streamlit web UI that lets a non-technical user
  trigger a scan from a browser, watch it run, and read the resulting briefs. The app
  is deliberately thin: it knows how to *launch* the pipeline and *read its output
  files*, and nothing else.

You can use CLIP entirely from the command line without the app. The app just makes
it approachable for people who don't live in a terminal.

---

## 2. Architecture overview

The pipeline runs in **7 stages**, in order. Each stage reads the previous stage's
output and writes its own (cached on disk so re-runs are cheap):

1. **S1 Discovery** — figure out which communities exist in the target state.
2. **S2 State context** — gather state-level charter policy / authorizer context (cached per state).
3. **S3 Fact extraction** — pull per-community facts (Census, NCES, local charter activity), plus an optional gated web search for local political climate.
4. **S4 Verification** — sanity-check and validate the extracted claims.
5. **S5 Scoring** — deterministic scoring across several dimensions, weighted by the chosen strategy preset.
6. **S6 Synthesis** — write the brief narrative (and an audit pass over it).
7. **S7 Render** — write the final `.md` and `.html` brief files to `outputs/`.

**Key design principle: the app never imports the pipeline.** The only contract
between them is the command line and the filesystem. The app shells out to
`main.py` as a separate process and then reads the files the pipeline writes. This
keeps the two decoupled — you can change pipeline internals freely and the app keeps
working, and the app can't accidentally break the pipeline.

```
  Streamlit UI (app/app.py)
        │  builds CLI flags from the form
        ▼
  app/runner.py  ──launches──►  python main.py [flags]   (detached subprocess)
        │                              │
        │                              ├─ writes briefs → outputs/by_community/...
        │                              └─ writes log    → app/runs/<run_id>/stdout.log
        ▼
  app/runs.py + app/briefs.py  ──read──►  app/runs/  +  outputs/
        │
        ▼
  Brief viewer (History tab)
```

---

## 3. Session log

**Session 32 (2026-06-02) — Flask UI Map Hover & Stroke Fixes (6b106b9)**
- Added `vector-effect="non-scaling-stroke"` to all state `<path>` elements in `app/ui/static/js/app.js`; unified `stroke-width` to `1.5px` across default/hover/active CSS rules
- Replaced drop-shadow hover glow on `.state-path:hover` with a clean solid gold stroke (`2px`, `#f5c842`)
- Added `paint-order: stroke fill` + `mouseover` DOM-reorder handler to fix shared-edge stroke clipping; bumped hover stroke to `2.5px`
- Refactored hover outline to a dedicated `#hover-overlay` SVG `<path>` (appended last in SVG, above all fills); `mouseover`/`mouseout` JS handlers drive it; stripped stroke from CSS hover rule
- Fixed post-refactor regression where states could not be hovered or clicked
- Added `import re` + regex validation to `resolve_community()` in `main.py` — raises `ValueError` on malformed `community_id`
- Tests: 290 passing

**Session 31 (2026-06-02) — S6 Prompt Hardening + Railway Scaffolding**
- S6 synthesis prompt: added NUMERIC PRECISION (no rounding), FINANCIAL TERMINOLOGY (no expenditure/revenue conflation), and INFERENCE PROHIBITION (no claims beyond explicit fact bundle) guards
- S6 force-refreshed for nm-los-lunas and nm-rio-rancho; all three error categories verified absent
- `.railwayignore` added — excludes `data/`, `data/raw/`, `outputs/`, `app/runs/`, `.env` from Railway source-sync
- Tests: 290 passing

**Session 28 (2026-06-01) — Lightweight Verification Flag Resolution**
- Added `config/pec_renewal_stats.yaml` (placeholder: renewal_rate, denial_rate, median_approval_days, source, vintage — user fills to activate)
- Added `config/nmped_shortage_areas.yaml` (NM PED teacher shortage districts list; Albuquerque Public Schools prepopulated)
- Enhanced `propublica_fetcher.py`: surface 990 `tax_period` (YYYYMM) from per-EIN detail as `data_vintage_note` for confidence annotation
- Enhanced `ccd_entity_verifier.py`: filter closed schools (status 2/6) from roster unless year ≥ 2018; expose `closure_year` for S7 recency notes
- Wired S4 Pass E: deterministic static resolution — PEC renewal facts resolve when config fields non-null; teacher shortage facts resolve when district in shortage list
- Tests: 290 passing

---

## 3. Quick start (local development)

Requires **Python 3.11+** (the pipeline uses 3.10+ syntax; 3.11 is the supported target).

```bash
# 1. Clone
git clone https://github.com/TLGMustard/charter-intel.git
cd charter-intel

# 2. Install pipeline dependencies (use a 3.11+ virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
#   then edit .env and fill in the two keys (see below)
```

**Required `.env` variables:**

| Variable | Required | Where to get it |
|----------|----------|-----------------|
| `ANTHROPIC_API_KEY` | Yes (for real scans) | https://console.anthropic.com → API keys |
| `CENSUS_API_KEY` | Yes (for live Census data) | https://api.census.gov/data/key_signup.html |

`.env` is loaded automatically by `main.py` (via `python-dotenv`). It is gitignored —
never commit it.

**Verify it works with a zero-cost mock scan** (replays recorded fixtures, no API calls):

```bash
python3 main.py "Questa" --depth standard --preset maturity_adjusted --mock
```

You should see all 7 stages complete and a brief written under
`outputs/by_community/nm-questa/`.

Run the test suite (160 tests, no API calls):

```bash
python3 -m pytest tests/ -q
```

---

## 4. Running the Streamlit app locally

```bash
pip install -r app/requirements.txt      # streamlit only
export CLIP_PASSWORD="choose-a-password"  # the app refuses to start without this
streamlit run app/app.py
```

Open the URL Streamlit prints (default http://localhost:8501). Enter the password
you set in `CLIP_PASSWORD` to get past the gate. Three views:

- **New Scan** — pick a city, strategy, and (optionally) advanced flags, then Run Scan.
- **Live Run** — watches the active scan's log and stage progress.
- **History + Briefs** — past runs; select one to view/download its brief.

**Rate-limit behavior:** the New Scan view shows a status line ("This session: X/3 ·
Today: Y/20 · Est. spend: $Z / $10.00"). If a limit is hit, the Run Scan button is
disabled with the reason. **Dry-run and mock scans are always exempt** and never
count — turning on the red "Dry run" toggle re-enables the button immediately. See
[Rate limits](#9-rate-limits).

---

## 5. Deployment

The container image (`Dockerfile`) and startup script (`docker-entrypoint.sh`) are
platform-agnostic. **Railway is the production target** (always-on container,
persistent volume, GitHub auto-deploy) configured in `railway.toml`. **Streamlit
Community Cloud is the current free POC** (UI demo only — see limitations).
`render.yaml` is a complete, committed **fallback** to Render that activates with no
code changes. Full step-by-step instructions, env vars, and the platform tradeoffs
are in **[DEPLOY.md](DEPLOY.md)**.

---

## 6. Pipeline CLI reference

All flags below are read directly from `main.py`'s argparse. Usage:
`python3 main.py [COMMUNITY] [flags]`

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `COMMUNITY` (positional) | string | none | City name or community_id, e.g. `"Santa Fe"` or `nm-santa-fe` |
| `--state` | string | `NM` | Two-letter state code |
| `--community` | string | none | Specific community_id or city name (alternative to positional) |
| `--all` | flag | off | Run every community in the state |
| `--preset` | choice | `growth` | Strategy: `growth` \| `replication` \| `turnaround` \| `maturity_adjusted` |
| `--mode` | string | `2` | Output mode: `1` \| `2` \| `3`, or `zip` / `zip_v2` for ZIP-drill sub-modes |
| `--depth` | choice | `standard` | S3 web-search depth: `fast` (none) \| `standard` (political climate only) \| `deep` (all dimensions) |
| `--stages` | string | all | Comma-separated subset of stages to run, e.g. `s5,s6,s7` |
| `--force-refresh` | flag | off | Ignore cache; regenerate all stages |
| `--no-cache` | flag | off | Disable cache reads **and** writes |
| `--dry-run` | flag | off | Validate inputs and print a cost estimate; makes **no** API calls |
| `--mock` | flag | off | Replay recorded fixtures instead of calling APIs (zero cost) |
| `--record` | flag | off | Run normally and save API responses as fixtures |
| `--force-record` | flag | off | Overwrite existing fixtures when recording |
| `--batch` | flag | off | Use Anthropic Batch API (~50% cheaper; adds polling latency; incompatible with `--mock`/`--record`) |
| `--interactive` | flag | off | Prompt for state and community interactively |

---

## 7. Adding features

**Adding pipeline features** — edit `main.py` and `pipeline/` (new data sources,
scoring tweaks, new flags, etc.). Because the app builds its command from the real
CLI and only reads output files, **the app picks up new flags and outputs with no
app changes** — just expose the flag and the New Scan form's "Extra args" box can
already pass it (and you can add a dedicated control later).

**Adding UI features** — edit only files under `app/`. **Never import from
`pipeline/` or `main.py`.** If the UI needs new data, get it from the CLI (a flag) or
from files on disk — that is the entire contract. New app modules should import
sibling modules by plain name (`import config`), the way the existing ones do.

---

## 8. Project structure

```
charter-intel/
├── main.py                  # Pipeline entry point (argparse + stage orchestration)
├── requirements.txt         # Pipeline dependencies (Python 3.11+)
├── requirements_app.txt     # Community Cloud deps (streamlit only)
├── Dockerfile               # Container image (pipeline + app, Python 3.11)
├── docker-entrypoint.sh     # Wires persistent volume, then launches the app
├── railway.toml             # Railway deploy config (ACTIVE production target)
├── render.yaml              # Render deploy config (committed fallback, inactive)
├── DEPLOY.md                # Full deployment guide
├── .env.example             # Template for ANTHROPIC_API_KEY / CENSUS_API_KEY
│
├── pipeline/                # The 7-stage pipeline
│   ├── __init__.py          # PipelineConfig, STAGE_ORDER, shared types/helpers
│   ├── s1_discovery.py      # S1: enumerate communities
│   ├── s2_state_context.py  # S2: state-level charter context
│   ├── s3_fact_extraction.py# S3: per-community fact extraction + gated web search
│   ├── s4_verification.py   # S4: verify/validate claims
│   ├── s5_scoring.py        # S5: deterministic dimension scoring
│   ├── s6_synthesis.py      # S6: brief narrative + audit (dry-run guarded)
│   ├── s7_render.py         # S7: render .md / .html briefs
│   ├── zip_drill.py / zip_drill_v2.py  # ZIP-level drill sub-modes
│   ├── (s3_extract.py, s4_verify.py, s5_score.py, run_*.py — legacy/experimental)
│   └── utils/               # Data fetchers + infra
│       ├── api_client.py        # Anthropic call wrapper (call_claude)
│       ├── batch_runner.py      # Batch API gateway (--batch)
│       ├── cache.py             # On-disk cache with TTL policy
│       ├── token_logger.py      # Per-run token/cost logging
│       ├── acs_fetcher.py / saipe_fetcher.py   # Census income data
│       ├── nces_fetcher.py / population_trends_fetcher.py  # NCES enrollment
│       ├── charter_schools_fetcher.py / authorizers_fetcher.py / ped_fetcher.py
│       └── schema_validator.py  # JSON schema validation
│
├── app/                     # Streamlit wrapper (never imports pipeline)
│   ├── app.py               # UI entry: password gate + 3 views
│   ├── config.py            # Paths, interpreter, flag definitions, rate-limit constants
│   ├── runner.py            # Launches main.py as a detached subprocess; writes meta/status
│   ├── runs.py              # Scans app/runs/ for run history
│   ├── briefs.py            # Locates + loads brief files for a run
│   ├── rate_limit.py        # Filesystem-only abuse guard (session/daily/cost)
│   ├── requirements.txt     # streamlit only
│   ├── run.sh               # Local/dev launcher (binds 0.0.0.0, honors PORT)
│   ├── .streamlit/config.toml  # Theme (TMT brand) + server config
│   └── runs/demo-questa-001/   # Committed demo run record for the POC viewer
│
├── config/                  # states.yaml (per-state config, exclusions) + scoring config
├── prompts/                 # LLM prompt templates per stage
├── schemas/                 # JSON schemas for stage outputs
├── templates/               # Jinja2 brief templates (.md.j2 / .html.j2)
├── outputs/                 # Generated briefs (gitignored; nm-questa demo committed)
├── data/                    # Caches + raw source data (gitignored)
├── tests/                   # 160 pytest tests + recorded fixtures
├── scripts/                 # One-off / maintenance scripts (cache build, cache bust, etc.)
└── docs/                    # Session notes, cost baseline, data sources, annual update
```

---

## 9. Rate limits

The app has a filesystem-only abuse guard (`app/rate_limit.py`) so a shared
deployment can't burn API credits. Three layers:

- **Per-session job limit** — 3 scan jobs per browser session.
- **Daily job limit** — 20 jobs per rolling 24 hours.
- **Daily cost cap** — $10.00 of *estimated* spend per rolling 24 hours (the primary guard).

Rules:
- **A statewide `--all` scan counts as 3 jobs**; a single city counts as 1.
- **Dry-run and mock scans are always exempt** — they cost nothing, so they never
  count toward any limit and are never blocked.
- The cost estimate is a deliberately conservative **$0.30 per job-weight unit** —
  it over-estimates on purpose. It is an abuse guard, not a billing system.

**To adjust**, edit the constants in `app/config.py`:

```python
RATE_LIMIT_SESSION_MAX     = 3       # jobs per session
RATE_LIMIT_DAILY_MAX       = 20      # jobs per day
RATE_LIMIT_DAILY_COST_CAP  = 10.00   # $ per day
RATE_LIMIT_WINDOW_HOURS    = 24      # rolling window
RATE_LIMIT_STATEWIDE_WEIGHT= 3       # --all weight
RATE_LIMIT_FREE_FLAGS      = ["dry_run", "mock"]
RATE_LIMIT_COST_PER_WEIGHT = 0.30    # $ per weight unit (conservative)
```

---

## 10. Known limitations and open issues

Be honest about these before relying on CLIP for anything consequential:

- **Scoring weights are not fully calibrated.** Do **not** use scores for real
  strategic decisions yet. `maturity_adjusted` is the most-developed preset but still
  needs the operator-profile conversation (below) before it's trustworthy.
- **`facilities_feasibility` defaults to a 5** when no facility data is found — a
  known data gap, not a real signal.
- **Single-box deployment.** No horizontal scaling; the persistent volume binds to
  one instance. Fine for current NM scale; revisit for multi-state concurrency.
- **Community Cloud POC has no persistent disk** and only installs `streamlit` — so
  on that deployment the UI works but **launching a real scan won't produce briefs**
  (no pipeline deps, and it would cost API credits). It's a UI/UX demo; live
  generation needs the Railway/Docker deploy. History there shows the committed demo
  record only.
- **Cost estimates are conservative approximations, not billing data.** Both the
  dry-run estimate and the rate-limiter use heuristics; verify real cost against
  `token_logger` output after a live run.
- **Interpreter sensitivity.** Needs Python 3.11+. A machine whose `python3` is older
  (e.g. 3.9) can run the tests but not a real pipeline invocation; set `CLIP_PYTHON`
  to a 3.11+ interpreter for deployment.
- **Some `data/` source files are gitignored** (e.g. NCES raw CSVs). On a fresh
  deploy, NCES-dependent features need those files provisioned on the volume.
- **Contact information in briefs must be human-verified** before any external use.
- **Legacy/experimental files** exist in `pipeline/` (`s3_extract.py`, `s4_verify.py`,
  `s5_score.py`, `run_*.py`); the canonical stages are the seven listed in section 2.

---

## 11. Handoff notes for the next developer

**Production-ready:**
- The 7-stage pipeline runs end-to-end; 160 tests pass; mock/dry-run modes are solid.
- The Streamlit app (gate, three views, rate limiting, brief download) works locally
  and is container/Railway-ready.
- Deployment configs (Railway active, Render fallback, Dockerfile) are committed.

**Still in progress / needs a human:**
- **Scoring-weight calibration.** This is the big one. Before scores drive real
  decisions, TMT needs to have the **operator-profile conversation** — i.e. sit down
  and define what "a good charter opportunity" actually means to TMT (which
  dimensions matter, how much), then calibrate the weights to that. Until then, treat
  briefs as research drafts, not recommendations.
- Replace heuristic cost estimates with measured `token_logger` figures from a live
  statewide run.

**Where things live:**
- **Session history / decisions** are in `docs/` (cost baseline, bias resolution,
  ranking delta, data sources, annual update) — read these for *why* things are the
  way they are.
- **Standing project rules** (do-not-touch list, scoring sign-off requirements) have
  been maintained across sessions; honor them.

**Logistics:**
- The repo currently lives at `TLGMustard/charter-intel` and should be **transferred
  to The Mind Trust's GitHub organization** when ownership moves.
- **AI disclosure:** CLIP was built with substantial assistance from Claude (Anthropic)
  across ~19 working sessions. Treat the code as you would any contractor-delivered
  system — review before trusting, especially the scoring logic.

---

*The Mind Trust — Charter Community Landscape Intelligence Platform. Pre-production;
scoring uncalibrated. See `docs/` for session history and `DEPLOY.md` for deployment.*

---

## Session Log

### Session — 2026-06-03 (81af345)

**Accomplished:**
- Audited `data/raw/` (8.1 GB): deleted 4 national NCES membership CSVs (7.2 GB dead weight after parquet was built) and 2 orphaned files (`nces_lea_directory_2024.csv`, `nces_sch_characteristics_2024.csv`)
- Added dead-function note above `get_school_enrollment_trends()` in `population_trends_fetcher.py` documenting raw CSV deletion and re-download URL
- Completed Railway deploy readiness audit; fixed two blockers: removed `data/processed/` from `.dockerignore` so parquet bakes into image; added `CLIP_UI=flask` row to `DEPLOY.md` env var table
- Fixed stale healthcheck path in `DEPLOY.md` (`/_stcore/health` → `/api/health`)
- Committed and deployed S33 Flask scan wiring (`app/ui/server.py`, `app.js`, `index.html`, `main.css`) plus three new test files (337 passing)
- Diagnosed Railway cold-start failure: `data/raw/` volume empty, parquet missing from image (gitignored `data/processed/`); built `data/seeded/` directory with 6 active NM data files + parquet and added idempotent entrypoint seeding blocks
- Fixed entrypoint seeding crash: raw seeding loop iterated `data/seeded/processed/` as a state dir and tried `cp` on a directory without `-r`; added `[ -f "$f" ] || continue` guard and replaced processed seeding with direct-path parquet copy
- Added `--refresh-data` flag to `scripts/bust_cache.sh`: clears S1, S3, S4, S5, S6 cache while preserving S2 (state context — expensive Opus call); S7 has no independent cache

**Decisions:**
- Parquet seeded via `data/seeded/processed/` (committed) rather than removing `data/processed/` from `.gitignore` — avoids committing derived data to the main tree; entrypoint copies 462 KB on every container start (fast, idempotent)
- Raw NM data files seeded to volume (persistent, skip-if-exists) rather than baked directly into image layer — keeps them updatable without a full rebuild
- `--refresh-data` implemented as a named alias for `--stages s1,s3,s4,s5,s6` inside existing arg-parsing machinery — zero new code paths

**Next Steps:**
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data to activate S4 Pass E resolution
- [ ] Extend `config/nmped_shortage_areas.yaml` with full NMPED hard-to-staff district list
- [ ] Calibrate scoring weights (blocked until operator-profile conversation with The Mind Trust)
- [ ] Re-run Questa scan to rebuild cleared cache (smoke-test of `--refresh-data` cleared it)

---

### Session 34 — 2026-06-03 (5b4ceaa)

**Accomplished:**
- Added `link_persistent "${DATA_DIR}/data_logs" "${APP_DIR}/data/logs"` to `docker-entrypoint.sh` — token logs now survive container restarts on the Railway volume (same pattern as `data/raw/` and `data/cache/`)
- Added `tests/unit/test_token_logger.py` (2 tests) verifying `finalize()` writes to `data/logs/` and that stage/community appear in the CSV
- Added `_truncate_to_schema_limits()` and `_cap_str()` to `s6_synthesis.py`; called before `validate_against_schema` — caps all 10 `maxLength` fields in `brief.schema.json` at word boundaries with ellipsis, eliminating recurring schema length warnings from the statewide sweep
- Added 11 new length-cap tests to `tests/unit/test_s6_injections.py`
- Wired ZIP drill end-to-end into Flask UI: `server.py` gains `run_zip_drill_background`, `_city_name_from_slug`, `_find_zip_drill_path`, and a `mode=="zip"` branch in `/api/scan`; result endpoint is zip-aware; `index.html` adds a ZIP Version select in advanced settings; `app.js` hides depth/mode-num for ZIP mode, forces Single City scope, sends `zip_version` in POST body
- Added 10 new zip-drill route tests to `tests/unit/test_scan_api.py`; total test count 337 → 360

**Decisions:**
- Zip drill skips cost-cap rate limit (no Claude API calls) but uses the same job-polling and HTML-iframe result pattern as community scans — no JS protocol changes needed
- `_truncate_to_schema_limits()` runs pre-validation so length violations become silent caps + `"Schema length cap: …"` warnings, never validation errors that halt the pipeline
- ZIP v2 (geospatial) requires pre-downloaded ZCTA shapefile + `TRANSITLAND_API_KEY`; UI defaults to v1 with v2 selectable in advanced settings to avoid surprises on Railway
- `_city_name_from_slug("nm-santa-fe")` → `"Santa Fe"` — strips two-letter state prefix, title-cases the rest

**Next Steps:**
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data to activate S4 Pass E resolution
- [ ] Extend `config/nmped_shortage_areas.yaml` with full NMPED hard-to-staff district list
- [ ] Calibrate scoring weights (blocked until operator-profile conversation with The Mind Trust)
- [ ] Fix pre-existing `TODO: replace with server-side auth` in `app/ui/static/js/app.js:6`
- [ ] Run a live zip drill (v1) against Albuquerque to smoke-test the full UI wiring in the Flask environment
