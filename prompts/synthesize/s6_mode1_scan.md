You are summarizing a charter school market scorecard.
Write a one-line snapshot (max 20 words) describing the city's opportunity and primary risk.
Return only valid JSON. No markdown fences, no preamble.
Do not invent facts not present in the scorecard.

CITY: {{COMMUNITY_NAME}}
COMMUNITY_ID: {{COMMUNITY_ID}}

SCORECARD:
{{SCORECARD_JSON}}

Return this exact JSON structure — no other fields:
{
  "city": "{{COMMUNITY_NAME}}",
  "composite": <copy composite_score_rounded from scorecard>,
  "tier": "<copy tier from scorecard>",
  "one_line_snapshot": "<max 20 words describing opportunity and primary risk>",
  "signal_tensions": [<copy triggered_by strings from override_flags, or empty list>],
  "deep_review_level": "<one of: strong_recommendation | recommended | optional_review | no_deep_needed>",
  "data_coverage_pct": <copy data_coverage_pct from scorecard>,
  "data_coverage_tier": "<copy data_coverage_tier from scorecard>"
}

deep_review_level rules (apply the FIRST match):
- strong_recommendation: tier is HIGH_PRIORITY
- recommended: tier is STRONG
- optional_review: tier is MODERATE or WATCHLIST
- no_deep_needed: tier is AVOID
