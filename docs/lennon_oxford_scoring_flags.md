# Oxford, MS — Scoring concerns for Lennon sign-off

**Status:** No scores, dimension weights, or scoring formulas were changed in this
pass. The Oxford brief-hardening work (D1–D8) was strictly presentation /
eligibility-consistency. The two items below are scoring-methodology questions
surfaced by the red-team review and require Lennon's sign-off before any change.

Composite (6.3) and all dimension scores/weights are unchanged. `git diff` touches
no scoring file (`s5_scoring.py`, `s5_score.py`, `scoring_weights.yaml`,
`scoring_presets.yaml`).

---

## Concern 1 — Funding Environment scored 9.0 may rest on a backwards causal inference

**What the score rests on.** Funding Environment = 9.0 (weight 15%), driven by
"per-pupil revenue 29.4% above the state average." The original LLM driver framed
this as signalling "reduced charter funding pressure."

**The red-team objection.** Mississippi charters are funded on the **state share**
of the resident district's adequate-education entitlement (the local contribution
is deducted). A strong local tax base therefore tends to produce a **smaller**
state share per pupil — so high district revenue may indicate *more* charter
funding pressure, not less. If correct, the inference baked into the 9.0 is
backwards, and a high local-revenue district should not automatically score as a
strong charter funding environment.

**A second issue (see D4).** The per-pupil figure ($16,954.05) is NCES
`TOTALEXP / enrollment` — **total** expenditure (capital-inclusive), not current
expenditure. It is now labelled as such in the brief, but if the funding score is
meant to proxy operating-cost headroom, a current-expenditure measure may be the
more appropriate input.

**Narrative edit made under the one permitted exception (flagged here as required).**
The brief's permission allowed neutralizing a *demonstrably false causal clause*
to a factual statement of the figure. I removed "and reduced charter funding
pressure" from the funding top-driver string and restated it factually:
> "Total per-pupil expenditure (capital-inclusive) is 29.4% above the state
> average, signaling a strong local revenue base."
This is a **narrative** edit only; the 9.0 score is untouched.

**Options for Lennon:**
1. Re-anchor the Funding Environment scoring input to the **state-share** funding
   mechanics (and/or current expenditure), so the dimension reflects charter
   funding reality rather than district wealth. (Scoring change — needs sign-off.)
2. Leave the score as-is and treat Funding Environment as a district-wealth proxy,
   documenting that it is **not** a charter-funding-favorability signal.

---

## Concern 2 — Academic Need scored 5.0 uses boilerplate that contradicts the data

**What the score rests on.** Academic Need = 5.0 (weight 15%). Driver: "ELA 62.1% /
math 69.4% indicate moderate academic pressure; charter can differentiate on
instruction."

**The red-team objection.** Oxford is an **A-rated district performing above the
state proficiency average**. A generic "a charter can differentiate on instruction"
rationale is the same boilerplate that would apply to a struggling district; it does
not reflect that this is a high-performing district where the academic-need case for
a charter is weak. The 5.0 (midpoint) may be defensible, but the *rationale* is not
data-grounded for this community.

**Left untouched per scope.** Per instructions, Academic Need's score **and**
narrative were not modified — flag only.

**Options for Lennon:**
1. Revise the Academic Need rationale generation so an A-rated / above-average
   district yields a low (not midpoint) academic-need score with a data-grounded
   rationale. (Scoring + narrative change — needs sign-off.)
2. Keep the 5.0 but replace the boilerplate rationale with a district-specific one
   acknowledging the strong baseline. (Narrative change — needs sign-off.)

---

## What this pass *did* add (non-scoring, for context)

To stop the brief from over-claiming demand, the re-rendered brief now carries a
deterministic, data-driven disclosure and recommendation that frame the entry case
honestly:
- A composite-context note: when both demand dimensions (Charter Saturation,
  Competitive Opportunity) are excluded, the composite "measures operational
  viability — not charter demand."
- A recommendation to **test parent demand / the differentiation thesis** as the
  gating market-entry question for a well-regarded district.

These reinforce — but do not substitute for — a methodology decision on Concerns 1
and 2.
