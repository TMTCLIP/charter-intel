# SESSION STATE — charter-intel
_Last updated: 2026-05-22_

---

## Completed Work

### Schema Fix
- `schemas/datapoint.schema.json` — `fact_key` added to `properties`
- Was missing entirely; S5 depends on it for all fact lookups
- `additionalProperties: false` remains in place

### Synthetic Pipeline (S3 → S4 → S5)
Four new files created and confirmed working:

| File | Role |
|---|---|
| `pipeline/s3_extract.py` | Emits 14 synthetic facts with `fact_key` |
| `pipeline/s4_verify.py` | Blocks ADVOCACY / SELF_REPORTED / LOW confidence facts |
| `pipeline/s5_score.py` | Deterministic scoring, inline thresholds, no API calls |
| `pipeline/run_pipeline.py` | Entry point — chains S3→S4→S5, prints output |

### Confirmed Output
```
python3 pipeline/run_pipeline.py
→ S3: 14 facts
→ S4: 12 in_main_analysis, 2 blocked
→ S5: composite 6.35 — MODERATE OPPORTUNITY
```

---

## Existing Files (key)

```
pipeline/
  __init__.py                  # shared types: Confidence, StageResult, PipelineConfig, etc.
  run_pipeline.py              # ✅ new — synthetic entry point
  s3_extract.py                # ✅ new — synthetic facts
  s4_verify.py                 # ✅ new — enforcement pass
  s5_score.py                  # ✅ new — deterministic scoring
  s3_fact_extraction.py        # real S3 — calls Claude Sonnet, not yet runnable
  s4_verification.py           # real S4 — calls Claude Haiku, not yet runnable
  s5_scoring.py                # real S5 — loads YAML configs, not yet runnable
  s1/s2/s6/s7_*.py             # not yet tested
  utils/{api_client, cache, schema_validator}.py
schemas/
  datapoint.schema.json        # ✅ patched — fact_key now defined
  scorecard.schema.json
  community.schema.json
  brief.schema.json
config/
  scoring_weights.yaml         # 10 dimensions, thresholds, fact_key mappings
  scoring_presets.yaml         # weights by operator preset (growth/replication/turnaround)
  sources.yaml                 # source class rules, URL patterns
  pipeline.yaml
prompts/extract/s3_community_facts.md   # full fact_key spec for real S3 LLM call
tests/fixtures/
  scorecard_riverdale.json
  brief_riverdale_mode2.md
```

---

## Remaining Next Steps

1. Wire real `s3_fact_extraction.py` → `s4_verification.py` → `s5_scoring.py` against live data
2. Create `tests/synthetic_pipeline_test.py` using real stage modules (no API calls, dry_run=True)
3. Validate `scorecard_riverdale.json` fixture against `scorecard.schema.json`
4. Run S1/S2 for a real community to populate `data/cache/`
5. Test `s6_synthesis.py` and `s7_render.py`

---

## Current Transition State

Moving from synthetic pipeline validation to first real NM pilot execution.

Identified blockers for real S3 execution:
- Python dependencies not installed yet
- `ANTHROPIC_API_KEY` not configured
- Missing:
  - `data/cache/state/nm/s2_state_context.json`
  - `pipeline/run_s3_pilot.py`

Synthetic pipeline remains green:
- S3 → S4 → S5 validated
- 7/7 contract tests passing
- deterministic scoring preserved

Architectural invariants unchanged:
- `fact_key` preserved across stages
- S5 remains deterministic
- `additionalProperties: false` still enforced
- no architecture redesigns performed

---

## Architectural Invariants — Do Not Change

- `fact_key` must be present on every fact and flow unchanged through S3 → S4 → S5
- `in_main_analysis` is set by S3 prompt output and **re-enforced** by S4 — both gates are intentional
- S5 (`s5_scoring.py`) is **deterministic** — no LLM calls, ever
- `additionalProperties: false` stays on `datapoint.schema.json`
- Stage modules never import each other — all shared types go through `pipeline/__init__.py`
- `fact_key` and `datapoint_id` are distinct: `datapoint_id` = unique record ID; `fact_key` = semantic scoring key
