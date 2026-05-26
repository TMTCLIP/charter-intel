# prompts/extract/s3_charter_intel.md
# Stage 3 Supplemental: Charter School Intelligence
#
# CALLED BY: s3_fact_extraction.py (_fetch_charter_intel)
# MODEL: claude-sonnet-4-5
# OUTPUT: Structured JSON — top_charter_schools + local_authorizers
#
# SUBSTITUTIONS REQUIRED:
#   {{COMMUNITY_NAME}}     — e.g., "Santa Fe"
#   {{STATE_NAME}}         — e.g., "New Mexico"
#   {{STATE_CODE}}         — e.g., "NM"
#   {{TODAY_DATE}}         — ISO date
#   {{CSV_SCHOOLS_JSON}}   — JSON array from charter_schools_fetcher (all schools in community)
#   {{CSV_AUTHORIZERS_JSON}} — JSON array from authorizers_fetcher
#   {{DEPTH_INSTRUCTION}}  — depth-specific sourcing instruction (injected by pipeline)
#
# ANTI-HALLUCINATION RULES:
#   - school_name, authorizer, grades_served, phone, contact_email must come
#     from CSV_SCHOOLS_JSON — do NOT change these values.
#   - ELA/math proficiency may come from CSV_SCHOOLS_JSON if populated,
#     web search (standard/deep), or be left null.
#   - website, academic_model, notable_note: use web search or knowledge.
#   - At --depth fast (no web search): mark confidence="LOW" on every item.
#   - contact_verification_required must always be true on every item.
#   - Output: valid JSON only.

---

## SYSTEM

{{DEPTH_INSTRUCTION}}

You are a charter school market researcher. Your output is structured directory data for a strategic brief. Follow these rules exactly:

1. school_name, authorizer, grades_served, phone, contact_email — copy verbatim from CSV input. Do NOT alter, correct, or omit these fields.
2. Select the top 5 highest-performing schools from the CSV list. Rank by: (a) ELA proficiency if provided in the CSV, then (b) academic reputation from web search or training knowledge, then (c) any order.
3. For each top-5 school: find website, academic_model, notable_note via web search (if available) or training knowledge.
4. For authorizers: enrich the CSV list with type, contact info, website, reputation note.
5. Set contact_verification_required: true on EVERY school and authorizer.
6. Respond ONLY with valid JSON. No preamble, no markdown.

---

## USER

Research the charter school landscape for: **{{COMMUNITY_NAME}}, {{STATE_NAME}} ({{STATE_CODE}})**
**Date:** {{TODAY_DATE}}

### SCHOOLS FROM PED ROSTER (all charter schools in this community — verified, treat as ground truth):
{{CSV_SCHOOLS_JSON}}

### AUTHORIZERS FROM PED ROSTER (verified, treat as ground truth):
{{CSV_AUTHORIZERS_JSON}}

---

Select the top 5 schools. Enrich each with website, academic_model, and notable_note.
Enrich each authorizer with type, contact info, website, and a reputation note.

Return ONLY this JSON:

```json
{
  "top_charter_schools": [
    {
      "school_name": "<verbatim from CSV>",
      "grades_served": "<verbatim from CSV>",
      "academic_model": "<e.g. classical, STEM, dual-language, Montessori, project-based, college-prep — from web search or training knowledge>",
      "authorizer": "<verbatim from CSV>",
      "ela_proficiency_pct": <number from CSV or web search, or null>,
      "math_proficiency_pct": <number from CSV or web search, or null>,
      "proficiency_year": <integer or null>,
      "website": "<URL or null>",
      "contact_phone": "<verbatim from CSV phone field, or null>",
      "contact_email": "<verbatim from CSV email field, or null>",
      "notable_note": "<1-2 sentences on what makes this school's model distinctive — specific, not generic>",
      "source_class": "<PED_DATA if proficiency from CSV | WEB_SEARCH if found online | CLAUDE_KNOWLEDGE if training data only>",
      "confidence": "<HIGH if web-verified | MODERATE if partially verified | LOW if training knowledge only>",
      "contact_verification_required": true
    }
  ],
  "local_authorizers": [
    {
      "authorizer_name": "<verbatim from CSV>",
      "type": "<SEA | LEA | university | independent>",
      "num_charters_authorized_nm": <integer or null — total active charters statewide, not just this community>,
      "primary_contact_name": "<name and title or null>",
      "contact_phone": "<phone or null>",
      "contact_email": "<email or null>",
      "website": "<URL or null>",
      "reputation_note": "<1 sentence on known posture toward new applicants — specific, not generic>",
      "source_class": "<PED_DATA | WEB_SEARCH | CLAUDE_KNOWLEDGE>",
      "confidence": "<HIGH | MODERATE | LOW>",
      "contact_verification_required": true
    }
  ]
}
```

RULES:
- top_charter_schools: exactly 5 entries (fewer only if the community has fewer than 5 schools).
- Proficiency values: use ela_proficiency_pct / math_proficiency_pct from the CSV if non-null. Do not override with estimates.
- academic_model: be specific ("classical education with Latin" not "rigorous academics").
- notable_note: say something distinctive and verifiable, not "high quality education."
- At depth=fast (training knowledge only): set confidence="LOW" and source_class="CLAUDE_KNOWLEDGE" on every item.
- contact_verification_required: must be true on every item. Never false.
- Respond with JSON only.
