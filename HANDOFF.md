# Charter Intel Platform (CLIP) — Claude Code Handoff
# Session 9 | 2026-05-27

---

## Project
7-stage Python pipeline generating charter school market intelligence
briefs for individual cities. Primary use: The Mind Trust NM expansion
strategy.

Local: ~/Downloads/charter-intel/
GitHub: https://github.com/TLGMustard/charter-intel

---

## What Was Built This Session (Session 8)

### New files
| File | Purpose |
|---|---|
| `pipeline/utils/token_logger.py` | Token usage singleton — accumulates all API call data, writes CSV, prints per-stage cost summary |
| `data/logs/` | Token CSVs land here as `token_usage_{run_id}.csv` |
| `scripts/bust_cache.sh` | Clears community + synthesis cache for a single city by name |
| `scripts/generate_pdf.sh` | Converts most recent markdown brief to browser-printable HTML; opens automatically |

### Modified files
| File | Change |
|---|---|
| `pipeline/utils/api_client.py` | Calls `token_logger.log_call()` after every API call |
| `pipeline/utils/acs_fetcher.py` | **Two guards added:** (1) sentinel guard — returns `None` when majority of LTVW vars return negative Census suppression values; (2) zero-cell guard — returns `None` when all LTVW vars return 0 AND denominator < 1500 (small-district cell rounding) |
| `pipeline/s3_fact_extraction.py` | Address artifact stripping (`_strip_address_artifacts`); sets `ell_data_suppressed=True` on `facts_output` when ACS returns `None` |
| `pipeline/s6_synthesis.py` | `_inject_ell_gap_disclosure()` — reads `ell_data_suppressed` from S3 cache post-generation, appends standard Needs Verification entry; called after `_inject_charter_intel()` |
| `pipeline/s7_render.py` | Dynamic charter schools section header in `_render_fallback()` |
| `pipeline/s1_discovery.py` | `_strip_address_artifacts(city)` — strips leading numbers, highway refs (US-70, NM Hwy 564), building/suite codes (Bld. 2B, Ste C) from parsed city slugs |
| `main.py` | Token logger integration: generates `run_id`, calls `token_logger.finalize()` after pipeline |
| `templates/strategic_brief.md.j2` | Dynamic charter schools header: 0 schools = section omitted; 1 = "Top Charter School in Market"; 2–4 = "Top N Charter Schools in Market"; 5 = "Top 5 Charter Schools in Market" |
| `prompts/extract/s3_political_climate.md` | Source relevance guidance integrated into CRITICAL RULE #2: only cite sources with direct geographic/topical connection to the community |

---

## Design Decisions Made This Session

### Source relevance filter — prompt-level, not Python-level
The Pecos Cyber Academy bug (statewide virtual charter cited as Carlsbad political
climate source) was initially fixed with a Python pre-filter (`_filter_relevant_sources`).
This was replaced with a prompt-level instruction in `s3_political_climate.md` CRITICAL
RULE #2. Reason: the Python filter ran after the API call on the returned JSON and
couldn't affect what the model cited during web search. The prompt instruction prevents
irrelevant sourcing at the point where the model selects sources.

### ELL data gap disclosure — programmatic, not prompt-level
The synthesis prompt's GROUNDING RULE ("ONLY reference facts in VERIFIED FACTS") has
higher precedence than anything in FINAL RULES. A prompt-based ELL gap instruction was
tested and failed — the model correctly obeyed the anti-hallucination rule and omitted
the entry. Post-generation injection via `_inject_ell_gap_disclosure()` is correct here:
data-absence disclosures are pipeline metadata, not model-synthesized claims.

### ACS ELL guards — two separate cases
- **Sentinel guard** (threshold = 4/8 vars): handles Census suppression of small districts
  (negative values like -666666666). Questa triggers this.
- **Zero-cell guard** (threshold = denominator < 1500): handles ACS cell-level rounding
  that produces all-zero rows. Questa triggers this too. Carlsbad (denominator = 7,242)
  does not trigger either guard — its 0.0% ELL is genuine Census data.

---

## Community List State (29 clean communities)

Running `python3 main.py --state NM --all ... --dry-run` after deleting
`data/processed/nm/community_list.json` now produces **29 clean communities**.
Key merges from address artifact stripping:

| Before | After | Notes |
|---|---|---|
| nm-bld.-2b-albuquerque + nm-bldg.-2-albuquerque + nm-ste-c-albuquerque | nm-albuquerque (58 schools) | Building/suite codes stripped |
| nm-us-70-alamogordo | nm-alamogordo | Highway ref stripped |
| nm-156-navajo | nm-navajo | PO Box number stripped |
| nm-4386-shiprock | nm-shiprock | PO Box number stripped |
| nm-230-el-prado | nm-el-prado | Highway number stripped |
| nm-344-edgewood + nm-66-edgewood | nm-edgewood (2 schools) | Merged |
| nm-564-gallup + nm-nm-602-gallup | nm-gallup (3 schools) | Merged |
| nm-4-jemez-pueblo | nm-jemez-pueblo (2 schools) | Merged |

