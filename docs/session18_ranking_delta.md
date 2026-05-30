# Session 18 Ranking Delta

**⚠️ NOT FOR STRATEGIC USE — weights partially calibrated, operator profile conversation pending. Scoring weights are uncalibrated starting-point estimates. Do not use these rankings for real decisions.**

Generated: 2026-05-29 | Script: `scripts/session18_ranking_delta.py` | Read-only, no API calls.

---

## What this shows

Compares post-Session-18 bias-resolution scoring (current S5 logic) against pre-Session-18 cached scorecards for communities that have cached S4 bundles. Two key changes drive the differences:

- **`data_coverage_tier=INSUFFICIENT`** (Area C): Communities with ≥4 defaulted dimensions are flagged and excluded from the ranked table for strategic use. 18 of 42 communities affected.
- **`competitive_opportunity` demand-evidence gate** (Area D): High score requires waitlist, grade-band-gap, or whitespace signal; else clamped to 5.0 and excluded from composite. 22 of 42 communities affected.

---

## maturity_adjusted Preset — Full Rank Table

| Rank | Community | Score (new) | Score (old) | Δ | Coverage tier (new/old) | comp_opp (new/old) | Gated | INSUFFICIENT |
|------|-----------|-------------|-------------|---|--------------------------|---------------------|-------|--------------|
| 1  | nm-156-navajo              | 7.67 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 2  | nm-el-prado               | 7.67 | 7.67 | 0.0   | INSUFFICIENT/unreliable    | 5.0/5.0  | ✓ | ✓ |
| 3  | nm-navajo                 | 7.67 | 7.67 | 0.0   | INSUFFICIENT/unreliable    | 5.0/5.0  | ✓ | ✓ |
| 4  | nm-shiprock               | 7.67 | 7.67 | 0.0   | INSUFFICIENT/unreliable    | 5.0/5.0  | ✓ | ✓ |
| 5  | nm-4-jemez-pueblo         | 7.00 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 6  | nm-564-gallup             | 6.56 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 7  | **nm-gallup**             | **6.42** | 6.36 | +0.06 | reliable/reliable       | 5.0/6.0  | ✓ | — |
| 8  | **nm-questa**             | **6.42** | 6.42 | 0.0   | reliable/reliable       | 5.73/5.73|   | — |
| 9  | nm-4386-shiprock          | 6.35 | —    | —     | INSUFFICIENT/—             | 5.73/—   |   | ✓ |
| 10 | nm-edgewood               | 6.33 | 6.33 | 0.0   | INSUFFICIENT/INSUFFICIENT  | 5.0/5.0  | ✓ | ✓ |
| 11 | **nm-carlsbad**           | **6.31** | 6.31 | 0.0   | reliable/reliable       | 5.0/5.0  | ✓ | — |
| 12 | nm-alamogordo             | 6.24 | 6.24 | 0.0   | INSUFFICIENT/INSUFFICIENT  | 5.73/5.73|   | ✓ |
| 13 | **nm-nm-602-gallup**      | **6.12** | —    | —     | reliable/—              | 5.73/—   |   | — |
| 14 | nm-cleveland              | 6.08 | —    | —     | reliable/—              | 5.0/—    | ✓ | — |
| 15 | **nm-red-river**          | **5.86** | 5.86 | 0.0   | reliable/reliable       | 2.0/2.0  |   | — |
| 16 | nm-sandia-park            | 5.85 | 5.85 | 0.0   | reliable/reliable        | 5.0/5.0  | ✓ | — |
| 17 | nm-230-el-prado           | 5.67 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 18 | nm-jemez-pueblo           | 5.64 | 5.64 | 0.0   | INSUFFICIENT/provisional   | 5.0/5.0  | ✓ | ✓ |
| 19 | nm-bld.-2b-albuquerque    | 5.58 | —    | —     | INSUFFICIENT/—             | 7.2/—    |   | ✓ |
| 20 | nm-66-edgewood            | 5.50 | —    | —     | INSUFFICIENT/—             | 5.73/—   |   | ✓ |
| 21 | nm-aztec                  | 5.46 | 5.46 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 22 | nm-ste-c-albuquerque      | 5.46 | —    | —     | reliable/—               | 5.0/—    | ✓ | — |
| 23 | nm-angel-fire             | 5.45 | 5.45 | 0.0   | INSUFFICIENT/INSUFFICIENT  | 5.0/5.0  | ✓ | ✓ |
| 24 | nm-rio-rancho             | 5.40 | 5.40 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 25 | nm-las-cruces             | 5.31 | 5.31 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 26 | nm-deming                 | 5.28 | 5.28 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 27 | nm-los-ranchos-de-albuquerque | 5.28 | 5.28 | 0.0 | reliable/reliable      | 8.67/8.67|   | — |
| 28 | nm-us-70-alamogordo       | 5.28 | —    | —     | INSUFFICIENT/—             | 5.73/—   |   | ✓ |
| 29 | nm-los-lunas              | 5.25 | 5.36 | **-0.11** | reliable/reliable   | 5.0/6.0  | ✓ | — |
| 30 | nm-bldg.-2-albuquerque    | 5.22 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 31 | nm-socorro                | 5.18 | 5.18 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 32 | nm-truth-or-consequences  | 5.18 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 33 | nm-taos                   | 5.07 | 5.07 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 34 | nm-santa-fe               | 5.06 | 5.18 | **-0.12** | reliable/reliable   | 5.0/6.0  | ✓ | — |
| 35 | nm-344-edgewood           | 5.00 | —    | —     | INSUFFICIENT/—             | 5.0/—    | ✓ | ✓ |
| 36 | nm-roswell                | 4.97 | 4.97 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 37 | nm-east-taos              | 4.92 | 5.07 | **-0.15** | reliable/reliable   | 5.0/6.0  | ✓ | — |
| 38 | nm-las-vegas              | 4.89 | 4.89 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 39 | nm-w-taos                 | 4.89 | 4.89 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 40 | nm-silver-city            | 4.68 | 4.68 | 0.0   | reliable/reliable        | 5.73/5.73|   | — |
| 41 | nm-espanola               | 4.58 | 4.79 | **-0.21** | reliable/reliable   | 5.0/6.0  | ✓ | — |
| 42 | nm-albuquerque            | 4.55 | 4.55 | 0.0   | reliable/reliable        | 4.0/4.0  |   | — |

