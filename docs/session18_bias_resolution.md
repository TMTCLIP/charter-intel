# Session 18 — Bias Resolution Pass

Single-pass resolution of structural biases surfaced by the read-only Opus audit
of the CLIP pipeline (Charter Community Landscape Intelligence Platform), built for
The Mind Trust's New Mexico charter strategy.

**Constraints honored:** no scoring-weight changes (`scoring_weights.yaml` /
`scoring_presets.yaml` untouched); no new stages or architecture redesign; no
fabricated facts (external-fact gaps are scaffolded with `# TODO: requires verified
source`); no rollback of prior gate wiring. All 108 pre-existing tests stay green;
new tests were added for every new flag/gate.

---

## What changed, by area

| Area | Change | Files |
|---|---|---|
| A | Web-search fallback honesty: a "5" backed by no evidence is now LOW/NOT_FOUND (excluded from composite), distinct from a thin-rural-market PROVISIONAL 5 and an evidence-backed 5. Central resolver `_resolve_websearch_status`. | `pipeline/s3_fact_extraction.py`, 5 S3 prompts |
| B | Political-climate gate: baseline injection on gate-skip now carries LOW confidence, a `DATA GAP:` rationale, and PROVISIONAL status (no silent confident 5). `no_charters` uses `is None`, not falsy. | `pipeline/s3_fact_extraction.py` |
| C | **`data_coverage_tier = INSUFFICIENT`** override when ≥4 dimensions defaulted — the composite is then mostly redistributed midpoints. Pulled out of the S7 ranked table; banner on the brief. | `pipeline/s5_scoring.py`, `schemas/scorecard.schema.json`, `templates/scan_results.html.j2`, `templates/strategic_brief.*.j2` |
| D | **competitive_opportunity demand-evidence gate**: a score above midpoint requires a real demand signal (waitlist / grade-band gap / geographic whitespace). Without one, the high score is a `default_to_midpoint` artifact → clamped to 5.0, LOW confidence, excluded from composite. **Affects rankings — operator sign-off required.** | `pipeline/s5_scoring.py` |
| E | Brief honesty notes (BLOCKER 4b): deterministic `recommendation_confidence_gate` + `data_coverage_tier` injected into the brief. Mode-2 prompt no longer forces "exactly 2" confident recs — under INSUFFICIENT/unreliable/LOW coverage, an honest "verify X and Y before deciding" recommendation is correct, not a failure. | `schemas/brief.schema.json`, `pipeline/s6_synthesis.py`, `prompts/synthesize/s6_mode2_strategic_brief.md`, `templates/strategic_brief.*.j2` |
| F | Tribal jurisdiction gate (BLOCKER 5a): config-driven `tribal_jurisdiction_flag` (FLAGGED / REVIEW_NEEDED) injected into the brief + a HIGH-impact `needs_verification` entry + brief banner. Scoring does not model tribal charter authority. | `config/states.yaml`, `schemas/brief.schema.json`, `pipeline/s6_synthesis.py`, `templates/strategic_brief.*.j2` |
| G | `teacher_supply_warning` scaffold (BLOCKER 5b): statewide NM teacher-shortage warning surfaced into every NM brief's `needs_verification` at impact HIGH. Per-community hard-to-staff status is **not** asserted — `# TODO: requires verified source` (NMPED shortage-area list). | `config/states.yaml`, `pipeline/s6_synthesis.py` |
| H | `frl_compressed` flag set when FRL ≥ 80% (ceiling-compression signal). | `pipeline/utils/nces_fetcher.py`, `pipeline/s3_fact_extraction.py`, `schemas/datapoint.schema.json` |
| I | Sub-district artifacts (e.g. building-level "communities") added to `excluded_communities` with rationale. | `config/states.yaml` |
| J | Rural thin-market branches added to S3 prompts (market_thin ⇒ PROVISIONAL "availability unknown, not favorable"). | 3 S3 prompts |
| K | PEC geographic-authorization pattern: S2 must not assert PEC historical authorization geography as fact without a URL; otherwise add an explicit `needs_verification` entry, treat as PROVISIONAL. | `prompts/extract/s2_state_context.md` |
| L | Authorizer-type default logs a warning when an authorizer falls back to `LEA` (may misclassify a state/tribal authorizer). | `pipeline/utils/authorizers_fetcher.py` |

All fixes touch confidence labels, flags, gates, prompts, schema, and logging —
**never scoring weights.**

---

## No-cost ranking-delta check (Areas C + D)

Reproduce with `python3 scripts/session18_ranking_delta.py` (reads cached S4
bundles + cached scorecards + config; writes nothing; no API calls). Re-scores all
42 cached communities in memory under the post-Session-18 S5 logic.

**Headline findings:**

- **18 / 42 communities are now `INSUFFICIENT`** (≥4 defaulted dimensions). Under
  the old `maturity_adjusted` ranking, the **top four** communities by composite
  (`nm-156-navajo`, `nm-el-prado`, `nm-navajo`, `nm-shiprock`, all ~7.67) were in
  fact data-poor and several are tribal / sub-district artifacts. They now carry
  the INSUFFICIENT tier and are pulled out of the ranked table — the single most
  important honesty correction in this pass.
