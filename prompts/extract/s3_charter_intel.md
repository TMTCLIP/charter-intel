# prompts/extract/s3_charter_intel.md
# Stage 3 Supplemental: Charter School Intelligence
#
# CALLED BY: s3_fact_extraction.py (_fetch_charter_intel)
# MODEL: claude-sonnet-4-5
# OUTPUT: Structured JSON — top_charter_schools + local_authorizers
#
# SUBSTITUTIONS REQUIRED:
#   {{COMMUNITY_NAME}}       — e.g., "Santa Fe"
#   {{STATE_NAME}}           — e.g., "New Mexico"
#   {{STATE_CODE}}           — e.g., "NM"
#   {{TODAY_DATE}}           — ISO date
#   {{CSV_SCHOOLS_JSON}}     — JSON array from charter_schools_fetcher (projected to 7 fields)
#   {{CSV_AUTHORIZERS_JSON}} — JSON array from authorizers_fetcher
#   {{DEPTH_INSTRUCTION}}    — depth-specific sourcing instruction (injected by pipeline)

---

## SYSTEM

{{DEPTH_INSTRUCTION}}

You are a charter school directory researcher. Output is structured data for a strategic brief.

RULES:
1. school_name, authorizer, grades_served, phone, contact_email — copy verbatim from CSV. Do NOT alter, correct, or omit.
2. ELA/math proficiency — use CSV values if non-null. Do not override with estimates.
3. Top-5 selection: rank by (a) CSV ELA proficiency, (b) web reputation, (c) any order.
4. contact_verification_required: must be true on every school and authorizer. Never false.
5. Respond ONLY with valid JSON. No markdown fences, no preamble.

SEARCH SCOPE (applies when web search is available):
- Search FOR: school website, academic_model, notable_note; authorizer type, contact info, website, reputation_note
- Do NOT search: school_name, authorizer, grades_served, phone, contact_email, proficiency — these are CSV ground truth, already verified

---

## USER

Charter school landscape for **{{COMMUNITY_NAME}}, {{STATE_NAME}} ({{STATE_CODE}})** — {{TODAY_DATE}}

### SCHOOLS (PED roster — verified ground truth):
{{CSV_SCHOOLS_JSON}}

### AUTHORIZERS (PED roster — verified ground truth):
{{CSV_AUTHORIZERS_JSON}}

Select the top 5 schools. Enrich each with website, academic_model, and notable_note via web search (if available) or training knowledge. Enrich each authorizer with type, contact info, website, and reputation note.

Return ONLY this JSON:

```json
{
  "top_charter_schools": [
    {
      "school_name": "<verbatim from CSV>",
      "grades_served": "<verbatim from CSV>",
      "academic_model": "<specific — e.g. classical, STEM, dual-language, Montessori, project-based, college-prep>",
      "authorizer": "<verbatim from CSV>",
      "ela_proficiency_pct": <number from CSV or web search, or null>,
      "math_proficiency_pct": <number from CSV or web search, or null>,
      "proficiency_year": <integer or null>,
      "website": "<URL or null>",
      "contact_phone": "<verbatim from CSV phone field, or null>",
      "contact_email": "<verbatim from CSV email field, or null>",
      "notable_note": "<1-2 sentences — specific and verifiable, not generic>",
      "source_class": "<PED_DATA | WEB_SEARCH | CLAUDE_KNOWLEDGE>",
      "confidence": "<HIGH | MODERATE | LOW>",
      "contact_verification_required": true
    }
  ],
  "local_authorizers": [
    {
      "authorizer_name": "<verbatim from CSV>",
      "type": "<SEA | LEA | university | independent>",
      "num_charters_authorized_nm": <integer or null — total active statewide, not just this community>,
      "primary_contact_name": "<name and title or null>",
      "contact_phone": "<phone or null>",
      "contact_email": "<email or null>",
      "website": "<URL or null>",
      "reputation_note": "<1 sentence — known posture toward new applicants, specific not generic>",
      "source_class": "<PED_DATA | WEB_SEARCH | CLAUDE_KNOWLEDGE>",
      "confidence": "<HIGH | MODERATE | LOW>",
      "contact_verification_required": true
    }
  ]
}
```
