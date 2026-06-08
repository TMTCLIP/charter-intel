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

---

## Addendum — 2026-06-08 (fresh-run regression review)

A red-team report described a fresh `--force` Oxford run failing on four counts
(market type flipped to strategic; MDE rating shown as B / "Not Available";
Funding Environment unscored; 43.8% data coverage). **None of these reproduce
against the current code + committed data.** Verified directly:

- `market_type` = **greenfield** (registry `has_charters: False`, static — `--force` cannot change it).
- `get_mde_district_rating("2803450")` = **A** (xlsx row "Oxford School District → A"; CCD crosswalk + rating map both clean).
- `get_district_data("ms-oxford-2803450","MS")` returns per-pupil **$16,954.05**; the fresh S5 scorecard scores Funding Environment **9.0** (`used_default=False`).
- Fresh S5 data coverage = **81.2%**; only the two demand dimensions are excluded, not five.

No scoring or data code was changed. Lock-in regression tests were added to pin
this verified-correct behavior. Two items for your awareness / sign-off:

### A — Dimension-exclusion behavior in consent-gated greenfield markets

The exclusion of **Charter Saturation** and **Competitive Opportunity** from
Oxford's composite is **data-driven**, not market-type-driven: both dimensions
`used_default=True` (no demand data), so the Session-18 demand-evidence gate
excludes them and redistributes their weight across the scored dimensions. This
is working as designed — but for a consent-gated *greenfield* market it means the
composite (6.3, "MODERATE OPPORTUNITY") rests entirely on **operational
viability** while both **demand** signals are absent. The prior session added a
deterministic disclosure + a parent-demand recommendation to surface this, but
the underlying question is a methodology call: **is it correct operator intent
for the maturity_adjusted composite to headline "opportunity" when no demand
dimension is scored?** If not, options: (1) floor data-coverage confidence harder
when both demand dims are excluded; (2) suppress the opportunity tier label (not
just disclose) until at least one demand signal exists. Either is a scoring/tiering
change → your sign-off.

### B — Academic Need 5.0 (carried forward, still unfixed)

Unchanged from the original flag above: the Academic Need 5.0 driver ("moderate
academic performance gaps" / "charter can differentiate on instruction") still
contradicts an above-proficiency, A-rated district. Score and narrative remain
untouched pending your decision. No code change made.

---

## Addendum — 2026-06-08 (Session 11: stale-artifact red-team + RC3 widening)

A red-team report flagged nine defects (B rating asserted, prohibition framing,
HOSTILE_AUTHORIZER cap, /9 denominator, etc.). **All nine were traced to a
superseded artifact** — `data/cache/synthesis/ms/ms-oxford/s6_brief_growth_mode2.json`
(community_id `ms-oxford` pre-LEAID-migration slug, `growth` preset, generated
2026-06-05, `statutory_barrier: NONE`). It predates the entire Session-8 barrier
layer. The current target (`ms-oxford-2803450`, `maturity_adjusted`) is clean on
all nine; the resolver maps every spelling of "Oxford" to `ms-oxford-2803450`, so
the bare slug can no longer be produced. The stale artifacts were deleted.

No scoring changed this session. One code change: `_patch_statutory_narrative`
was widened to also catch **prohibition-class** false framing (it previously
caught only "ineligible"/§37-28-5/D-F) and to scan dimension/top-driver text.
Four methodology questions for your awareness:

### A — Does a consent gate drive Political Climate / Authorizer Friendliness to 1.0?

In the **stale** artifact the consent gate collapsed Political Climate and
Authorizer Friendliness to floor scores with "legally ineligible" drivers. In the
**current** target this does NOT happen — Political Climate scores 5.0 (neutral
default) and Authorizer Friendliness 5.0; the Session-8 consent-gate modeling
already prevents the collapse. **No open defect.** The question for you is only
whether the *current* neutral treatment is the intended posture for a consent-gated
market, or whether the gate should nudge these dims at all. Either way → your call;
no code changed.

### B — Should a consent-gated authorizer count as "0 accessible authorizers"?

In the stale artifact, MCSAB (gated, not absent) triggered a HOSTILE_AUTHORIZER
override on "0 accessible authorizers," capping the tier at WATCHLIST. The current
target shows **no override flags** (MCSAB is correctly modeled as accessible-but-
gated). **No open defect in the target.** Flagging only so you can confirm the
override logic treats a consent-gated authorizer as accessible (count ≥ 1) by
intent — it is scoring-adjacent (tier cap), so no code was touched.

### C — Academic Need 5.0 framing (carried forward, unchanged)

Same as Concern 2 / the prior addendum. Still open; no code change.

### D — Demand-excluded consent-gated greenfield headlining an opportunity tier

Same as the prior addendum's item. Still open; no code change.