**Summary:** INSUFFICIENT tier: 18/42 | competitive_opportunity gated (Area D): 22/42

---

## growth Preset — Full Rank Table

| Rank | Community | Score (new) | Score (old) | Δ | Coverage tier (new/old) | INSUFFICIENT |
|------|-----------|-------------|-------------|---|--------------------------|--------------|
| 1  | nm-156-navajo              | 8.08 | 5.80 | **+2.28** | INSUFFICIENT/— | ✓ |
| 2  | nm-el-prado               | 8.08 | —    | —     | INSUFFICIENT/— | ✓ |
| 3  | nm-navajo                 | 8.08 | —    | —     | INSUFFICIENT/— | ✓ |
| 4  | nm-shiprock               | 8.08 | —    | —     | INSUFFICIENT/— | ✓ |
| 5  | nm-4-jemez-pueblo         | 7.69 | 6.32 | +1.37 | INSUFFICIENT/— | ✓ |
| 6  | nm-564-gallup             | 7.37 | 6.28 | +1.09 | INSUFFICIENT/— | ✓ |
| 7  | nm-edgewood               | 7.15 | —    | —     | INSUFFICIENT/— | ✓ |
| 8  | nm-gallup                 | 6.97 | —    | —     | provisional/—  | — |
| 9  | **nm-questa**             | **6.83** | —  | —     | reliable/—     | — |
| 10 | nm-4386-shiprock          | 6.76 | 5.79 | +0.97 | INSUFFICIENT/— | ✓ |
| 11 | nm-230-el-prado           | 6.67 | 5.82 | +0.85 | INSUFFICIENT/— | ✓ |
| 12 | nm-alamogordo             | 6.66 | —    | —     | INSUFFICIENT/— | ✓ |
| 13 | nm-nm-602-gallup          | 6.63 | 6.11 | +0.52 | provisional/—  | — |
| 14 | nm-cleveland              | 6.53 | 6.75 | -0.22 | provisional/reliable | — |
| 15 | nm-bld.-2b-albuquerque    | 6.51 | 5.92 | +0.59 | INSUFFICIENT/— | ✓ |
| 16 | nm-sandia-park            | 6.50 | —    | —     | reliable/—     | — |
| 17 | **nm-carlsbad**           | **6.43** | —  | —     | reliable/—     | — |
| 18 | nm-bldg.-2-albuquerque    | 6.35 | 5.66 | +0.69 | INSUFFICIENT/— | ✓ |
| 19 | **nm-red-river**          | **6.25** | —  | —     | reliable/—     | — |
| 20 | nm-66-edgewood            | 6.23 | 5.75 | +0.48 | INSUFFICIENT/— | ✓ |
| 21 | nm-us-70-alamogordo       | 6.17 | 5.71 | +0.46 | INSUFFICIENT/— | ✓ |
| 22 | nm-344-edgewood           | 6.14 | 5.68 | +0.46 | INSUFFICIENT/— | ✓ |
| 23 | nm-ste-c-albuquerque      | 6.14 | 6.01 | +0.13 | provisional/—  | — |
| 24 | nm-truth-or-consequences  | 6.14 | 5.66 | +0.48 | INSUFFICIENT/— | ✓ |
| 25 | nm-jemez-pueblo           | 6.08 | —    | —     | INSUFFICIENT/— | ✓ |
| 26 | nm-los-lunas              | 6.03 | —    | —     | provisional/—  | — |
| 27 | nm-angel-fire             | 6.02 | —    | —     | INSUFFICIENT/— | ✓ |
| 28 | nm-aztec                  | 5.98 | —    | —     | reliable/—     | — |
| 29 | nm-los-ranchos-de-albuquerque | 5.96 | — | —    | reliable/—     | — |
| 30 | nm-deming                 | 5.86 | —    | —     | reliable/—     | — |
| 31 | nm-rio-rancho             | 5.76 | —    | —     | reliable/—     | — |
| 32 | nm-las-cruces             | 5.70 | —    | —     | reliable/—     | — |
| 33 | nm-socorro                | 5.63 | —    | —     | reliable/—     | — |
| 34 | nm-east-taos              | 5.59 | —    | —     | provisional/—  | — |
| 35 | nm-taos                   | 5.54 | —    | —     | reliable/—     | — |
| 36 | nm-roswell                | 5.51 | —    | —     | reliable/—     | — |
| 37 | nm-las-vegas              | 5.43 | —    | —     | reliable/—     | — |
| 38 | nm-w-taos                 | 5.43 | —    | —     | reliable/—     | — |
| 39 | nm-silver-city            | 5.33 | —    | —     | reliable/—     | — |
| 40 | nm-espanola               | 5.29 | —    | —     | provisional/—  | — |
| 41 | nm-santa-fe               | 5.21 | —    | —     | reliable/—     | — |
| 42 | nm-albuquerque            | 4.40 | —    | —     | reliable/—     | — |