---

## Current Pipeline State

### Data sources — all wired
| Source | Status |
|---|---|
| PED charter roster CSV (107 schools, 29 clean communities) | ✅ Runtime |
| PED ELA/Math proficiency CSVs | ✅ Runtime |
| NCES finance data | ✅ Runtime |
| NCES free/reduced lunch | ✅ Runtime |
| Census ACS API (ELL%) | ✅ Runtime — requires CENSUS_API_KEY |
| Web search (political climate + charter intel) | ✅ standard + deep depth only |

### Environment variables required
```
ANTHROPIC_API_KEY=
CENSUS_API_KEY=
```
Both in `.env` at project root. `load_dotenv(override=True)` in `main.py`.

### Token logger pricing constants (verify before budget decisions)
- claude-sonnet-4-5: $3/$15 per MTok in/out
- claude-haiku-4-5-20251001: $0.80/$4 per MTok in/out
- claude-opus-4-5: $15/$75 per MTok in/out

### Typical run cost (--depth standard, single city)
~$0.55 for Carlsbad (1 school). Scales with school count and web search depth.
Full statewide fast scan: see `data/logs/` CSVs.

### Scoring dimensions
| Dimension | Source |
|---|---|
| academic_need | PED proficiency CSVs |
| charter_saturation | PED roster |
| population_trends | ⚠️ DEFAULTING TO 5 |
| political_climate | Web search |
| authorizer_friendliness | Claude knowledge (LOW) |
| facilities_feasibility | ⚠️ DEFAULTING TO 5 |
| replication_feasibility | PED roster |
| funding_environment | NCES finance |
| competitive_opportunity | PED roster |
| operational_complexity | NCES lunch + ACS ELL |

⚠️ Scoring weights uncalibrated. Do not use scores for real strategic
decisions until calibration is complete.

---

## Commands

```bash
# Single city
python3 main.py "Santa Fe" --depth standard

# Statewide fast scan
python3 main.py --state NM --all --preset growth --mode 2 --depth fast

# Re-run synthesis only (S6 + S7 from cached S3–S5)
python3 main.py "Santa Fe" --stages s6_synthesis,s7_render

# Bust S6 cache only (re-synthesize from existing facts)
rm -f data/cache/synthesis/nm/nm-CITY/s6_brief_growth_mode2.json
python3 main.py "CITY" --stages s6_synthesis,s7_render

# Full cache bust + re-run (utility script)
bash scripts/bust_cache.sh "Santa Fe"
python3 main.py "Santa Fe" --depth standard

# Generate print-ready HTML from most recent markdown brief
bash scripts/generate_pdf.sh "Santa Fe"
bash scripts/generate_pdf.sh nm-santa-fe   # slug form also accepted

# Delete stale community list and re-discover (after roster changes)
rm -f data/processed/nm/community_list.json
python3 main.py --state NM --all --depth fast --dry-run
```

---

## Known Issues (Priority Order)

1. **Web search token bloat — NOT YET FIXED (next session top priority)**
   `s3_fact_extraction_charter_intel` and `s3_fact_extraction_political_climate`
   consume 46k–52k input tokens at `--depth standard` due to Anthropic web search
   tool results injected server-side. A `_truncate_web_results` helper and
   `_build_audit_payload` audit trim were approved but the session was interrupted
   before implementation. See "Approved but not implemented" section below.

2. **Scoring weights uncalibrated** — all weights are starting-point estimates.
   Hand-score 5 cities, compare to pipeline output, adjust `config/scoring_weights.yaml`.

3. **population_trends defaulting to 5** — needs multi-year NCES membership files
   (2020–2024). Single-year already downloaded.

4. **facilities_feasibility defaulting to 5** — no structured source identified yet.

5. **nces_district_map in states.yaml has stale junk entries** — entries for
   nm-bld.-2b-albuquerque, nm-bldg.-2-albuquerque, nm-ste-c-albuquerque,
   nm-us-70-alamogordo, nm-156-navajo, etc. still exist. They don't cause errors
   (unmatched keys are ignored) but should be replaced with canonical IDs
   (nm-albuquerque, nm-alamogordo, nm-navajo, nm-shiprock, nm-el-prado, nm-edgewood).

6. **HTML template skeleton** — `templates/strategic_brief.html.j2` renders blank
   for markdown-sourced briefs (template expects S6 JSON structure). Workaround:
   `bash scripts/generate_pdf.sh "City Name"` produces a correctly styled HTML.

