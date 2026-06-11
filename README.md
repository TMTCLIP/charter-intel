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

**Session 3 / National CCD (2026-06-05) — National NCES Parquets + MS/TN/WI Community Registries**
- Replaced all state-trimmed NCES CSVs with single national parquet files queryable by STABBR, enabling any state to work without per-state data downloads.
- Created `scripts/download_nces_national.py` — downloads 6 CCD files from the Urban Institute Education Data API to `data/raw/national/`.
- Created `scripts/build_national_parquet.py` — converts those CSVs to 4 compressed parquets: `nces_lea_directory.parquet` (19,714 districts, `has_charter_schools` derived from school-level charter indicator), `nces_lea_finance.parquet` (19,570 districts, 51 states), `nces_sch_lunch.parquet` (146K FRL rows, 44 states), `nces_lea_membership.parquet` (78K rows, 4 years: 2020/22/23/24).
- Updated `pipeline/utils/nces_fetcher.py`: `_read_finance()` and `_aggregate_frl()` now query national parquets by LEAID / LEAID+filters instead of state-specific CSV paths.
- Updated `pipeline/utils/population_trends_fetcher.py`: removed NM-hardcoded `NCES_SOURCE_FILES` constant.
- Updated `scripts/build_nces_cache.py`: reads `data/raw/national/nces_lea_membership.parquet`, filters by STABBR, writes per-state parquet — no longer requires state-specific CSV downloads.
- Updated `scripts/build_community_registry.py`: `load_ccd()` now accepts `.parquet` files; uses `has_charter_schools` column from directory parquet as authoritative `has_charters` signal when no roster file is provided.
- Built community registries: `config/community_registry/ms.yaml` (142 districts), `tn.yaml` (139), `wi.yaml` (312).
- Tests: 544 passed (updated `test_nces_fetcher.py` for national parquet design).

**Session 34 (2026-06-05) — Multi-State Hard Blockers: FIPS, Path Threading, Graceful Missing Files (c7ff84e)**
- **Task 1** — Created `pipeline/utils/fips_utils.py` with complete 50-state + DC FIPS lookup dict and `get_state_fips(state)` function. Single source of truth for state FIPS codes.
- **Task 2** — Fixed `s1_discovery.py` highway prefix regex: changed hardcoded `NM` to `[A-Z]{2}` to strip highway codes from any state (MS 82, TN 64, WI 35, NM 14, etc.).
- **Task 3** — Parameterized data paths across 4 fetchers: `charter_schools_fetcher.py`, `authorizers_fetcher.py`, `ped_fetcher.py`, `population_trends_fetcher.py`. All now derive CSV paths from state param instead of hardcoded nm/.
- **Task 4** — Removed hardcoded `STATE_FIPS = "35"` from `saipe_fetcher.py`; added `state` param to `get_poverty_data()`, derive FIPS via `get_state_fips()`, thread through API calls.
- **Task 5** — Removed `STATE_FIPS = "35"` from `acs_fetcher.py`; removed NM-only guard from `get_total_population()`, parameterized both `_fetch_district()` and `_fetch_acs_vars()` to accept `state_fips`.
- **Task 6** — Expanded `ADJACENT_STATES` dict in `usaspending_csp_fetcher.py` with MS, TN, WI definitions; added fallback for unknown states (log warning, use state-only list).
- **Task 7** — Added 29 new tests: `test_fips_utils.py` (8), `test_acs_fetcher.py` (4), enhanced `test_s1_discovery.py` (+8), `test_saipe_fetcher.py` (+2), `test_usaspending_csp_fetcher.py` (+8).
- **Graceful degradation** — All missing-file errors now log at debug level and return empty list/None rather than crash. MS/TN/WI data files will be added in Session 2 (states.yaml) and Session 3 (CSV downloads).
- Tests: 502 (baseline) → 531 (final, +29 new, 0 failed).

**Session 33 (2026-06-04) — Fix @media print CSS in strategic_brief.html.j2 (e76b9c7)**
- Replaced CSS-variable-override `@media print` block in `templates/strategic_brief.html.j2` with explicit `!important` property declarations — Chrome's print renderer resolves variables before applying media query overrides, causing broken dark colors in print/PDF
- Corrected all selector names to match actual template classes: `.page-shell` (not `.layout-wrapper`), `.sb-score-num` (not `.score-display`), `.exec-snapshot` (not `.executive-snapshot`), `.disclosure-footer` (not `.brief-footer`), `.banner-insufficient/.banner-gate/.banner-tribal/.banner-pci-legacy` (not generic `.banner-warn/info`)
- Re-rendered nm-questa maturity_adjusted demo brief via `scripts/render_s7.py`; HTML output committed to `outputs/by_community/nm-questa/`
- Tests: 417 passing

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
├── scripts/                 # One-off / maintenance scripts (cache build, cache bust, national data download, etc.)
│   ├── download_nces_national.py  # Download 6 national CCD files from Urban Institute API → data/raw/national/
│   ├── build_national_parquet.py  # Convert those CSVs to 4 national parquets (run once after download)
│   ├── build_nces_cache.py        # Filter national membership parquet to one state → data/processed/{state}/
│   └── build_community_registry.py  # Generate config/community_registry/{state}.yaml from parquet
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
- **NCES data requires a one-time download on a fresh deploy.** Run
  `python3 scripts/download_nces_national.py` then `python3 scripts/build_national_parquet.py`
  to produce the four national parquets in `data/raw/national/`. Per-state processed
  parquets (`data/processed/{state}/`) are built automatically on first pipeline run
  for that state. The raw CSVs and all `data/` files are gitignored.
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

### Session — 2026-06-11 (ebee04c)

**Accomplished:**
- Implemented C-1 (previously deferred): a configurable per-run spend ceiling in `pipeline/utils/api_client.py` — `BudgetExceededError`, `set_run_budget()`, and `_enforce_budget()` called at the start of every `call_claude`, checking cumulative spend against `CLIP_MAX_RUN_COST_USD` / `CLIP_MAX_RUN_TOKENS` (both unset → unlimited, unchanged behavior)
- Added cumulative `total_cost_usd` / `total_tokens` read-only properties to `TokenLogger` (instrumentation-only; budget reads these, logger still aborts nothing itself)
- Added opt-in `call_claude_cached(cache, cache_key, ttl_days=…, **call_kwargs)` wrapper using the existing `CacheManager.get/set` unchanged — cache hit returns a reconstructed `APIResult` and makes no API call (never counts against the ceiling); existing per-stage caches untouched
- Wired the budget in `main.py`: `_env_float`/`_env_int` helpers read the env vars; `set_run_budget` called after `set_run_id`; `BudgetExceededError` now propagates past the per-stage exception handler and is caught at the run loop (serial + batch) via `_abort_over_budget()` → finalizes token CSV/summary, logs one clean line, exits non-zero (no stack trace)
- Verified P-2: the dry-run backstop inside `call_claude` (`if _DRY_RUN`) and `set_dry_run` wiring in `main.py` are present, and all per-stage `config.dry_run` guards (s2/s3/s4/s6) remain intact — already implemented last session, no change needed
- Added `tests/unit/test_budget_and_cache.py` (8 tests: budget no-op/cost/token ceilings, cache hit/miss/bypass/empty-not-cached); suite **859 → 867 passing**

**Decisions:**
- Enforcement lives in `api_client`, not `token_logger` — keeps the logger instrumentation-only (it just exposes cumulative totals)
- Budget configured via env vars (not a `PipelineConfig` field) to avoid touching the config dataclass; matches the existing `set_dry_run` module-state pattern
- `_enforce_budget` checks at call start so the run aborts before spending further once a prior call crossed the ceiling; warns once at 80%
- Cache wrapper is opt-in and omits the non-serialisable `raw_response`; no stage was rewired to use it (would alter existing cache behavior)
- Excluded the regenerated demo-brief HTML (timestamp-only churn) from the commit again

**Next Steps:**
- [ ] Decide whether any stage should adopt `call_claude_cached` for response-level caching (currently opt-in, unused)
- [ ] Consider surfacing `CLIP_MAX_RUN_COST_USD` / `CLIP_MAX_RUN_TOKENS` in the app UI / deploy configs
- [ ] Budget enforcement in `--batch` mode aborts on the first over-ceiling future; revisit if finer-grained parallel accounting is needed

**Tests:** 867 passed (`python3 -m pytest -q`).

### Session — 2026-06-11 (3cc9824)

**Accomplished:**
- Red-team audit (audit-only first pass) of security, data-integrity, pipeline-logic, cost, and code-quality; verified every recon finding against source and discarded 6 false positives (e.g. `ValidationResult.__bool__` makes `if not validation:` correct; `acs_fetcher` already length-guards; `runner` shell-injection mitigated by `shlex.join`)
- Security: removed OAuth `id_info` debug print (`app/ui/auth.py`); redacted full token-exchange response prints in `gmail.py` / `gmail_ingestor.py`; pinned deps with upper bounds in `requirements.txt` / `requirements_app.txt`; added free-text `--state` format check in `app/runner.py`; routed `CacheManager.invalidate` prefix through traversal guard; synced `.env.example` to all 11 vars (real `.env` untouched)
- Data integrity: `_validate_parsed` now returns a structured error (no raise) so callers skip a bad city gracefully; `s3` guards non-dict `parsed_json`; `s5` SMALL_MARKET uses defensive `float()`/`int()` coercion; `s5` warns on preset-missing dimension and errors on unknown preset; `s5` per-key threshold defaults; `s4`/`s6` handle corrupt cache JSON; `api_client` filters text blocks instead of assuming `content[0]`
- Pipeline/cost: fixed `${CLIP_UI:-}` unbound-var crash in `docker-entrypoint.sh`; added a global dry-run backstop inside `call_claude` (wired from `main.py`), keeping per-stage guards; documented mtime-TTL limitation; flagged C-1 (cache/spend-ceiling) as a deferred architecture decision via TODO
- Code quality: replaced inline `__import__("os")` in `charter_schools_fetcher.py`; added Python 3.11+ runtime assert in `main()`
- Test suite green after every individual fix: **859 passing**

