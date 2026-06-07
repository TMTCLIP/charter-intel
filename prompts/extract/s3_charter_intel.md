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
5. CHARTER-ADJACENT SCHOOLS (charter_adjacent_schools array): list schools that compete
   for the same students as a charter would but are NOT on the PED charter roster above.
   This is a SEPARATE, non-authoritative signal used to flag markets that look "empty" on
   the formal roster but are not. Definition — INCLUDE a school only if it is one of:
     • an autonomous public school of choice operating outside a standard attendance zone
       (e.g. district innovation school, magnet/option school run with charter-like autonomy),
     • a university-model or "hybrid" school operating as a distinct entity,
     • an established private micro-school / independent school enrolling K-12 students,
     • a school that has FORMALLY APPLIED to an authorizer or PUBLICLY ANNOUNCED intent to
       open (status="pending").
   EXCLUDE: traditional zoned district schools; homeschool co-ops or pods without a fixed
   site; tutoring centers; daycares; any school already on the PED roster above.
   Each entry MUST carry status ("operating" or "pending"), source_class, confidence, and a
   source_url when web search was used. Do NOT pad the list — omit it (empty array) if you
   find nothing credible. NEVER invent a school to fill the array.
6. contact_verification_required: must be true on every school and authorizer. Never false.
7. Respond ONLY with valid JSON. No markdown fences, no preamble.

---

## USER

Charter school landscape for **{{COMMUNITY_NAME}}, {{STATE_NAME}} ({{STATE_CODE}})** — {{TODAY_DATE}}

### SCHOOLS (PED roster — verified ground truth):
{{CSV_SCHOOLS_JSON}}

### AUTHORIZERS (PED roster — verified ground truth):
{{CSV_AUTHORIZERS_JSON}}

Select the top 5 schools. Enrich each with website, academic_model, and notable_note via web search (if available) or training knowledge. Enrich each authorizer with type, contact info, website, and reputation note. Leave any field null if not readily found — do not search exhaustively to fill every field.

Then, if web search is available, identify CHARTER-ADJACENT schools per RULE 5 above. This matters most where the PED roster is empty or tiny: a market with zero authorized charters may still have charter-adjacent schools that affect real competition. Report only credible findings.

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
  ],
  "charter_adjacent_schools": [
    {
      "school_name": "<name>",
      "status": "<operating | pending>",
      "type": "<innovation school | magnet/option | university-model | private micro-school | other>",
      "grades_served": "<grade band or null>",
      "notable_note": "<why it is charter-adjacent — 1 sentence, specific>",
      "website": "<URL or null>",
      "source_class": "<WEB_SEARCH | CLAUDE_KNOWLEDGE>",
      "source_url": "<URL or null>",
      "confidence": "<HIGH | MODERATE | LOW>",
      "contact_verification_required": true
    }
  ]
}
```