**Summary:** INSUFFICIENT tier: 18/42 | competitive_opportunity gated (Area D): 22/42

---

## Plain-Language Summary

The Session 18 bias-resolution pass made two structural changes that most visibly reshape the ranked output. First, 18 of 42 NM communities now carry `data_coverage_tier=INSUFFICIENT` because they have ≥4 scoring dimensions that defaulted to the midpoint (5.0) — these communities appear to rank high under both presets (nm-navajo, nm-el-prado, nm-shiprock all land at 7.67–8.08) but those scores are composed almost entirely of redistributed midpoints rather than real evidence, making them unreliable. Second, the competitive_opportunity demand-evidence gate (Area D) clamped 22 communities' competitive_opportunity score to 5.0 and excluded it from their composites; communities with confirmed waitlist data, grade-band gaps, or documented whitespace are unaffected (nm-questa, nm-aztec, nm-deming retain their demand signal). Communities that moved down meaningfully — nm-espanola (−0.21), nm-east-taos (−0.15), nm-santa-fe (−0.12), nm-los-lunas (−0.11) — all had their competitive_opportunity gate triggered, indicating their prior higher score rested on an unsupported high demand claim. The most important operator conversation to resolve before using these rankings is whether any of the currently-gated communities have verifiable demand evidence (waitlist enrollment, school-age enrollment trends relative to seat supply) that would un-gate their competitive_opportunity and change their rank.

---

## Top-5 Eligible Communities — maturity_adjusted (non-INSUFFICIENT, post-bias-pass)

| Rank (among eligible) | Community | Score | Coverage tier | comp_opp | Notes |
|-----------------------|-----------|-------|---------------|----------|-------|
| 1 | **nm-gallup** | 6.42 | reliable | 5.0 (gated) | comp_opp gated — verify demand evidence |
| 2 | **nm-questa** | 6.42 | reliable | 5.73 | demand signal present |
| 3 | **nm-carlsbad** | 6.31 | reliable | 5.0 (gated) | comp_opp gated — Carlsbad baseline from Session 17 ($0.303 actual) |
| 4 | **nm-nm-602-gallup** | 6.12 | reliable | 5.73 | demand signal present; artifact community — confirm not duplicate of nm-gallup |
| 5 | **nm-red-river** | 5.86 | reliable | 2.0 | very low comp_opp — thin market, small enrollment |

*Tribal jurisdiction flags: nm-gallup = PENDING_HUMAN_REVIEW. Requires operator legal/policy review before any strategic use involving communities with tribal flags.*

---

*Generated from `scripts/session18_ranking_delta.py` run 2026-05-29. Scoring weights are uncalibrated — operator profile conversation pending. Not for strategic use.*