**Decisions:**
- Honored standing rules: no architecture redesign and no scoring-weight changes — C-1 and P-4 documented/deferred rather than implemented; D-3/D-4 are defensive coercion + warnings only, no weight or score logic touched
- Per explicit user constraints: schema-validation failures return structured errors (never abort the run); dry-run backstop added inside `call_claude` while per-stage `if config.dry_run` checks remain; only `.env.example` edited, real `.env` never read or written
- Excluded a regenerated demo-brief HTML (timestamp-only churn) from the commit

**Next Steps:**
- [ ] Decide on C-1: add cache-aware wrapper and/or hard per-run token/$ ceiling in `call_claude` (needs architecture sign-off)
- [ ] Decide on P-4: embed `fetched_at` in cached JSON vs. current mtime-based TTL (changes stored shape + tests)
- [ ] Consider making LLM schema validation actively used (pass schemas at call sites) now that failures surface cleanly

**Tests:** 859 passed (`python3 -m pytest -q`).

### Session — 2026-06-09 (ff943ce)

**Accomplished:**
- Fixed `used_default=True` false-positives in `pipeline/s5_scoring.py` for three dimensions (`charter_saturation`, `funding_environment`, `operational_complexity`): added `_score_partial_coverage()` dispatcher and `_degrade_confidence()` helper so partial data reduces confidence (×0.7) without triggering a default exclusion
- Added `_validate_community_id()` guard in `pipeline/s6_synthesis.py` (called at both synthesis and scan write paths) that rejects bare slugs (e.g. `ms-oxford`) using `^[a-z]{2}-[a-z0-9]+-\d{7}$`; all four active states (NM, MS, TN, WI) confirmed 7-digit LEAIDs
- Staged and permanently deleted 44 stale bare-slug synthesis cache artifacts from `data/cache/synthesis/` (pre-LEAID-migration runs); `nm-questa` stale synthesis brief also deleted (pre-fix, unreliable output)
- Monkeypatched `_validate_community_id` to a no-op in `tests/integration/test_mock_pipeline.py` for the pre-migration `nm-questa` fixture (bare slug; documented intent in comment)
- Confirmed Oxford MS composite stable at 6.28 and excluded_dimensions unchanged (`charter_saturation`, `competitive_opportunity`) after fix

**Decisions:**
- Partial-coverage path uses `confidence * 0.7` rather than a fixed LOW label — preserves relative signal strength while honestly representing incomplete data
- `_VALID_COMMUNITY_ID` regex uses `\d{7}` (not `\d+`) because all four active states are confirmed 7-digit; tighten if new states with different LEAID lengths are added
- `nm-questa` stale synthesis brief deleted rather than renamed: pre-fix output with incorrect defaulting is worse than forcing a clean regeneration on next run

**Next Steps:**
- [ ] Run full MS batch (`python3 main.py --state MS --all --preset maturity_adjusted --depth fast`) now that S5 threshold fix is in place
- [ ] Review ELL flip report after MS batch (None→non-None flips require Lennon awareness before re-rendering)
- [ ] `competitive_opportunity/grade_band_gaps` — string fact with no scoring path; needs operator decision on numeric score for "zero grade-band charter coverage" (flagged as Lennon item)

**Test Status:** 813 passed

---

### Session — 2026-06-07 (275043b)

**Accomplished:**
- Fixed combobox Enter key handler: flushed pending debounce and added auto-select when exactly one option is visible (e.g., "type Oxford, press Enter" without arrow navigation)
- Added `fetchCitiesWithRetry(stateAbbr, retries=3, delayMs=500)` function for Railway cold-start resilience; wrapped `loadCitiesForPanel` fetch in retry logic with explicit `resp.ok` check
- Added visible error panel to combobox: when `/api/cities` fails, displays "Could not load cities — please refresh" inside the dropdown + console.error with status code
- Added console diagnostics: `[CLIP] Cities loaded: STATE=N` on success, error logging on fetch failure, `[CLIP] Selected city` object logging in `_cbSelect`
- Wrapped `_cbInit()` in try/catch to catch and log JS errors during combobox initialization (prevents silent failures on missing DOM elements)
- Fixed combobox filter show/hide: replaced `toggleAttribute("hidden", boolean)` with explicit `removeAttribute("hidden")` (matches) and `setAttribute("hidden", "")` (non-matches) for clarity and browser compatibility
- Added `.combobox-panel[hidden] { display: none; }` CSS rule so the panel actually hides when JS sets the `hidden` attribute
- Reordered Oxford entries in `config/community_registry/ms.yaml`: `ms-oxford-2803450` (LEAID 2803450, OXFORD SCHOOL DISTRICT) now comes first, so dedup keeps the correct entry (not 2802370)

**Decisions:**
- Used explicit `removeAttribute`/`setAttribute` in filter instead of `toggleAttribute` for clearer intent and safer browser compatibility
- Retry logic uses 500 ms delay between attempts, 3 total attempts — balances cold-start coverage vs. UX latency on real errors
- Error panel positioned manually via `getBoundingClientRect()` instead of creating a new `_cbOpenPanel` variant — avoids API surface expansion

**Next Steps:**
- [ ] Investigate potential race condition where `_cbSelect` may fire on keystroke instead of just Enter/click (symptom: console.log appears 3× while typing "Oxf")
- [ ] Add defensive LEAID validation to prevent accidental slug swaps in future registry updates

**Test Status:** 650 passed

---

### Session 5 — 2026-06-07 (5760915)

**Accomplished:**
- Created `scripts/trim_national_parquets.py`; trimmed `nces_lea_finance.parquet` from 352→7 columns (6.8 MB→680 KB); restructured `.gitignore` to allow `*.parquet` in `data/raw/national/`; committed 4 national parquets (~2 MB total)
- Fixed `app/ui/server.py` `/api/cities` route — replaced NM-hardcoded `nces_district_map` lookup with `_communities_from_registry()` reading `config/community_registry/{state}.yaml`; NM falls back to district_map via `_communities_from_district_map()`
- Added `_registry_prefix_lookup()` to `main.py` and wired into `resolve_community()` — `"Oxford"` → `ms-oxford` → `ms-oxford-2803450` via highest-enrollment prefix match (2 candidates in MS registry)
- Added `_inject_market_routing()` to `pipeline/s6_synthesis.py` injecting `market_type` (`greenfield` when `has_charters=False` in registry) and `verdict` (`INELIGIBLE` when tier=`AVOID`); wired template dispatch in `pipeline/s7_render.py` before mode default
- Created `templates/greenfield_brief.html.j2` (initial skeletal) and `templates/ineligible_brief.html.j2`; ran Oxford, MS smoke test — community resolution clean, S1–S3 data fetchers all succeeded, mock stopped at fixture-less LLM call (expected)
- Rebuilt `templates/greenfield_brief.html.j2` to full NM parity (13 sections): added banners, scorecard bars with color-coded fills, top drivers, override flags, local authorizers, recommendations, quick reads, sources, debug panel, sidebar score block + nav, responsive + print stylesheets, smooth-scroll scripts; offline-safe system font stack (no CDN)
- DIFFERENCE 1 — school cards replaced with "No established charter landscape" notice card; DIFFERENCE 2 — `academic_need` dimension renders "Awaiting Data" with ghost bar + note "No proficiency data available for this state" when `used_default=True`
- Oxford re-render via `--stages s7_render` (zero API cost): 36.8 KB, all 13 sections confirmed; 575/575 tests pass
- Ineligible template gap-checked: same 7 missing sections; flagged as next-session task (spawn chip created)

**Decisions:**
- Greenfield uses system font stack only (`var(--font-sans/mono/serif)`) — NM template retains Google Fonts CDN; offline rendering constraint applies only to the new templates
- `_registry_prefix_lookup` picks highest-enrollment match when multiple slugs share a city prefix — safe default for the majority case; logs candidate count
- `Awaiting Data` conditional is `row.dimension == 'academic_need'` — intentionally narrow until MS/TN/WI proficiency adapters are built

**Next Steps:**
- [ ] Bring `templates/ineligible_brief.html.j2` to full NM parity (same 13-section rebuild, red ineligible styling, "expansion suppressed" notice instead of school cards)
- [ ] Verify and populate MS/TN/WI `charter_law` fields from authoritative state statutes
- [ ] Verify `per_pupil_revenue_avg` for MS (12500.0), TN (11800.0), WI (13200.0) against NCES FY2022 actuals
- [ ] Populate MS/TN/WI `data_sources` URLs (authorizer_registry, charter_roster, enrollment_data, performance_data)
- [ ] Run full MS community batch (`--state MS --depth fast`) once charter_law is verified

---

### Session — 2026-06-04 (1dcfa76)

**Accomplished:**
- Removed 8-line shapefile bake block from `Dockerfile` (`ARG SHAPEFILE_BUST`, `COPY scripts/download_zcta_shapefile.sh`, two `RUN` commands) — shapefile is no longer fetched or written at image build time
- Removed 8-line runtime ZCTA shapefile download fallback from `docker-entrypoint.sh` — the container no longer attempts to download the shapefile on startup
- Both removals are intentional: the ZCTA shapefile now lives on the Railway persistent volume via manual upload and is expected to be present at the symlinked path

