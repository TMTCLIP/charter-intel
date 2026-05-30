# Session 18 Cost Baseline

Post-gate statewide scan cost estimate — established 2026-05-29.

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Communities scanned (post-exclusion) | 25 |
| Depth | standard |
| Preset | maturity_adjusted |
| Pre-gate baseline (Session 17, batch) | $0.244/city |
| Post-gate estimate (Session 18, non-batch, gate=YES) | $0.1577/city |
| Post-gate estimate (Session 18, non-batch, gate=NO) | $0.1142/city |
| Total 25-city estimate (all gate=YES) | $3.94 |
| Total 25-city estimate (all gate=NO) | $2.86 |
| Gate savings if all communities skip | $1.09 (28%) |
| Single-city synchronous baseline (Carlsbad, Session 17) | $0.303 |

> Note: all Session 18 estimates are **heuristic** (derived from nm-questa token counts). Actual cost varies ±30–50% by community. Session 17 $0.244 figure used `--batch` (50% discount); Session 18 estimates do NOT apply the batch discount — apply ×0.5 for batch-mode projection.

---

## Per-City Breakdown

Command: `python3 main.py --state NM --all --depth standard --preset maturity_adjusted --dry-run`

| Community | Depth | Gate (est) | Est. cost |
|-----------|-------|-----------|-----------|
| nm-alamogordo | standard | YES (est) | $0.1577 |
| nm-albuquerque | standard | YES (est) | $0.1577 |
| nm-angel-fire | standard | YES (est) | $0.1577 |
| nm-aztec | standard | YES (est) | $0.1577 |
| nm-carlsbad | standard | YES (est) | $0.1577 |
| nm-deming | standard | YES (est) | $0.1577 |
| nm-edgewood | standard | YES (est) | $0.1577 |
| nm-el-prado | standard | YES (est) | $0.1577 |
| nm-espanola | standard | YES (est) | $0.1577 |
| nm-gallup | standard | YES (est) | $0.1577 |
| nm-jemez-pueblo | standard | YES (est) | $0.1577 |
| nm-las-cruces | standard | YES (est) | $0.1577 |
| nm-las-vegas | standard | YES (est) | $0.1577 |
| nm-los-lunas | standard | YES (est) | $0.1577 |
| nm-navajo | standard | YES (est) | $0.1577 |
| nm-questa | standard | YES (est) | $0.1577 |
| nm-red-river | standard | YES (est) | $0.1577 |
| nm-rio-rancho | standard | YES (est) | $0.1577 |
| nm-roswell | standard | YES (est) | $0.1577 |
| nm-sandia-park | standard | YES (est) | $0.1577 |
| nm-santa-fe | standard | YES (est) | $0.1577 |
| nm-shiprock | standard | YES (est) | $0.1577 |
| nm-silver-city | standard | YES (est) | $0.1577 |
| nm-socorro | standard | YES (est) | $0.1577 |
| nm-taos | standard | YES (est) | $0.1577 |

**Total (25 communities, gate=YES): $3.94**

Gate column shows `YES (est)` for all communities because: after Session 18 Review Fix 2, the rule-based skip no longer fires in normal pipeline runs (known_schools is always `[]`, never `None`). All communities go through the Haiku gate at standard depth. The actual gate decision requires a live Haiku API call — not available in dry-run mode.

---

## Gate YES / NO Distribution (Dry-Run Estimate)

| | Count | Est. cost |
|--|-------|-----------|
| Gate YES (est) | 25 | $3.94 |
| Gate NO (est) | 0 | $2.86 (floor if all skip) |

Gate savings per community when it fires: **$0.0435/city** (the political_climate Sonnet web search skipped).

In production runs, actual gate distribution will vary. If ~50% of communities are gated (gate=NO), the expected total cost would be approximately **$3.40** ($3.94 − 12.5 × $0.0435).

---

## Cost Component Breakdown (per city)

| Stage | Model | Est. input tokens | Est. output tokens | Cost/city |
|-------|-------|------------------|--------------------|-----------|
| S3 fact extraction (main) | Sonnet | 3,700 | 3,600 | $0.0651 |
| S3 political_climate web search | Sonnet | 4,500 | 2,000 | $0.0435 *(gate-dependent)* |
| S4 verification | Haiku | 3,000 | 800 | $0.0056 |
| S6 synthesis | Haiku | 8,000 | 5,000 | $0.0264 |
| S6 audit pass | Haiku | 6,000 | 3,000 | $0.0168 |
| Haiku gate (political_climate) | Haiku | ~50 | ~50 | $0.0003 |
| **Total (gate=YES)** | | | | **$0.1577** |
| **Total (gate=NO)** | | | | **$0.1142** |

---

## Comparison: Pre-Gate vs Post-Gate Baselines

| Session | Mode | Gate | Cost/city | Notes |
|---------|------|------|-----------|-------|
| Session 17 (batch) | batch | No gate | $0.244 | Per-city with 50% batch discount |
| Session 17 (non-batch) | sync | No gate | ~$0.487 | Extrapolated from batch figure |
| Session 18 dry-run | sync | YES (est) | $0.1577 | Post-gate, non-batch |
| Session 18 dry-run | sync | NO | $0.1142 | Post-gate, gate fires, non-batch |
| Session 18 batch projection | batch | YES (est) | ~$0.079 | ×0.5 batch discount applied to $0.1577 |
| Session 17 single-city (Carlsbad) | sync | — | $0.303 | Actual measured cost, Session 17 |

**Primary cost driver of Session 17→18 reduction:** Session 15 moved S6 from Sonnet ($3.00/$15.00 per M tokens) to Haiku ($0.80/$4.00 per M tokens), reducing S6 synthesis cost from ~$0.099/city to ~$0.026/city and S6 audit from ~$0.072/city to ~$0.017/city. The Haiku political-climate gate (Session 18, Area B) adds a further ~$0.044/city saving when the gate fires — meaningful in aggregate but secondary to the S6 model switch.

---

## Interpretation

The post-gate, post-Haiku-S6 cost of **$0.1577/city** (standard depth, non-batch) represents a ~68% reduction versus the pre-Session-15 non-batch baseline ($0.487/city). The Session 18 bias-resolution pass itself did not add material API cost — the political_climate Haiku gate was the main cost change, and it saves money rather than spending it. The gate introduces a ~$0.0003/community Haiku call but saves ~$0.0435/community whenever a community is skipped (a 145:1 return on cost). A 25-city statewide scan at standard depth is projected to cost **$3.94** at most (all gate=YES) and **$2.86** at floor (all gate=NO), with the actual likely outcome somewhere in between depending on which communities have meaningfully distinct local charter politics. Adding the `--batch` flag would bring these totals to approximately **$1.97–$1.43**, consistent with the Session 17 trend.

---

*Generated: 2026-05-29. Heuristic estimates — actual ±30–50%. Verify against token_logger output after a live run.*
