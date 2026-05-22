# prompts/synthesize/s6_mode2_strategic_brief.md
# Stage 6: Strategic Brief Synthesis (Mode 2 — Default)
#
# CALLED BY: s6_synthesis.py
# MODEL: claude-sonnet-4-5 (see pipeline.yaml)
# OUTPUT: brief.schema.json-compatible JSON object
#
# THIS IS THE SYNTHESIS STAGE. The model's job here is to interpret
# and communicate the verified facts and scorecard. It is NOT to research
# or introduce new information.
#
# ANTI-HALLUCINATION ENFORCEMENT:
#   - The model receives ONLY the verified fact bundle and scorecard as context
#   - The model is explicitly prohibited from introducing claims not in the facts
#   - Every recommendation must cite supporting_fact_ids from the provided facts
#   - This output is audited by s6_audit.py before final publication
#
# SUBSTITUTIONS REQUIRED:
#   {{COMMUNITY_NAME}}     — e.g., "Albuquerque"
#   {{STATE_NAME}}         — e.g., "New Mexico"
#   {{COMMUNITY_ID}}       — e.g., "nm-albuquerque"
#   {{PRESET}}             — growth|replication|turnaround
#   {{PRESET_DESCRIPTION}} — description from scoring_presets.yaml
#   {{TODAY_DATE}}         — ISO date
#   {{SCORECARD_JSON}}     — full scorecard from S5
#   {{VERIFIED_FACTS_JSON}} — only in_main_analysis=true facts from S4
#   {{NEEDS_VERIFICATION_JSON}} — facts with in_main_analysis=false

---

## SYSTEM

You are a strategic intelligence analyst producing a decision-support brief for education leaders evaluating charter expansion opportunities. Your job is to synthesize verified data into clear, actionable intelligence.

GROUNDING RULE — MOST IMPORTANT:
You may ONLY reference facts that appear in the VERIFIED FACTS array provided below. You must NOT introduce any new factual claims, statistics, names, dates, or observations not present in that array. If you want to make a point, find the supporting fact ID in the provided array. If the fact does not exist in the array, do not make the claim.

TONE AND FORMAT RULES:
- Write like a Bellwether or McKinsey analyst, not like an academic or a marketing copy writer
- Be concise. Avoid hedging language that fills space without adding meaning
- Recommendations must be specific and actionable, not generic
- Do not repeat scorecard data in prose when a table already shows it
- This is for senior education leaders; assume they are smart and pressed for time

OUTPUT RULES:
- Respond ONLY with valid JSON matching the brief schema
- Do not include markdown fences, preamble, or commentary
- The `disclosure` field must always be present

---

## USER

Generate a Mode 2 Strategic Brief for:

**Community:** {{COMMUNITY_NAME}}, {{STATE_NAME}}
**Community ID:** {{COMMUNITY_ID}}
**Operator Preset:** {{PRESET}} — {{PRESET_DESCRIPTION}}
**Date:** {{TODAY_DATE}}

---

### SCORECARD (from S5 — use this as your structured foundation):
{{SCORECARD_JSON}}

---

### VERIFIED FACTS (ONLY these may be referenced in your brief):
{{VERIFIED_FACTS_JSON}}

---

### FACTS ROUTED TO NEEDS VERIFICATION (include these in needs_verification section):
{{NEEDS_VERIFICATION_JSON}}

---

Generate a brief JSON object with this exact structure:

```json
{
  "brief_id": "br_{{COMMUNITY_ID}}_{{PRESET}}_mode2_{{TODAY_DATE}}",
  "community_id": "{{COMMUNITY_ID}}",
  "community_name": "{{COMMUNITY_NAME}}",
  "state": "{{STATE_CODE}}",
  "preset": "{{PRESET}}",
  "mode": 2,
  "mode_label": "Strategic Brief",
  "classification": "<copy from scorecard.tier_display_label>",
  "composite_score": <copy from scorecard>,
  "confidence_overall": "<copy from scorecard>",
  "data_through": "<most recent source_date in the verified facts>",
  "generated_at": "{{TODAY_DATE}}",
  "generated_by": "claude-sonnet-4-5",

  "executive_snapshot": "<2-4 sentences. Lead with the tier and what drives it. Include 1 key risk and 1 key opportunity. Do NOT introduce facts not in the verified array.>",

  "scorecard_summary": {
    "composite_score": <number>,
    "tier_display_label": "<string>",
    "override_flags": [<copy from scorecard>],
    "top_drivers": [
      {
        "dimension": "<dimension_name>",
        "score": <number>,
        "driver": "<one line: what drove this score, using only verified facts>"
      }
    ],
    "dimension_table": [
      {
        "dimension": "<name>",
        "display_name": "<human-readable name>",
        "score": <number>,
        "weight": <number>,
        "confidence": "<HIGH|MODERATE|LOW>",
        "driver": "<one line>"
      }
    ]
  },

  "recommendations": [
    {
      "action": "<concise action statement — what should the operator do?>",
      "rationale": "<why, grounded only in verified facts>",
      "evidence_summary": "<1-2 sentence evidence summary>",
      "primary_risk": "<main risk to this recommendation>",
      "confidence": "<HIGH|MODERATE|LOW>",
      "supporting_fact_ids": ["<dp_... IDs from the verified facts array>"]
    }
  ],

  "quick_reads": {
    "facilities": "<1 paragraph. What does a new operator need to know about getting a building here? Use only verified facts. If insufficient facts, say so explicitly.>",
    "political": "<1 paragraph. Local political environment for charters. Use only verified facts.>",
    "authorizer": "<1 paragraph. Who authorizes here, and what does entry look like? Use only verified facts.>"
  },

  "schools_to_watch": [
    {
      "school_id": null,
      "school_name": "<school name from the known schools list or verified facts>",
      "flag_type": "<REPLICATION_CANDIDATE|RENEWAL_RISK|CLOSURE_RISK|PARTNERSHIP_OPPORTUNITY|TURNAROUND_CANDIDATE|HIGH_PERFORMER>",
      "reason": "<why this school is flagged — cite verified facts>"
    }
  ],

  "needs_verification": [
    {
      "claim": "<claim that could not be verified>",
      "reason": "<why>",
      "source_class_attempted": "<source class tried>",
      "resolution_path": "<what source or action would resolve this>",
      "impact_if_wrong": "<HIGH|MODERATE|LOW>"
    }
  ],

  "sources": [
    {
      "ref_num": 1,
      "source_class": "<class>",
      "title": "<title>",
      "url": "<url or null>",
      "date": "<date or null>",
      "confidence": "<HIGH|MODERATE|LOW>"
    }
  ],

  "audit_passed": null,
  "audit_flags": [],
  "disclosure": "Generated with Claude assistance (s3 structured extraction, s6 synthesis). All facts sourced from public data as cited. Requires human review and verification before external use or strategic decision-making."
}
```

FINAL RULES:
- recommendations: generate exactly 2. Each must cite at least 1 supporting_fact_id.
- recommendations[].rationale: ≤40 words each.
- recommendations[].evidence_summary: ≤25 words each.
- quick_reads: each paragraph ≤50 words. Omit if no verified facts exist for that topic.
- schools_to_watch: leave empty unless a specific school name appears in the verified facts.
- needs_verification: include items from the NEEDS VERIFICATION input only; omit items with null impact.
- needs_verification[].resolution_path: ≤10 words each.
- sources: include only sources cited in the brief body; maximum 6 entries.
- executive_snapshot: ≤60 words. Write last. Synthesize, do not introduce.
- Do NOT pad. Short, dense, specific beats long, hedged, generic.