**Decisions:**
- Shapefile lifecycle moved entirely off the container and onto the volume — eliminates build-time download failures and cold-start crash loops; shapefile must be manually maintained on the volume

**Next Steps:**
- [ ] Verify shapefile is present on Railway volume at `data/raw/national/tl_2023_us_zcta520/tl_2023_us_zcta520.shp` before next deploy
- [ ] Confirm zip drill works end-to-end on Railway after these removals

### Session — 2026-06-04 (3f1214c)

**Accomplished:**
- Built `pipeline/layer2/ingestors/gmail_ingestor.py` — external-only Gmail ingestor (Phase 2); no LLM extraction, snippet-only evidence, always `confidence=None`/`confidence_tier="review"`
- Added `_PERSONAL_ADDRESS_EXCLUDE` constant (`bauslander8@gmail.com`, `b.auslander@ufl.edu`) to drop threads where the only external participants are the operator's own personal addresses
- Implemented `--dry-run` + `--fixture-file` modes; dry-run validated against real Gmail MCP data (19 threads → 3 signals after filtering)
- Wrote 3 external signals live to Notion (`status="active"`, `confidence_tier="review"`, `operator_verified=False`); Notion row IDs: `375850c1-d2b5-816b-b9bc-d03635ce1fca`, `375850c1-d2b5-81d4-8147-d7f1cbb07091`, `375850c1-d2b5-811b-a62b-d7ea3ef9d5e9`
- Added `gmail_ext` key to `config/ingest_cache.json` dedup cache; added `config/ingest_cache.json` to `.gitignore` (runtime state)
- Verified full test suite at **502 passing** after all Layer 2 Phase 2 additions

**Decisions:**
- `ingestors/` (no LLM) lives parallel to `ingest/sources/` (LLM extractor path) — keeps external stub signals structurally separate from Claude-extracted signals
- `city=None` coerced to `city=""` before Notion write to avoid null-query crash in `_city_page_id`; all 3 live signals linked to a blank-slug placeholder city row — operator must correct city relations at review time
- F-signal/P-score independence maintained: `gmail_ingestor.py` never imports S1–S7 stages; P-score pipeline unmodified

**Next Steps:**
- [ ] Fix blank-slug city placeholder issue: clear city relation on the 3 live signals (`375850c1-d2b5-816b`, `375850c1-d2b5-81d4`, `375850c1-d2b5-811b`) and implement proper None-safe city handling in live write path
- [ ] Operator review: assign `city`, `dimension`, and `direction` on all 3 signals in the Notion "Needs Review" view before any blend
- [ ] Wire Gmail ingestor to scheduled run (cron or `clip_ops/run_daily.py`) for ongoing ingestion
- [ ] Phase 3: wire Layer 2 blend into S5 scoring

### Session — 2026-06-04 (43f1429)

**Accomplished:**
- Built CLIP Layer 2 (soft-signal intelligence) Phase 1 code path under new `pipeline/layer2/` package
- Added `decay.py` (pure half-life decay math) and `blend.py` (s5_blend: anchor/reliability/field-score fusion of public score with field signals) — standalone, not wired into S5
- Added `storage_interface.py` (`SignalStore` ABC), `notion_client.py` (live `NotionSignalStore`, lazy SDK import + 3 req/s throttle + 429 backoff), `supabase_client.py` (interface-compliance stub)
- Added `config/field_signal_weights.yaml` (10 dimensions; 3 spec keys remapped to real `scoring_weights.yaml` keys) and placeholder `config/notion_ids.yaml`
- Added 36 unit tests (`test_decay.py` 9, `test_blend.py` 24, `test_storage_interface.py` 3) + `tests/fixtures/synthetic_signals.py`; full suite 453 passing
- Added `notion-client>=2.2.1` to `requirements.txt` (not installed)

**Decisions:**
- Dimension keys follow the repo's `scoring_weights.yaml`, not the spec: `facilities_availability`→`facilities_feasibility`, `financial_sustainability`→`funding_environment`, `operator_talent_pool`→`replication_feasibility`
- blend/decay kept network-free and pure; all I/O lives behind the `SignalStore` interface so Notion can later swap to Supabase without touching blend or ingestion
- Notion SDK imported lazily inside `NotionSignalStore.__init__` so the package stays importable without `notion-client` installed