- **22 / 42 communities had `competitive_opportunity` clamped** from 6.0/8.0 → 5.0
  by the Area-D demand-evidence gate (no waitlist / grade-band / whitespace signal
  behind the high `demand_supply_gap_index`). Where a clean before/after is
  available (recent `maturity_adjusted` scorecards), composite shifts are small and
  downward: roughly −0.1 to −0.2 (e.g. `nm-espanola` −0.21, `nm-east-taos` −0.15,
  `nm-santa-fe` −0.12). This removes inflated "opportunity" with no evidence behind it.

**Caveat on the `growth`-preset deltas:** many cached `growth` scorecards predate
other historical scoring changes, so their large positive deltas (e.g. +2.28)
conflate stale-cache drift with Session 18 effects and should **not** be read as
caused by this pass. The `maturity_adjusted` deltas are the clean signal.

---

## Items requiring human review / sign-off

1. **competitive_opportunity demand-evidence gate (Area D) — operator sign-off.**
   This changes rankings (22/42 communities clamped). Confirm the demand-signal set
   `{known_waitlist_data, grade_band_gaps, geographic_whitespace}` is the right bar
   for "opportunity above midpoint."
2. **Sub-district / artifact exclusions (Area I) — operator confirmation.** Confirm
   the building-level and artifact `community_id`s added to `excluded_communities`
   should indeed be excluded.
3. **Open verified-source TODOs (must not be invented):**
   - NMPED teacher shortage-area / hard-to-staff list → populate per-community
     `teacher_supply_warning` (Area G).
   - Tribal jurisdiction determinations for `REVIEW_NEEDED` communities
     (`nm-gallup`, `nm-espanola`) → confirm/clear the flag (Area F).
   - PEC historical geographic authorization pattern (Area K).
   - Political-climate baseline `political_climate_index` source (Area B scaffold).

---

## Tests

- 108 pre-existing tests remain green.
- New: `tests/unit/test_s5_scoring.py::TestCompetitiveOpportunityGate` (4),
  `::TestDataCoverageInsufficient` (2),
  `tests/unit/test_s6_injections.py` (11),
  `tests/unit/test_s3_websearch_status.py` (6).

---

## Review Fixes (post-pass)

Four targeted fixes applied after the main pass, based on post-review of the bias resolution work.

### Fix 1 — `geographic_whitespace` removed from demand-signal gate (`pipeline/s5_scoring.py`)

`geographic_whitespace` was removed from `_COMPETITIVE_DEMAND_SIGNAL_KEYS`. It is
itself a web-search-derived field subject to the same no-evidence→5 fallback that
Area D was correcting — so it could not serve as a gate signal without circular
reasoning. The qualifying set is now `{known_waitlist_data, grade_band_gaps}` only.
Added pass-through logging when the gate is satisfied (naming which signal triggered
it) and updated the clamp driver string. Added test:
`test_geographic_whitespace_alone_does_not_satisfy_gate`.

### Fix 2 — Political-climate gate cost note (`pipeline/s3_fact_extraction.py`)

Added a `# NOTE:` comment in the rule-based skip block documenting that
`no_charters` is always False in normal pipeline operation (S1 initialises
`known_schools` as `[]`, never `None`), so the zero-cost skip never fires and every
community at `--depth standard` reaches the Haiku gate. Estimated incremental cost:
~$0.008/statewide NM scan. No behavior change — comment only.

### Fix 3 — Gallup / Española `PENDING_HUMAN_REVIEW` state

Added a third `tribal_jurisdiction_flag` status (`PENDING_HUMAN_REVIEW`) for
communities where a human determination of tribal jurisdiction has **not yet been
made** (distinct from FLAGGED = strong signal, or REVIEW_NEEDED = probable). This
surfaces a yellow warning banner rather than the red blocker banner.

Changed in `config/states.yaml`: `nm-gallup` and `nm-espanola` from `REVIEW_NEEDED`
→ `PENDING_HUMAN_REVIEW`. Neither flag asserts sovereignty — both mark an open
determination.

`_inject_tribal_jurisdiction_flag` now handles all three statuses; `PENDING_HUMAN_REVIEW`
injects `impact_if_wrong: MODERATE` (vs HIGH for FLAGGED/REVIEW_NEEDED).

Schema: `schemas/brief.schema.json` tribal_jurisdiction_flag status enum updated.
Templates: `strategic_brief.{html,md}.j2` render distinct yellow banner for
`PENDING_HUMAN_REVIEW`. Tests: updated `test_review_needed_community_is_flagged` →
`test_pending_human_review_is_injected_with_moderate_impact`; added
`test_pending_human_review_espanola`.

### Fix 4 — `docs/data_sources.md` written

Documented all active pipeline data sources by reading the fetcher code directly.
Sources: NCES CCD (finance/lunch, enrollment, schools), NM PED (charter roster,
proficiency CSVs), Census ACS B16004 (ELL), Census TIGER ZCTA shapefile,
Transitland API, Anthropic web search + synthesis API, and static config
(states.yaml, scoring YAMLs). Known-gaps table includes open TODOs surfaced by
Session 18 (NMPED hard-to-staff list, tribal determinations, PEC authorization
geography, SAIPE pending, EDFacts pending download approval).