7. **S6 output_path=None** — cosmetic log warning, no functional impact.

8. **Schema warnings on long text fields** — cosmetic, no functional impact.

---

## Approved But Not Yet Implemented (Next Session)

These two fixes were specified and approved in Session 8 but the session was
interrupted before implementation. Implement them first in Session 9.

### Fix 1 — Web search token reduction

In `pipeline/s3_fact_extraction.py`, add near the top:

```python
_WEB_RESULT_MAX_CHARS = 600

def _truncate_web_results(results: list[dict]) -> list[dict]:
    """Truncate each web result's content field to reduce prompt bloat.
    Preserves url and title in full — only content/snippet is capped."""
    truncated = []
    for r in results:
        r = dict(r)
        for field in ("content", "snippet", "text", "body"):
            if field in r and isinstance(r[field], str):
                if len(r[field]) > _WEB_RESULT_MAX_CHARS:
                    r[field] = r[field][:_WEB_RESULT_MAX_CHARS] + "…"
        truncated.append(r)
    return truncated
```

Call it on the raw results list for both web search blocks (`charter_intel` and
`political_climate`) immediately before results are serialized into the prompt string.
Do not truncate `url` or `title` fields.

**Note from investigation:** The Anthropic `web_search_20250305` tool injects results
server-side as tool_result blocks — we don't see the raw result content as Python
objects before the API call. The large token counts (46k–52k) come from those
server-injected results. The `_truncate_web_results` function applies to the
`CSV_SCHOOLS_JSON` / `CSV_AUTHORIZERS_JSON` data we inject into the `charter_intel`
prompt (the charter_intel base prompt for Albuquerque's 58 schools is ~10k tokens).
The investigation was interrupted before determining the right truncation point.
**Start Session 9 by completing this investigation and implementing the fix.**

### Fix 2 — S6 audit prompt trim

In `pipeline/s6_synthesis.py`, add a helper that strips non-essential fields before
passing the brief to the audit:

```python
def _build_audit_payload(synthesis_output: dict) -> dict:
    """Extract only the fields the audit actually needs to check.
    Strips executive snapshot prose, quick reads, facilities narrative,
    needs_verification, schools_to_watch."""
    return {
        "scorecard_summary": synthesis_output.get("scorecard_summary"),
        "recommendations":   synthesis_output.get("recommendations"),
        "sources":           synthesis_output.get("sources"),
    }
```

Pass `_build_audit_payload(brief_json)` to `_run_audit()` instead of the full
`brief_json`. This cuts audit input tokens by ~46% (measured: 3,164 → 1,697 tokens
for the brief payload portion).

**Token targets after both fixes:**
- `s3_fact_extraction_charter_intel` input tokens < 15,000
- `s3_fact_extraction_political_climate` input tokens < 15,000
- `s6_synthesis_audit` input tokens < 8,000

If `_WEB_RESULT_MAX_CHARS = 600` makes the Questa political climate section thin,
bump to 900 and re-run before treating the number as final.

---

## Session 9 Roadmap

* [ ] **Implement web search truncation (Fix 1 above)** ← next
* [ ] **Implement S6 audit trim (Fix 2 above)** ← next
* [ ] Verify both token targets are hit on Questa --depth standard
* [ ] Commit all uncommitted session 8 changes (5 modified files)
* [ ] Weight calibration (hand-score 5 cities)
* [ ] Clean up stale nces_district_map entries in states.yaml
* [ ] Statewide fast scan review (re-run if needed after fixes)
* [ ] Build comparison table output (ranked, single-page)
* [ ] Fix HTML template (or document generate_pdf.sh as the canonical workaround)
* [ ] Add second state (Texas or Indiana)
* [ ] Final NM deliverable package (top 5 cities + methodology note)

---

## Uncommitted Changes (commit before starting Session 9 fixes)

```bash
git add pipeline/s1_discovery.py \
        pipeline/s3_fact_extraction.py \
        pipeline/s6_synthesis.py \
        pipeline/s7_render.py \
        templates/strategic_brief.md.j2
git commit -m "fix: ELL gap disclosure, dynamic charter header, address artifact stripping, token logger wiring"
```

---

## Standing Rules — Always Follow

* Do not redesign the architecture
* Do not add new pipeline stages without explicit instruction
* Do not modify scoring weights without human calibration
* Do not use scores for real strategic decisions until calibration
* Do not create files outside /charter-intel/ without asking
* Do not re-litigate architecture decisions
* Flag contact information in briefs as requiring human verification
  before any external use

---

## Disclosure

Built with Claude assistance. All code requires human review.
Scoring weights uncalibrated — not for real strategic decisions yet.