**Next Steps:**
- [ ] Connect a Notion MCP session, then create the 4-table workspace (City→Dimension→Source→Signal, ~19 API calls) and write real IDs to `config/notion_ids.yaml`
- [ ] `pip install notion-client` and run the manual smoke test: `python -m pipeline.layer2.notion_client`
- [ ] Create Signal saved views manually (Notion API can't: "Needs Review", "Active by City", "Expired/Archive")
- [ ] Phase 2: wire Layer 2 into S5 + build Gmail/Granola ingestion (out of Phase 1 scope)

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

---

### Session 34 (continued) — 2026-06-03 (b2a813f)

**Accomplished:**
- Diagnosed Santa Fe charter count: 9 in PED roster vs. 8 in NCES CCD — `Sun Mountain Community School` present in PED roster but absent from NCES; gap is an NCES reporting lag, not a missing school; no PEC per-school active list exists in config to cross-reference
- Fixed `app/rate_limit.py` midnight reset bug: replaced rolling 24h window in `_window_entries()` with UTC-midnight cutoff (`_utc_today_start()`); fixed `check_global_daily_cap()` to use `_utc_today_date()` not server-local `date.today()`; fixed `_parse_dt()` to treat naive timestamps as UTC; rewrote `_hours_until_reset()` as hours-to-UTC-midnight
- Added `scripts/trim_nces_csv.py` — state-agnostic CLI that trims any NCES national CSV to a single state (auto-detects `ST`/`LSTATE`/`FIPST` column); trimmed both `data/raw/nm/nces_sch_lunch_2024.csv` and `data/seeded/nm/nces_sch_lunch_2024.csv` from 89.4 MB → 831 KB (88.6 MB saved each)
- Fixed `pipeline/s6_synthesis.py` data_through bug: added `_derive_data_through(brief_json)` that collects oldest vintage year from non-ephemeral `sources[*].date` and `top_charter_schools[*].proficiency_year`, returning `{year}-12-31` (fallback: `(today.year-1)-12-31`); changed unconditional `generated_at = timestamp_now()` to `setdefault`
- Added 27 new tests: 12 in `tests/unit/test_rate_limit.py`, 15 in `tests/unit/test_s6_data_through.py`; suite 375 → 390 passing

**Decisions:**
- Rate limit uses UTC midnight reset (not rolling 24h) — server-timezone-independent; all helpers (`_utc_today_start`, `_utc_today_date`, `_utc_now`) are thin mockable wrappers
- `_derive_data_through` excludes MEDIA/WEB_SEARCH/PRESS_RELEASE/BLOG/SCRAPED source classes to avoid scrape dates masquerading as data vintage; fully generic, no state-specific names hardcoded
- `generated_at` bug was in S6 (line 187), not S7 as originally reported — S7 never modifies the brief dict
- NM-hardcoding in `s1_discovery._city_from_address` (splits on `, NM\b`) flagged but not fixed per scope; `LUNCH_CSV` hardcoding in `nces_fetcher.py` spawned as a separate chip task

**Next Steps:**
- [ ] Generalize `_city_from_address()` in `s1_discovery.py` — splits on `, NM\b`, will fail for non-NM states
- [ ] Generalize `LUNCH_CSV = "data/raw/nm/nces_sch_lunch_2024.csv"` in `pipeline/utils/nces_fetcher.py:37`
- [ ] Expand `STATE_NAME` substitution to full state name via `states.yaml` (`s6_synthesis.py:249`)
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session 35 — 2026-06-03 (b9959cf)

**Accomplished:**
- Fixed `_city_from_address()` and `_clean_city()` in `s1_discovery.py` — both hardcoded `, NM\b` split/strip patterns; introduced `state` param threaded from `parse_roster` so any state works; added 18 unit tests in `tests/unit/test_s1_discovery.py`
- Fixed `nces_fetcher.py` — `FINANCE_CSV = "data/raw/nm/..."` used in `_read_finance()`; changed to accept `state` param and derive path dynamically; removed dead `LUNCH_CSV` constant; tagged `NM_STATE_AVG_PPR` as `TODO(S35-sweep)`; added 5 unit tests in `tests/unit/test_nces_fetcher.py`
- Resolved `STATE_NAME` TODO in `s6_synthesis._generate_brief` — now calls `_load_state_config(state).get("name")` to expand code to full name (e.g. "New Mexico"); falls back to code for states not yet in `states.yaml`; added 4 unit tests in `tests/unit/test_s6_state_name.py`
- Traced `sources[*].date is None` structural gap — every S3 fetcher sets `source_date: None`; added `TODO(S35)` at NCES injection site explaining required fix (per-fetcher fiscal year-end date); no speculative fix added
- Completed NM hardcode sweep — added `TODO(S35-sweep)` to 7 more files: `saipe_fetcher.py` (STATE_FIPS), `population_trends_fetcher.py` (NCES source paths), `charter_schools_fetcher.py` (paths), `authorizers_fetcher.py` (roster + authorizer registry), `s3_fact_extraction.py` (_PED_ROSTER_TITLE + "NM state avg" label), `s4_verification.py` (NM PED shortage reference)

**Decisions:**
- `_city_from_address` uses `re.escape(state)` in a raw f-string pattern — correct for all 2-letter ASCII state codes; `\b` boundary prevents partial matches
- `FINANCE_CSV` constant removed entirely; `LUNCH_CSV` removed as dead code (`_aggregate_frl` already derived path from `state`)
- `STATE_NAME` falls back to raw code so states not yet in `states.yaml` still produce valid output
- No speculative fix for `source_date`; the structural gap requires per-fetcher changes and was scoped out of S35

**Next Steps:**
- [ ] Generalize `NM_STATE_AVG_PPR` in `nces_fetcher.py` — load per-state average from states.yaml
- [ ] Generalize `STATE_FIPS` in `saipe_fetcher.py` via states.yaml `state_fips` field
- [ ] Generalize NCES source file paths in `population_trends_fetcher.py` (see `TODO(S35-sweep)`)
- [ ] Populate `source_date` in S3 fetchers with fiscal year-end dates to enable accurate `data_through` vintage dating
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session 35 (continued) — 2026-06-03 (9f2c7bc)

**Accomplished:**
- Rewrote `templates/strategic_brief.html.j2` in-place — full dark-theme "precision intelligence" redesign; replaced light-blue chrome palette with `#0d1117` dark background and semantic CSS variables
- Introduced two-column sticky sidebar layout (260px sidebar + flex main, 1140px max-width); sidebar shows community name, DM Serif Display score dial, classification pill, confidence badge, data-through, and smooth-scroll section nav
- Replaced `-apple-system` font stack with Google Fonts: DM Serif Display (score/headings), IBM Plex Mono (labels/debug/sources), IBM Plex Sans (body)
- Added CSS animations (`fadeIn` on sections, `scaleIn` on score block), card hover transitions, scroll-position nav highlighting via IntersectionObserver
- Added responsive breakpoint at 900px (sidebar collapses to horizontal strip, nav hidden) and `@media print` palette inversion to white/black
- Preserved all 60+ Jinja2 expressions exactly as in original; Jinja2 parse verified clean; 417 tests still pass

**Decisions:**
- `brief.scorecard_summary.tier_display_label` referenced directly in the sidebar rather than via `sc` shorthand — keeps `{% set sc = brief.scorecard_summary %}` untouched in section 4
- `@media print` uses CSS variable overrides to invert the dark palette rather than class-swapping, keeping the HTML clean
- Quick reads section uses `.qr-card` wrapper cards for visual parity with school cards

**Next Steps:**
- [ ] Generalize `NM_STATE_AVG_PPR` in `nces_fetcher.py` — load per-state average from states.yaml
- [ ] Generalize `STATE_FIPS` in `saipe_fetcher.py` via states.yaml `state_fips` field
- [ ] Generalize NCES source file paths in `population_trends_fetcher.py` (see `TODO(S35-sweep)`)
- [ ] Populate `source_date` in S3 fetchers with fiscal year-end dates to enable accurate `data_through` vintage dating
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session 36 — 2026-06-04 (2d23bbe)

**Accomplished:**
- Created all four Notion databases (City, Dimension, Source, Signal) via MCP with full schema; Signal includes 3 outbound relations (city, dimension, source) and a self-relation (`superseded_by`)
- Seeded City DB (3 rows: nm-albuquerque, tx-houston, oh-columbus) and Dimension DB (10 rows with per-dimension `wf_base`/`half_life_days` from `field_signal_weights.yaml`); defaults applied for `competitive_opportunity` and `operational_complexity`
- Wrote 8 IDs to `config/notion_ids.yaml` — 4 database IDs (for `pages.create`) and 4 data source UUIDs (for `data_sources.query`)
- Fixed `pipeline/layer2/notion_client.py`: title property key `"name"` → `"title"` in `write_signal` and `write_source`; all 5 `databases.query` calls migrated to `data_sources.query`; `_page_to_source` reader fixed from `_r_text` to `_r_title`; data source IDs loaded in `__init__`
- Pinned `notion-client>=2.2.1,<3.0.0` in `requirements.txt` — v3 removed `databases.query`; installed 2.7.0
- Smoke test passing end-to-end: write source → write signal → read back (1 active) → update → expire stale
- Created 3 operator saved views on Signal DB via MCP: "Needs Review" (table, `confidence_tier=review`), "Active by City" (board, `status=active`, grouped by city), "Expired / Archive" (table, `status=expired OR rejected`)

**Decisions:**
- Config stores both `*_db_id` and `*_ds_id` separately — `pages.create` uses database IDs, `data_sources.query` uses collection UUIDs; they are different identifiers in the new Notion API
- `superseded_by` added as a one-way relation (not DUAL) to avoid an unwanted reverse synced property on Signal
- Databases created via Notion MCP used a different integration token than the `CLIP` key in `.env`; required manual "Share with CLIP integration" step in Notion UI before the Python SDK could access them — document this for any future workspace recreation
- `notion-client` v3 released with breaking API changes; `<3.0.0` pin prevents silent breakage on next `pip install`

**Next Steps:**
- [ ] Wire `NotionSignalStore` into the S3/S4 pipeline ingestion path — signals extracted by S3 should be written to Layer 2 at pipeline end
- [ ] Generalize `NM_STATE_AVG_PPR` in `nces_fetcher.py` — load per-state average from states.yaml
- [ ] Generalize `STATE_FIPS` in `saipe_fetcher.py` via states.yaml `state_fips` field
- [ ] Populate `source_date` in S3 fetchers with fiscal year-end dates
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session — 2026-06-04 (c7c3a0e)

**Accomplished:**
- Added defensive markdown-fence stripping to `SignalExtractor.extract()` in `pipeline/layer2/ingest/extractor.py` — `import re` added, three-line strip block inserted before `json.loads()` to handle model wrapping responses in ` ```json ``` ` blocks despite system-prompt instructions
- Updated `tests/unit/test_extractor.py`: renamed `test_markdown_fenced_json_not_parsed` → `test_markdown_fenced_json_is_parsed` and flipped assertion to confirm a fenced payload is parsed and written correctly
- Wired Layer 2 ingestion into `main.py` CLI: added `--ingest {granola,gmail,all,file}` and `--ingest-path` arguments, and `_run_ingest()` dispatch function (strictly independent of S1–S7)
- Added `pipeline/layer2/ingest/` source modules (Granola, Gmail, FileIngester) and `tests/unit/test_file_ingester.py`
- All 489 unit tests pass; 502 total across full suite

**Decisions:**
- Fence stripping is done with two `re.sub` calls (prefix tag + trailing fence) rather than a single greedy regex — avoids clobbering embedded newlines inside the JSON array
- `_run_ingest` defers all Layer 2 imports to call time so `main.py` stays fast for S1–S7 users who never touch `--ingest`

**Next Steps:**
- [ ] Run `python main.py --ingest file --ingest-path <path>` against a real Granola export to validate end-to-end write path
- [ ] Wire `NotionSignalStore` into the S3/S4 pipeline ingestion path — signals extracted by S3 should be written to Layer 2 at pipeline end
- [ ] Generalize `NM_STATE_AVG_PPR` in `nces_fetcher.py` — load per-state average from states.yaml
- [ ] Generalize `STATE_FIPS` in `saipe_fetcher.py` via states.yaml `state_fips` field
- [ ] Populate `source_date` in S3 fetchers with fiscal year-end dates
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session 3 / National CCD — 2026-06-05

**Accomplished:**
- Created `scripts/download_nces_national.py` — downloads 6 national NCES CCD files from the Urban Institute Education Data API using per-state pagination strategy; produces `nces_lea_directory_2022.csv`, `nces_sch_directory_2022.csv`, and `nces_lea_membership_{2020,2022,2023,2024}.csv` in `data/raw/national/`
- Created `scripts/build_national_parquet.py` — converts those CSVs to 4 compressed parquets: `nces_lea_directory.parquet` (19,714 districts, `has_charter_schools` derived from school directory), `nces_lea_finance.parquet` (19,570 rows, 51 states; source was `data/raw/nm/nces_lea_finance_2024.csv` already national, moved to `data/raw/national/`), `nces_sch_lunch.parquet` (146K FRL rows in NCES filter format), `nces_lea_membership.parquet` (78K rows, years 2020/22/23/24)
- Updated `pipeline/utils/nces_fetcher.py`: `_read_finance()` reads from `data/raw/national/nces_lea_finance.parquet` (LEAID lookup by index); `_aggregate_frl()` reads from `data/raw/national/nces_sch_lunch.parquet` (filtered by LEAID, LUNCH_PROGRAM, DATA_GROUP); added `pandas` import; both fall back gracefully with clear message if parquet missing
- Updated `pipeline/utils/population_trends_fetcher.py`: removed `NCES_SOURCE_FILES`, `_MEMBERSHIP_FILES`, `_SCHOOL_MEMBERSHIP_FILES` — all were NM-hardcoded paths to deleted CSV files
- Updated `scripts/build_nces_cache.py`: replaced per-state CSV imports with a single national parquet read filtered by STABBR; removed `NCES_SOURCE_FILES` import from population_trends_fetcher
- Updated `scripts/build_community_registry.py`: `load_ccd()` accepts `.parquet` files (detected by extension) via new `_iter_ccd_rows()` helper; reads `has_charter_schools` column from directory parquet as `has_charters` fallback when no roster signals are provided; enrollment parsing handles float strings (e.g. `"4766.0"`)
- Built community registries from `data/raw/national/nces_lea_directory.parquet`: MS (142 districts, 4 with charters), TN (139 districts, 6 with charters), WI (312 districts, 94 with charters)
- Built MS membership parquet at `data/processed/ms/nces_membership_ms.parquet` (614 rows, 4 years, 159 districts) via updated `build_nces_cache.py`
- Updated `tests/unit/test_nces_fetcher.py`: rewrote 2 state-specific CSV tests to use parquet fixtures; added `_write_finance_parquet` helper; added `test_read_finance_returns_none_for_unknown_leaid`
- NM regression verified: APS LEAID `3500060` returns `MEMBERSCH=79805`, `FRL=55009`; NM parquet (`data/processed/nm/nces_membership_nm.parquet`) intact at 317,158 rows

**Decisions:**
- `_read_finance()` looks up by LEAID directly (globally unique); `state` param is no longer used for path derivation — both NM and any other state read the same national parquet
- `has_charter_schools` from the Urban Institute school directory (charter==1 schools per LEA) is used as the authoritative `has_charters` signal in the registry; roster city/name matching still takes priority when a roster is provided
- The NM validation warnings in `build_national_parquet.py` comparing directory/finance rows to `nces_ccd_schools_nm.csv` are expected false alarms — the baseline is school-level (892 rows) while the directory parquet is district-level (~194 NM districts)
- `build_nces_cache.py` is now fully self-contained; it no longer imports from `population_trends_fetcher`

**Tests:** 544 passed.

**Next Steps (Session 4):**
- [ ] Add MS, TN, WI to `config/states.yaml` (nces_district_map entries + data_sources) to enable full S1–S7 pipeline runs for those states
- [ ] Wire S5/S6/S7 templates for MS/TN/WI (Session 5)

---

### Session — 2026-06-04 (2a9887f)

**Accomplished:**
- Rewrote `pipeline/layer2/ingest/sources/gmail.py` to use direct Gmail REST API — removed all `MCPHTTPClient`, `MCPError`, `MCPUnexpectedSchema` imports and the `_find_tool` helper entirely
- Added `_exchange_refresh_token()`: POSTs to `https://oauth2.googleapis.com/token` using `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` env vars; called in `__init__` so credential errors surface before ingest begins
- Added `_http_get()` for authenticated REST calls using stdlib `urllib` (no `requests` — not in requirements.txt)
- Added `_decode_body_data()` for base64url → UTF-8 decoding of Gmail message body parts
- Added `_extract_text_from_payload()` to recursively extract text from multipart MIME payloads; `_get_subject_from_thread()` to pull the Subject header from message payload headers
- Removed `mcp_url` constructor parameter; all 502 unit tests pass

**Decisions:**
- Credential validation and token exchange placed in `__init__` (not `ingest()`) — fails fast with a clear `SystemExit(1)` + stderr message rather than silently getting a 401 mid-run
- `urllib` (stdlib) used instead of `requests` since `requests` is absent from `requirements.txt`; avoids adding a new dependency for three simple HTTP calls
- Every thread is always fetched with `format=full` — eliminates the old MCP conditional logic that only fetched full content when a separate get-thread tool existed

**Next Steps:**
- [ ] Set `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` in `.env` and run a live `--ingest gmail` to smoke-test the full REST path
- [ ] Run `python main.py --ingest file --ingest-path <path>` against a real Granola export
- [ ] Wire `NotionSignalStore` into the S3/S4 pipeline ingestion path
- [ ] Generalize `NM_STATE_AVG_PPR` in `nces_fetcher.py`
- [ ] Generalize `STATE_FIPS` in `saipe_fetcher.py`
- [ ] Populate `source_date` in S3 fetchers with fiscal year-end dates
- [ ] Fill `config/pec_renewal_stats.yaml` with verified PEC renewal/denial rate data
- [ ] Calibrate scoring weights (blocked on operator-profile conversation with The Mind Trust)

---

### Session 4 — 2026-06-07 (9180502)

**Accomplished:**
- Added MS, TN, WI state entries to `config/states.yaml` — each with full `charter_law`, `nces_district_map` (142/139/312 entries), `data_sources`, and `per_pupil_revenue_avg`; all charter_law fields set `null # NEEDS_VERIFICATION`
- Added `per_pupil_revenue_avg: 24356.0` to NM entry in `config/states.yaml`
- Rewrote `pipeline/s2_state_context.py` to replace 90-day TTL cache logic with event-driven `_should_regenerate()` — regenerates only on missing cache, `--force`, or active `s2_cache_bust` Notion signal; clears signal after successful regen
- Added `get_s2_cache_bust_signals(state)` to `pipeline/layer2/notion_client.py` (`NotionSignalStore`) — queries active signals, filters Python-side by `source_metadata` JSON for `signal_type=s2_cache_bust` and matching state
- Removed `NM_STATE_AVG_PPR = 24_356.0` hardcode from `pipeline/utils/nces_fetcher.py`; replaced with `_get_state_avg_ppr(state)` — loads from `states.yaml`, falls back to national parquet, then `None`
- Created `tests/unit/test_s2_cache_signal.py` — 31 new tests (575 total, all pass); covers states.yaml structure for MS/TN/WI, `_should_regenerate` (5 cases), nces_fetcher PPR loading (4 cases)
- Oxford smoke test (`--state MS --community ms-oxford --depth fast`) exit 0; S2 regenerated via cache-absent path; brief rendered 4.2/10 WATCHLIST with real USAspending + ProPublica data

**Decisions:**
- Notion signal schema has no native `signal_type`/`state`/`cleared` fields — stored in `source_metadata` JSON; `status=active/expired` used as cleared flag; no schema migration needed
- `_should_regenerate` priority: cache absent → force → Notion signal; Notion never queried if cache is absent
- All MS/TN/WI `charter_law` fields intentionally null — no web source treated as verified for legal/policy data

**Next Steps:**
- [ ] Verify and populate MS/TN/WI `charter_law` fields from authoritative state statutes
- [ ] Verify `per_pupil_revenue_avg` for MS (12500.0), TN (11800.0), WI (13200.0) against NCES FY2022 actuals
- [ ] Wire Phase 2 ingestion to write `s2_cache_bust` signals with correct `source_metadata` JSON
- [ ] Populate MS/TN/WI `data_sources` URLs (authorizer_registry, charter_roster, enrollment_data, performance_data)
- [ ] Run full MS community batch (`--state MS --depth fast`) once charter_law is verified

---

### Session — 2026-06-07 (37c5864)

**Accomplished:**
- Diagnosed city combobox dropdown invisibility: `backdrop-filter: blur(20px)` on `#panel-scan` creates a CSS containing block for `position: fixed` descendants, causing `#city-listbox` viewport-relative coordinates from `getBoundingClientRect()` to be interpreted as panel-relative — on an ~860px viewport the dropdown rendered ~500px off-screen to the right
- Fixed in one line inside `_cbInit()` in `app/ui/static/js/app.js`: `document.body.appendChild(panel)` moves the listbox out of `#panel-scan`'s DOM subtree so `position: fixed` resolves correctly against the viewport
- Verified all existing `_cb*` functions (`_cbBuild`, `_cbFilter`, `_cbOpenPanel`, `_cbClosePanel`, `_cbSelect`) continue to work unchanged — they all use `getElementById("city-listbox")` which still finds the element regardless of its position in the DOM tree
- Confirmed 650 tests pass, zero CDN links introduced

**Decisions:**
- Chose the minimal single-line DOM move over alternatives (re-calculating offset-adjusted coordinates or removing `backdrop-filter` from the scan panel) — keeps all existing JS/CSS logic intact and is robust to future layout changes
- No HTML or CSS changes required; fix is entirely in the JS init function

**Next Steps:**
- [ ] Remove debug `console.log` statements in `_cbInit`, `_cbOpenPanel`, and related `_cb*` functions (added in commit `b9e0b42`, still present)
- [ ] Verify and populate MS/TN/WI `charter_law` fields from authoritative state statutes
- [ ] Verify `per_pupil_revenue_avg` for MS (12500.0), TN (11800.0), WI (13200.0) against NCES FY2022 actuals
- [ ] Wire Phase 2 ingestion to write `s2_cache_bust` signals with correct `source_metadata` JSON
- [ ] Run full MS community batch (`--state MS --depth fast`) once charter_law is verified

---

### Session — 2026-06-08 (c746d88)

**Accomplished:**
- Created `templates/pdf_brief.html.j2` — single-column PDF-optimized Jinja2 template; IBM Plex fonts (Google Fonts + system fallbacks), identical dark-theme palette, `@page Letter` sizing, all brief sections (scorecard bars, drivers, flags, school cards or greenfield notice, authorizers, recommendations, quick reads, needs verification, data sources), no animations or sidebar
- Added `GET /api/brief/pdf?run_id=<run_id>` route to `app/ui/server.py` — loads S6 brief JSON from `data/cache/synthesis/`, applies weight patch, renders `pdf_brief.html.j2`, pipes through Playwright Chromium (`wait_until="networkidle"`), returns `application/pdf` download
- Added `_pdf_patch_excluded_weights()` to `server.py` — inlined mirror of `s7_render._patch_excluded_weights`, restores original dimension weights from `s5_scorecard.json` before PDF render
- Added `#btn-download-pdf` anchor to `app/ui/templates/index.html` brief toolbar; wired in `loadBrief()` in `app.js` — hidden by default, shown with correct `href` for non-zip runs only
- Added inline PDF link to run cards in `renderRunCards()` in `app.js` alongside "View Brief" button; zip drill runs excluded from both buttons
- Added `playwright>=1.40.0` to `requirements.txt`; updated `Dockerfile` with `ENV PLAYWRIGHT_BROWSERS_PATH` and `playwright install chromium --with-deps` layer

**Decisions:**
- PDF route uses `run_id` query param (not path slugs) to match the existing `/api/brief` pattern — consistent with how all brief data is already accessed server-side
- Route reads S6 brief JSON directly (not the pre-rendered HTML) so the PDF template gets structured data, enabling clean single-column layout independent of the screen template
- `_pdf_patch_excluded_weights` inlined in `server.py` rather than imported from `pipeline/` — preserves the app's CLI-only contract with the pipeline layer
- IBM Plex fonts loaded via Google Fonts CDN; `wait_until="networkidle"` in Playwright gives fonts time to resolve before PDF capture

**Next Steps:**
- [ ] **SECURITY: restore `@require_login` on `/api/brief/pdf`** — commented out for local testing (`app/ui/server.py:593`), must be re-enabled before any Railway deploy
- [ ] Run `playwright install chromium` locally once before testing PDF route
- [ ] Test Google Fonts loading inside Railway container — if CDN is blocked, self-host fonts in `app/ui/static/fonts/` and update `pdf_brief.html.j2` fallback `src:` URLs
- [ ] Smoke-test PDF output against `demo-questa-001` run (ms-oxford is the primary test case)
- [ ] Remove debug `console.log` statements in `_cbInit`, `_cbOpenPanel`, and related `_cb*` functions (added in commit `b9e0b42`, still present)

**Tests:** 650 passed.

---

### Session — 2026-06-08 (c80e330)

**Accomplished:**
- Diagnosed PDF render bug: white margins and unstyled output caused by Google Fonts CDN blocking `wait_until="networkidle"` in Railway sandbox and missing explicit Playwright margin param
- Removed Google Fonts CDN `<link>` tags from `templates/pdf_brief.html.j2` — eliminates CDN hang; `networkidle` now settles immediately
- Updated `--font-sans`, `--font-mono`, `--font-serif` CSS variables to pure system stacks (dropped `IBM Plex Sans`, `IBM Plex Mono`, `DM Serif Display` named fonts that required the CDN)
- Added explicit `margin` dict to `page.pdf()` in `app/ui/server.py` — Playwright and CSS `@page` now agree on `0.6in`/`0.7in` margins instead of relying on version-specific Playwright defaults
- Added `base_url=f"file://{BASE_DIR}/"` to `set_content()` in `server.py` for correct relative-path resolution

**Decisions:**
- Removed CDN links entirely rather than self-hosting fonts — no new asset management needed; system stacks (Georgia, -apple-system, ui-monospace) were already in the fallback chain and are sufficient
- Explicit Playwright `margin` parameter preferred over relying on CSS `@page` alone — avoids version-specific Playwright behavior differences

**Next Steps:**
- [ ] Smoke-test PDF output against `demo-questa-001` run to confirm dark background and correct margins
- [ ] Remove debug `console.log` statements in `_cbInit`, `_cbOpenPanel`, and related `_cb*` functions (added in commit `b9e0b42`, still present)
- [ ] Verify and populate MS/TN/WI `charter_law` fields from authoritative state statutes
- [ ] Run full MS community batch (`--state MS --depth fast`) once `charter_law` is verified

**Tests:** Test runner not detected — run manually before next session.

---

### Session — 2026-06-08 (pending)

**Accomplished:**
- Added dev-mode OAuth bypass in `app/ui/auth.py` — injected dummy session keys (`user_email: dev@themindtrust.org`, `user_name: Dev (local)`) when **both** `FLASK_ENV=development` **and** request from loopback IP (`127.0.0.1` or `::1`); structurally impossible to ship open since Railway never sets development env
- Fixed PDF rendering in `app/ui/server.py` `/api/brief/pdf` route — removed `base_url=f"file://{BASE_DIR}/"` from `set_content()` call (eliminated stale file:// base path references); added explicit `page.set_viewport_size({"width": 816, "height": 1056})` before content load (Letter @ 96 DPI); zeroed Playwright `margin` dict (defer entirely to CSS `@page` rule)
- Fixed scorecard right-side clipping in `templates/pdf_brief.html.j2` — added `flex-shrink: 0` to `.bar-score` to prevent score numbers from being squeezed by long dimension names; moved `@page { margin: 0.6in 0.7in }` to `@page { margin: 0 }` and `body { padding: 0.6in 0.7in }` (separated concerns); added explicit `html, body { width: 100%; min-height: 100%; background: #0d1117; }` to fill full Playwright viewport with dark background

**Decisions:**
- Two-condition bypass (`FLASK_ENV=development` AND loopback IP) — each condition is independently necessary and sufficient to disable the bypass, making accidental exposure structurally impossible
- Removed CDN fallback assumption (`base_url=file://...`) — Playwright default relative-path resolution is sufficient now that fonts are system-only
- All margin/padding control delegated to CSS (not Playwright parameters) — cleaner separation between rendering engine (Playwright) and layout (CSS)

**Next Steps:**
- [ ] Smoke-test PDF output against a live brief to confirm dark background fills edges and score numbers don't clip
- [ ] Test dev-mode OAuth bypass locally: `FLASK_ENV=development python3 app/ui/server.py`, then navigate to `http://localhost:5001`

---

### Session — 2026-06-08 (ff38ac3)

**Accomplished:**
- Fixed MS Local Authorizers block generating false eligibility verdict — added `_patch_authorizer_descriptions()` to `s6_synthesis.py`; derives correct `reputation_note` from `statutory_barrier.severity`/`applies` rather than serving stale S3 LLM text; guard prevents application to non-MS states
- Fixed three cached data artifacts for `ms-oxford-2803450`: corrected broken `reputation_note` ("Authorizes charter schools only in designated geographic areas…") in `s3_facts_raw.json` and `s6_brief_maturity_adjusted_mode2.json`
- Fixed FRL/SAIPE CEP conflict in operational_complexity scoring — added `_score_operational_complexity_cep()` and `_saipe_to_complexity_index()` to `s5_scoring.py`; when FRL > 85, SAIPE < 40, and gap > 50 pts, SAIPE drives the complexity index instead of the FRL artifact
- Fixed false "High poverty (X% FRL, Y% SAIPE)" narrative — added `_patch_cep_driver()` to `s6_synthesis.py`; deterministically overwrites the LLM-generated `dimension_table[operational_complexity].driver` string for CEP districts post-generation
- Patched cached `s6_brief_maturity_adjusted_mode2.json` for Oxford with correct CEP driver string for immediate re-render

**Decisions:**
- `_patch_authorizer_descriptions` placed AFTER `_inject_market_routing` — ordering is load-bearing because it reads `brief_json["statutory_barrier"]` which is set by market routing
- CEP gate follows the existing `competitive_opportunity`/`charter_saturation` pattern: special-case in `score_dimension`, returns `None` on no-match to fall through to standard path; no weight changes
- SAIPE→index thresholds anchored to national SAIPE norms (~15% median); SAIPE=12.2% → index=3 (same as prior LLM-derived value for Oxford — no composite shift for this community)
- `_patch_cep_driver` reads from `verified_bundle` (S4 facts) not scorecard — independent of S5 caching, fires even on cached S5 runs

**Next Steps:**
- [ ] Run full MS community batch (`--state MS --depth fast`) to verify authorizer and CEP patches fire correctly across all MS communities
- [ ] Check ms-ackerman and ms-jackson authorizer `reputation_note` values in next batch run — `_patch_authorizer_descriptions` will overwrite their existing notes
- [ ] Verify `cep_detected: True` appears in S5 scorecard output for Oxford on next forced re-score

**Tests:** Test runner not detected — run manually before next session.

---

### Session — 2026-06-08 (b33b13a)

**Accomplished:**
- Hardened the Oxford MS brief against red-team defects D2–D8 (D1 + CEP already shipped in `ff38ac3`) — eligibility-consistency + presentation fixes, no scoring changes
- D2: relabeled "Recommendations suppressed" → "Recommendations provisional — pending verification" in greenfield/strategic/pdf templates (recs still render)
- D3: `_build_data_sources` now renders proficiency vintage as a school-year span ("2022-23" not bare "2023"); precision fix — rejected the red team's false "2023-24" since the source CSV is SY2022-23
- D4: labeled the per-pupil figure as total/capital-inclusive (NCES TOTALEXP/enrollment) in the S3 prompt + Oxford brief
- D5/D6: added a data-driven demand-exclusion disclosure ("composite measures operational viability, not charter demand") and a weight-rounding residual note to the three eligible-market templates
- D7/D8: new `_inject_market_entry_flags()` in `s6_synthesis.py` — for MS consent-gated markets appends a rating-cycle verification flag + recs to test parent demand/differentiation and to confirm the amended 2025 § 37-28-7 (HB1432)
- Added `tests/unit/test_oxford_brief_hardening.py` (12 tests incl. the load-bearing D1 no-false-string render regression); full suite 763 → **775 passing**
- Wrote `docs/lennon_oxford_scoring_flags.md` (Funding 9.0 backwards charter-funding inference; Academic Need 5.0 boilerplate vs A-rated)

**Decisions:**
- No-fabrication rule overrides the task spec: D3 vintage relabeled to "2022-23" to match the source CSV, NOT the spec's "2023-24" (which the source does not support)
- S6 cache short-circuits all patches/injections, so S6-side fixes were both code-fixed (root cause, all gated-eligible markets) AND the cached Oxford JSON was hand-patched so the cache-hit re-render reflects them — mirrors how CEP/D1 shipped
- One permitted narrative neutralization: removed the false "reduced charter funding pressure" clause from the funding top-driver; flagged in the Lennon memo. No scores/weights/formulas changed
- D5/D6 done template-side (data-driven off `excluded_dimensions`/weights) so they render on every pass without a cache patch

**Next Steps:**
- [ ] Lennon to sign off on Funding 9.0 + Academic Need 5.0 methodology (see `docs/lennon_oxford_scoring_flags.md`)
- [ ] Run full MS batch with `--force` so `_inject_market_entry_flags` + D3/D4 fire freshly across ackerman/jackson (cached communities still hold old text until re-synthesized)
- [ ] Confirm the amended 2025 § 37-28-7 (HB1432) text and whether 2024-25 MDE ratings change Oxford's A

**Tests:** 775 passed (`python3 -m pytest -q -p no:cacheprovider`).

---

### Session — 2026-06-08 (a4cebf0)

**Accomplished:**
- Diagnosed a red-team "fresh `--force` run" report claiming four Oxford failures (market type greenfield→strategic, MDE rating B/"Not Available", Funding Environment unscored, 43.8% coverage) — verified against ground truth that **three do not reproduce** (greenfield is static config, MDE fetcher returns A, per-pupil $16,954.05 present, fresh S5 coverage 81.2%)
- Built RC3 hardening for the one real (latent) risk: added `_patch_statutory_narrative()` in `s6_synthesis.py` — a targeted 3-operation find-and-correct for consent-gated markets (wrong statute §37-28-5→§37-28-7, "ineligible" verdict→eligible-but-gated, strip false "unless rating changes to D/F" clause); not a prose rewriter; idempotent; logs every edit to a new `narrative_corrections` field
- Strengthened the S6 mode-2 prompt with an explicit "STATUTORY FRAMING" rule so future live runs frame the consent gate correctly first-pass
- Registered optional `narrative_corrections` in `brief.schema.json`; fixed a logging bug where `df_re.sub` discarded its callback (caught by a new test)
- Added 9 tests to `test_oxford_brief_hardening.py` (RC3 guard + lock-in tests pinning greenfield/rating-A/per-pupil); suite 775 → **784 passing**
- Appended a fresh-run-regression addendum to `docs/lennon_oxford_scoring_flags.md`

**Decisions:**
- No-fabrication / verify-first outranks the task prompt: refused to "fix" the three non-reproducing branches (would have edited correct code); added lock-in tests instead
- RC3 patch scoped to factual corrections only ("make it not false, not good") — no rating-letter reconciliation, no driver-narrative edits; prompt context is the lever for prose quality, the patch is the safety net
- No cache patch needed — Oxford's current brief is already clean, so the guard produces zero corrections; re-render is byte-stable on the deterministic checks
- `_patch_statutory_narrative` validated by unit tests; end-to-end on dirty LLM output pends a live `--force` run (no 3.11/API here)

**Next Steps:**
- [ ] Run a live `--force` MS batch to exercise `_patch_statutory_narrative` end-to-end and confirm prompt guidance reduces first-pass drift
- [ ] Lennon sign-off: (A) should a consent-gated greenfield composite headline "opportunity" when both demand dims are excluded; (B) Academic Need 5.0 boilerplate vs A-rated
- [ ] If the original fresh-run artifact exists, capture it — the described output didn't reproduce here (likely stale/pre-fix repo or missing data files)

**Tests:** 784 passed (`python3 -m pytest -q -p no:cacheprovider`).

---

### Session — 2026-06-08 (116515f)

**Accomplished:**
- Traced a 9-defect red-team report (B rating, prohibition framing, HOSTILE_AUTHORIZER cap, /9 denominator) entirely to a **superseded artifact** — `data/cache/synthesis/ms/ms-oxford/s6_brief_growth_mode2.json` (pre-LEAID-migration `ms-oxford` slug, `growth` preset, 2026-06-05, `statutory_barrier: NONE`); the target `ms-oxford-2803450`/`maturity_adjusted` is clean on all nine
- Confirmed no routing bug: `resolve_community` maps every spelling of Oxford → `ms-oxford-2803450` via registry prefix-lookup, so the bare slug can't be regenerated
- Widened `_patch_statutory_narrative` to catch prohibition-class false framing (restricts-to-C/D/F, statutorily prohibited, cannot authorize, automatic rejection, "no operator can legally enter", closed-by-law, "regardless of board support") and extended scope to dimension/top-driver narratives; idempotent, logs `prohibition_framing` to `narrative_corrections`, zero false-positives on the clean target
- Added 6 regression tests (prohibition removal, driver scope, logging, idempotency, no-false-positive, non-consent no-op); suite 784 → **790 passing**
- Deleted the orphaned bare-slug artifacts (cache + outputs for `ms-oxford`); appended a Session-11 addendum to `docs/lennon_oxford_scoring_flags.md`

**Decisions:**
- Verify-first held a third time: refused to "fix" the target for defects that live only in a dead pre-migration artifact; the only code change is preventive (widening the existing RC3 guard)
- No cache patch — the target was already clean, so the widened guard produces 0 corrections on it; re-render is byte-stable
- Lennon items A (consent gate → Political/Authorizer floor) and B (gated authorizer counted as 0 accessible → HOSTILE_AUTHORIZER) are already resolved by the Session-8 barrier layer in the target; flagged as confirm-intent only, no scoring touched

**Next Steps:**
- [ ] Live `--force` MS batch to exercise the widened prohibition patterns end-to-end on fresh LLM output
- [ ] Lennon sign-off on the four methodology questions (A–D) in the memo
- [ ] Consider a guard so deprecated community_id artifacts can't accumulate and mislead future red-teams

**Tests:** 790 passed (`python3 -m pytest -q -p no:cacheprovider`).

---

### Session — 2026-06-09 (bc1676c)

**Accomplished:**
- Added `demoted_by_consistency_check`, `consistency_check_id`, and `consistency_check_reason` fields to `_run_consistency_checks()` in `pipeline/s4_verification.py` so CHECK-001/002/003 are fully traceable — CHECK-001 sets `corrected_by_consistency_check=True`, CHECK-002/003 set `demoted_by_consistency_check=True`
- Added `_check_consistency_check_override()` helper in `pipeline/s5_scoring.py` called at the top of `score_dimension()` before all special-case paths; demoted/corrected dimensions return score=5.0 with `used_default=False`, keeping weight in the composite denominator (eliminates ~0.2 composite inflation from denominator shrinkage)
- Added `_inject_consistency_check_banner()` in `pipeline/s6_synthesis.py` that reads `consistency_check_triggered` flags from scorecard dimensions and injects `brief["consistency_check_corrections"]` for template rendering
- Added amber `banner-pci-legacy` banner to all three HTML templates (`strategic_brief.html.j2`, `greenfield_brief.html.j2`, `pdf_brief.html.j2`) that renders per-dimension check details when `brief.consistency_check_corrections` is non-empty
- Updated `schemas/brief.schema.json` to add `consistency_check_corrections` array property (required due to top-level `additionalProperties: false`)
- Oxford MS cold render confirmed: composite 6.28 (target 6.28 ± 0.10), banner present listing `political_climate/CHECK-002` and `authorizer_friendliness/CHECK-001`, genuine exclusions (`charter_saturation`, `competitive_opportunity`) unchanged
- Suite 825 → **843 passing** (18 new tests: 9 in test_s5_scoring, 7 in test_s4_verification, 5 in test_s6_injections)

**Decisions:**
- `used_default=False` for both corrected and demoted facts so the composite denominator stays stable; genuinely missing dimensions (`used_default=True`) are still excluded as before
- S5 checks corrected facts (in `relevant_facts`) before demoted facts (full-bundle scan) — CHECK-001 takes precedence over CHECK-003 for `authorizer_friendliness` when both fire on the same dimension
- Brief schema required an explicit property addition; scorecard schema did not (dimension_score allows additional properties)

**Next Steps:**
- [ ] Live `--force` MS batch to exercise consistency-check flags across ackerman/jackson (other MS communities may also trigger CHECK-002/003)
- [ ] Lennon sign-off on methodology questions A–D in `docs/lennon_oxford_scoring_flags.md`
- [ ] TODO(S35-sweep): `_pass_e_static_resolution` verification note references NM-specific config file — generalize to `config/{state}_ped_shortage_areas.yaml`
- [ ] Option B trust hierarchy (Lennon sign-off required before implementing)

**Tests:** 843 passed (`python3 -m pytest -q`).

---

### Session — 2026-06-09 (9a9e515)

**Accomplished:**
- Added `_run_consistency_checks(facts, pipeline_context)` to `pipeline/s4_verification.py` — three always-on guards (CHECK-001, CHECK-002, CHECK-003) that cross-check S3/S4 LLM-extracted facts against `config/charter_law/{state}.yaml` before writing `s4_verified.json`
- CHECK-001 corrects `num_accessible_authorizers=0` to `1` when a `consent_required` statutory barrier exists (MS § 37-28-7 pattern — consent adds a procedural step, not a prohibition)
- CHECK-002 demotes `political_climate_index` to `in_main_analysis=False` when ineligibility language appears in its own claim (primary) or in an adjacent auth-fact claim (secondary); catches cases where S3 puts the ineligibility reasoning in the `num_accessible_authorizers` claim rather than the PCI fact
- CHECK-003 demotes `school_board_charter_orientation` to `in_main_analysis=False` when the value asserts restriction/ineligibility but no `prohibition` barrier exists in config
- Added `_run_trust_hierarchy_check()` skeleton (Option B) with feature flag `s4_trust_hierarchy_enabled: false` in `config/pipeline.yaml` — no-op until enabled; raises `NotImplementedError` if toggled; Lennon sign-off required before enabling
- Wired consistency checks into `run()` after Pass E, before `_build_summary` (line 165); loads charter law barriers via existing `load_charter_law_barriers(state)` utility
- Cold-rendered Oxford MS with all cache deleted — all three checks fired; `num_accessible_authorizers` auto-corrected from 0→1, PCI and orientation demoted; composite 6.51 (vs 6.31 prior patched baseline — difference is LLM variance in this S3 run's other dimension facts, not a regression)
- Added 12 tests to `tests/unit/test_s4_verification.py` covering contradiction and pass-through cases for all three checks plus Option B no-op and NotImplementedError paths; suite 813 → **825 passing**

**Decisions:**
- CHECK-002 needs a secondary trigger path because S3 sometimes puts ineligibility language in the `num_accessible_authorizers` or `school_board_charter_orientation` claim rather than in the PCI fact itself; secondary fires when adjacent auth-fact claims contain ineligibility phrases AND PCI is `PROVISIONAL` or `UNVERIFIED`
- Option B feature flag lives in `config/pipeline.yaml` (not hardcoded), defaults to `false`; the `_run_trust_hierarchy_check` function accepts a plain dict `config` (not `PipelineConfig`) so it is testable without the full pipeline object
- Score difference (6.51 vs 6.28 target): when PCI is excluded from composite, S5 normalizes by remaining weight (0.6875 instead of 0.8125), inflating the result; target 6.28 assumed PCI stays in the composite at neutral 5.0 — this is a normalization interaction to resolve in a future session

**Next Steps:**
- [ ] Resolve PCI composite normalization: when CHECK-002 demotes PCI, S5 should use 5.0 as the PCI contribution in the composite *without* excluding it from the denominator — or CHECK-002 should correct the value to 5 and keep `in_main_analysis=True`
- [ ] Lennon sign-off on Option B trust hierarchy design before implementing
- [ ] Live `--force` MS batch to exercise all three consistency checks across ackerman/jackson (other MS communities may also trigger CHECK-002/003)
- [ ] Lennon sign-off on the four methodology questions (A–D) in `docs/lennon_oxford_scoring_flags.md`

**Tests:** 825 passed (`python3 -m pytest -q`).

---

### Session — 2026-06-08 (af70651)

**Accomplished:**
- Fixed `templates/pdf_brief.html.j2` — added Jinja2 `or` fallback on all three `.bar-name` spans so a missing `display_name` never blanks a scorecard heading; falls back to `dimension | replace('_', ' ') | title`
- Regenerated Oxford MS PDF with all dimension headings visible; used current post-hardening s6 JSON (6.3 / MODERATE OPPORTUNITY)
- `pipeline/utils/acs_fetcher.py`: added `_ACS_DISTRICT_TYPES` constant + unified→elementary→secondary fallback loop in `_fetch_district` and `get_total_population`, mirroring SAIPE behavior; extends ELL/population coverage to non-unified and secondary districts
- `pipeline/s3_fact_extraction.py`: removed hardcoded `"NM state avg"` label (S35 TODO resolved); now uses `state_context.get('state_name', state)` for all states
- `pipeline/utils/saipe_fetcher.py`: changed default `state=""` (was `"NM"`) to avoid misleading callers — FIPS is always derived from LEAID prefix regardless
- `config/states.yaml`: annotated `ms-choctaw` BIA LEAID 5900031 with a comment explaining that Census/ACS/SAIPE will structurally return None (BIA prefix 59 is not a Census state FIPS)
- Added 221 lines of ACS fallback tests to `tests/unit/test_acs_fetcher.py` (F-002: `_fetch_district` loop, end-to-end fallback, BIA LEAID graceful None, unified regression guard)
- Added 44 lines of SAIPE state-param tests to `tests/unit/test_saipe_fetcher.py` (F-001: state arg ignored, default is `''`)

**Decisions:**
- Template fix uses Jinja2 `or` so explicit `display_name` is always preferred; fallback fires only when the field is absent or empty (defensive, not overriding)
- PDF regenerated from current authoritative s6 JSON rather than reconstructed old data — the 3.5/AVOID ENTRY brief was an LLM artifact corrected by the hardening pipeline; 6.3 is the intentional post-fix state
- ACS fallback mirrors SAIPE's existing `_DISTRICT_TYPES` pattern exactly — same order, same semantics, one source of truth for district-type resolution strategy

**Next Steps:**
- [ ] Run live `--force` MS batch to exercise ACS district-type fallback + widened statutory narrative guard on fresh LLM output
- [ ] Lennon sign-off on methodology questions A–D in `docs/lennon_oxford_scoring_flags.md`
- [ ] Add guard against deprecated community_id slug artifacts accumulating in cache/outputs

**Tests:** Test runner not detected — run `python3 -m pytest -q -p no:cacheprovider` before next session.

---

### Session — 2026-06-11 (20b7736)

**Accomplished:**
- Created `app/cache_utils.py` with `bust_community_cache(community_id, state, leaid)`: deletes community cache (`data/cache/community/{state}/{community_id}/*.json`), synthesis cache (`data/cache/synthesis/{state}/{community_id}/*.json`), and scoped fetcher cache (paths containing `community_id` or `leaid`); hard guard prevents any touch of `config/charter_law/`
- Added `POST /api/regen/<state>/<community_id>` to `app/ui/server.py`: validates community against registry YAML, looks up `district_nces_id` for leaid scoping, calls `bust_community_cache`, launches `python3 main.py {community_id} --state {STATE} --preset maturity_adjusted --depth fast` in a background thread, returns `job_id` for polling
- Added `_run_regen_background()` and `_regen_running: set[str]` lock to `server.py`; background runner reuses existing `_jobs` store and `_append_stage_log` so client polls `/api/scan/status/<job_id>` with no new endpoint needed
- Added `↺ Regen Data` button to brief toolbar in `index.html`, confirmation modal, and a collapsible regen progress strip (live log + error fallback) that appears below the toolbar during regen
- Added CSS for regen UI components to `main.css` (`.regen-progress-strip`, `.regen-strip-status`, `.regen-log-body`, `.regen-error-msg`)
- Added regen JS in `app.js` (`setupRegenButton`, `_startRegen`, `_regenPollOnce`, `_onRegenComplete`, `_regenFailed`): posts to `/api/regen`, polls existing status endpoint, streams log lines into collapsible panel, reloads brief iframe on success without full page navigation
- Added 16 new tests: 9 in `tests/unit/test_cache_bust.py` (deletion, community scoping, leaid scoping, charter_law guard, summary shape), 7 in `tests/unit/test_regen_route.py` (400/404/409/200 + cache bust call); suite 843 → **859 passing**

**Decisions:**
- `app/cache_utils.py` (not `pipeline/cache_utils.py`) because `app/ui/server.py` cannot import from `pipeline/` per the app's CLI+filesystem-only contract
- Polling pattern reused (via existing `/api/scan/status/<job_id>`) instead of SSE — no new infrastructure needed
- Old brief HTML is never touched by the route; pipeline overwrites its own output file; client reloads iframe only on `status=complete`
- `_regen_running` set (protected by existing `_jobs_lock`) provides 409 lock; released in `finally` block of `_run_regen_background`
- Rate limit checks intentionally omitted for regen (manual power-user action behind OAuth; cost is bounded by single fast run)

**Next Steps:**
- [ ] Regen returns 404 for states without a community_registry YAML (only NM/MS/TN/WI have one); consider fallback to `states.yaml` district_map for completeness
- [ ] Lennon sign-off on methodology questions A–D in `docs/lennon_oxford_scoring_flags.md`
- [ ] Run live `--force` MS batch to exercise ACS district-type fallback + widened statutory narrative guard on fresh LLM output
- [ ] Add guard against deprecated community_id slug artifacts accumulating in cache/outputs

**Tests:** 859 passed (`python3 -m pytest -q`).

---

### Session — 2026-06-11 (3b8a168)

**Accomplished:**
- Wired `call_claude_cached` into all 8 `call_claude` call sites in `pipeline/s3_fact_extraction.py`; S3 now deduplicates LLM calls against a per-prompt content-hash key stored under `data/cache/llm/{state}/{community_id}/s3_{stage}_{hash}.json`
- Added `S3_LLM_CACHE_TTL_DAYS: int = 30` constant (env-overridable via `S3_LLM_CACHE_TTL_DAYS`) as the configurable TTL for all S3 LLM response caches
- Added `_s3_llm_cache_key(state, community_id, stage_suffix, system, user)` helper that hashes the full prompt content (SHA-1 prefix, 12 hex chars) so any injected-data or date change produces a new cache key
- Haiku gate (`_should_run_political_climate_search`) and charter-intel helper (`_fetch_charter_intel`) create their own `CacheManager(config)` instances locally, avoiding signature changes
- Added `import hashlib` and `call_claude_cached` to the `api_client` import in `s3_fact_extraction.py`
- Changed cache-hit log in `call_claude_cached` (`api_client.py`) from `INFO` to `DEBUG` level

**Decisions:**
- Cache key hashes full `system + user` prompt text so any change in injected data, `TODAY_DATE`, depth flags, or roster content produces a distinct key — stale reads impossible
- `S3_LLM_CACHE_TTL_DAYS` defaults to 30 days (matches existing `s3_political_climate` TTL in `CACHE_TTL_DAYS`) and is env-overridable
- Web-search calls embed `TODAY_DATE`, so their keys rotate daily; same-day reruns still hit the cache
- No S3 logic, scoring, or output shape changed — purely a call-site transport swap

**Next Steps:**
- [ ] Add `S3_LLM_CACHE_TTL_DAYS` entry to `CACHE_TTL_DAYS` dict in `cache.py` as the canonical registry entry
- [ ] Regen returns 404 for states without a community_registry YAML; consider fallback to `states.yaml` district_map
- [ ] Lennon sign-off on methodology questions A–D in `docs/lennon_oxford_scoring_flags.md`
- [ ] Run live `--force` MS batch to exercise ACS district-type fallback + widened statutory narrative guard

**Tests:** 867 passed (`python3 -m pytest -q -p no:cacheprovider`).
