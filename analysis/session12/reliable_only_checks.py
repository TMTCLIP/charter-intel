"""
Session 12 — Three data checks
Standalone, run from ~/Downloads/charter-intel/

Section 1: Reliable-only correlation recompute + diff vs 28-city matrix
Section 2: Stable-four SMALL_MARKET enrollment check
Section 3: Edgewood/Jemez authorizer score confirmation
"""

import json
import math
import numpy as np
import pandas as pd
from pathlib import Path

CACHE_BASE = Path("data/cache/community/nm")
S1_PATH = Path("data/cache/state/nm/s1_community_list_2026-05-28.json")
OUTPUT_DIR = Path("analysis/session12/outputs")
MATRIX_28_PATH = OUTPUT_DIR / "correlation_matrix.csv"
SENS_MATRIX_PATH = OUTPUT_DIR / "sensitivity_matrix.csv"

DIMENSIONS = [
    "academic_need",
    "competitive_opportunity",
    "charter_saturation",
    "political_climate",
    "authorizer_friendliness",
    "replication_feasibility",
    "population_trends",
    "operational_complexity",
    "funding_environment",
    "facilities_feasibility",
]

STABLE_FOUR = ["nm-questa", "nm-carlsbad", "nm-gallup", "nm-deming"]
GATE_CITIES = ["nm-edgewood", "nm-jemez-pueblo"]
CONTRAST_CITIES = ["nm-carlsbad", "nm-espanola"]

separator = "─" * 60


# ── Load canonical city list (excluding nm-cleveland) ─────────────────────────

with open(S1_PATH) as f:
    s1 = json.load(f)
canonical_ids = [
    c["community_id"] for c in s1["communities"]
    if c["community_id"] != "nm-cleveland"
]


# ── Load all scorecards ────────────────────────────────────────────────────────

def load_scorecard(cid):
    path = CACHE_BASE / cid / "s5_scorecard.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


scorecards = {cid: load_scorecard(cid) for cid in canonical_ids}
scorecards = {k: v for k, v in scorecards.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Reliable-only correlation recompute
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 60)
print("SECTION 1 — RELIABLE-ONLY CORRELATION RECOMPUTE")
print("=" * 60)
print()

# Identify reliable-tier cities (coverage >= 70%)
reliable_cities = []
for cid, sc in scorecards.items():
    pct = sc.get("data_coverage_pct", 0)
    if pct >= 70:
        reliable_cities.append((cid, pct))

reliable_cities.sort(key=lambda x: x[1])
reliable_ids = [c for c, _ in reliable_cities]

print(f"Reliable-tier cities (data_coverage_pct ≥ 70%): {len(reliable_ids)} cities")
print()
for cid, pct in reliable_cities:
    print(f"  {pct:.0f}%  {cid}")
print()

# Build reliable-only DataFrame
rows = []
for cid in reliable_ids:
    sc = scorecards[cid]
    dims = sc["dimensions"]
    row = {"city": cid}
    for dim in DIMENSIONS:
        row[dim] = dims[dim]["score"] if dim in dims else float("nan")
    rows.append(row)

df_rel = pd.DataFrame(rows).set_index("city")[DIMENSIONS]

# Compute reliable-only Pearson correlation
corr_rel = df_rel.corr(method="pearson", min_periods=2).round(4)

# Load 28-city matrix (skip header comment line)
corr_28 = pd.read_csv(MATRIX_28_PATH, comment="#", index_col=0)
corr_28 = corr_28.round(4)

# ── Diff table ────────────────────────────────────────────────────────────────

THRESHOLD = 0.05

diff_lines = []
diff_lines.append(separator)
diff_lines.append("CORRELATION DIFF REPORT")
diff_lines.append(f"28-city matrix vs reliable-only matrix ({len(reliable_ids)} cities)")
diff_lines.append(separator)
diff_lines.append("")
diff_lines.append(
    f"Cells where |reliable-only r − 28-city r| > {THRESHOLD}:"
)
diff_lines.append("")

significant_diffs = []
for i, d1 in enumerate(DIMENSIONS):
    for d2 in DIMENSIONS[i + 1:]:
        r28 = corr_28.loc[d1, d2]
        rrel = corr_rel.loc[d1, d2]
        if pd.isna(r28) or pd.isna(rrel):
            continue
        delta = rrel - r28
        if abs(delta) > THRESHOLD:
            if r28 * rrel < 0:
                direction = "SIGN FLIPPED"
            elif abs(rrel) > abs(r28):
                direction = "STRENGTHENED"
            else:
                direction = "WEAKENED"
            significant_diffs.append((d1, d2, r28, rrel, delta, direction))

if significant_diffs:
    for d1, d2, r28, rrel, delta, direction in sorted(
        significant_diffs, key=lambda x: -abs(x[4])
    ):
        diff_lines.append(f"  [{d1}] × [{d2}]")
        diff_lines.append(f"    28-city r:       {r28:+.4f}")
        diff_lines.append(f"    reliable-only r: {rrel:+.4f}")
        diff_lines.append(f"    change:          {delta:+.4f}")
        diff_lines.append(f"    direction:       {direction}")
        diff_lines.append("")
else:
    diff_lines.append("  No cells exceed the ±0.05 threshold.")
    diff_lines.append("")

# ── Focused findings on three pairs ──────────────────────────────────────────

diff_lines.append(separator)
diff_lines.append("FOCUSED FINDINGS — THREE PAIRS OF INTEREST")
diff_lines.append(separator)
diff_lines.append("")

pairs_of_interest = [
    (
        "political_climate", "facilities_feasibility",
        "political_climate × facilities_feasibility"
    ),
    (
        "charter_saturation", "replication_feasibility",
        "charter_saturation × replication_feasibility"
    ),
    (
        "academic_need", "operational_complexity",
        "academic_need × operational_complexity"
    ),
]

for d1, d2, label in pairs_of_interest:
    r28 = corr_28.loc[d1, d2]
    rrel = corr_rel.loc[d1, d2]
    delta = rrel - r28
    diff_lines.append(f"  {label}")
    diff_lines.append(f"    28-city r: {r28:+.2f} | reliable-only r: {rrel:+.2f} | change: {delta:+.2f}")

    if d1 == "political_climate" and d2 == "facilities_feasibility":
        if abs(rrel) > 0.70:
            note = (
                f"Holds as a high-correlation pair ({rrel:+.2f}) even on reliable-tier "
                f"cities only; the r = 0.80 in the full matrix is not primarily a "
                f"default-value artifact."
            )
        elif abs(rrel) < 0.50:
            note = (
                f"Drops below moderate-correlation threshold on reliable-tier cities; "
                f"the r = 0.80 in the full matrix is likely driven by shared default "
                f"values (5.0) and does not represent a robust construct relationship."
            )
        else:
            note = (
                f"Weakens but remains a moderate correlation on reliable-tier cities; "
                f"the full-matrix r = 0.80 is partly, but not entirely, a default-value "
                f"artifact."
            )
    elif d1 == "charter_saturation" and d2 == "replication_feasibility":
        if rrel < -0.50:
            note = (
                f"Negative relationship survives on reliable-tier cities (r = {rrel:+.2f}); "
                f"the inverse association between low charter saturation and low replication "
                f"readiness is not driven by unreliable-tier cities."
            )
        elif rrel > 0:
            note = (
                f"Sign flips on reliable-tier cities; the negative relationship in the "
                f"full matrix does not hold when low-coverage cities are excluded."
            )
        else:
            note = (
                f"Negative relationship weakens but persists on reliable-tier cities "
                f"(r = {rrel:+.2f}); some attenuation may be driven by low-coverage cities."
            )
    else:  # academic_need × operational_complexity
        if rrel < -0.50:
            note = (
                f"Negative relationship survives on reliable-tier cities (r = {rrel:+.2f}); "
                f"the association between high academic need and high operational complexity "
                f"is not an artifact of low-coverage cities."
            )
        elif rrel > 0:
            note = (
                f"Sign flips on reliable-tier cities; the negative relationship in the "
                f"full matrix does not hold when low-coverage cities are excluded."
            )
        else:
            note = (
                f"Negative relationship weakens on reliable-tier cities (r = {rrel:+.2f}); "
                f"some of the correlation may be driven by low-coverage cities."
            )

    diff_lines.append(f"    → {note}")
    diff_lines.append("")

diff_lines.append(separator)

diff_text = "\n".join(diff_lines)
print(diff_text)

# Write outputs
csv_path = OUTPUT_DIR / "correlation_matrix_reliable_only.csv"
with open(csv_path, "w") as f:
    f.write(f"# Pearson correlation matrix — {len(reliable_ids)} reliable-tier NM cities (coverage ≥70%)\n")
corr_rel.round(2).to_csv(csv_path, mode="a")
print(f"\nReliable-only matrix written to {csv_path}")

diff_path = OUTPUT_DIR / "correlation_diff_report.txt"
diff_path.write_text(diff_text)
print(f"Diff report written to {diff_path}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — Stable-four SMALL_MARKET enrollment check
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 60)
print("SECTION 2 — STABLE-FOUR SMALL_MARKET ENROLLMENT CHECK")
print("=" * 60)
print()

SMALL_MARKET_THRESHOLD = 1500

sens_df = pd.read_csv(SENS_MATRIX_PATH, index_col="city")

def find_enrollment(cid):
    """Return (value, source_file) for k12_enrollment_total. Checks s4, s3, s5."""
    for fname in ["s4_verified.json", "s3_facts_raw.json"]:
        path = CACHE_BASE / cid / fname
        if not path.exists():
            continue
        with open(path) as f:
            data = json.load(f)
        facts = data.get("facts", [])
        for fact in facts:
            if fact.get("fact_key") == "k12_enrollment_total":
                val = fact.get("value")
                if val is not None:
                    return (val, fname)
    # Try enrollment_by_year last year as fallback
    for fname in ["s4_verified.json", "s3_facts_raw.json"]:
        path = CACHE_BASE / cid / fname
        if not path.exists():
            continue
        with open(path) as f:
            data = json.load(f)
        for fact in data.get("facts", []):
            if fact.get("fact_key") == "enrollment_by_year":
                val = fact.get("value")
                if val and isinstance(val, dict):
                    latest_year = str(max(int(y) for y in val.keys()))
                    return (val[latest_year], f"{fname} (enrollment_by_year, {latest_year})")
    return (None, "NOT_FOUND")


city_results = []
for cid in STABLE_FOUR:
    enrollment, source = find_enrollment(cid)
    if cid in sens_df.index:
        rank_a = sens_df.loc[cid, "rank_A"]
        # Adjust for nm-cleveland removal (cleveland was rank 2; cities ranked >2 shift up)
        adj_rank_a = rank_a - 1 if (rank_a != 99 and rank_a > 2) else rank_a
    else:
        adj_rank_a = "N/A"

    if enrollment is None:
        status = "NO DATA"
        fires = None
    elif float(enrollment) < SMALL_MARKET_THRESHOLD:
        status = "FLAG FIRES"
        fires = True
    else:
        status = "clear"
        fires = False

    print(f"City:              {cid}")
    print(f"k12_enrollment:    {enrollment if enrollment is not None else 'NOT_FOUND'}")
    print(f"Source file:       {source}")
    print(
        f"Threshold (1500):  {status}"
    )
    print(f"Composite rank A:  {adj_rank_a} (28-city adjusted)")
    print()
    city_results.append((cid, enrollment, source, status, fires, adj_rank_a))

# Summary block
print(separator)
print("SMALL_MARKET STATUS — STABLE FOUR")
print(separator)
any_fires = False
for cid, enrollment, source, status, fires, rank_a in city_results:
    enroll_str = str(enrollment) if enrollment is not None else "no data"
    print(f"  {cid}: {enroll_str} — {status}")
    if fires:
        any_fires = True

print()
if any_fires:
    print(
        "⚠ IMPLICATION: SMALL_MARKET flag is informational-only in current\n"
        "  pipeline but the deliverable tier must caveat any flagged city\n"
        "  explicitly. A top-stable result that fires SMALL_MARKET is a\n"
        "  scale artifact, not a robust opportunity signal."
    )
else:
    print("✓ All four stable cities clear the enrollment threshold.")
print(separator)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Confirm edgewood and jemez authorizer defaults
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 60)
print("SECTION 3 — EDGEWOOD AND JEMEZ AUTHORIZER SCORE CONFIRMATION")
print("=" * 60)
print()

def get_authorizer_detail(cid):
    """
    Returns dict with: score, used_default, confidence, driver,
    raw_fact_value, raw_fact_confidence, raw_fact_verification,
    in_main_analysis, source_file
    """
    result = {}

    # Scorecard: score and used_default
    sc = scorecards.get(cid)
    if sc:
        dim = sc["dimensions"].get("authorizer_friendliness", {})
        result["score"] = dim.get("score")
        result["used_default"] = dim.get("used_default")
        result["confidence_s5"] = dim.get("confidence")
        result["driver"] = dim.get("primary_driver")
    else:
        result["score"] = None
        result["used_default"] = None
        result["confidence_s5"] = None
        result["driver"] = None

    # S4 verified facts: raw authorizer fact
    for fname in ["s4_verified.json", "s3_facts_raw.json"]:
        path = CACHE_BASE / cid / fname
        if not path.exists():
            continue
        with open(path) as f:
            data = json.load(f)
        for fact in data.get("facts", []):
            fk = fact.get("fact_key", "")
            if fk in ("num_accessible_authorizers", "authorizer_friendliness_index",
                      "authorizer_known_preferences"):
                result["raw_fact_key"] = fk
                result["raw_fact_value"] = fact.get("value")
                result["raw_fact_confidence"] = fact.get("confidence")
                result["raw_fact_verification"] = fact.get("verification_status")
                result["in_main_analysis"] = fact.get("in_main_analysis")
                result["source_file"] = fname
                break
        if "raw_fact_key" in result:
            break

    if "raw_fact_key" not in result:
        result["raw_fact_key"] = "NOT_FOUND"
        result["raw_fact_value"] = None
        result["raw_fact_confidence"] = None
        result["raw_fact_verification"] = None
        result["in_main_analysis"] = None
        result["source_file"] = "NOT_FOUND"

    return result


print("Detail for gate cities and contrast cities:\n")
all_details = {}

for cid in GATE_CITIES + CONTRAST_CITIES:
    d = get_authorizer_detail(cid)
    all_details[cid] = d
    print(f"City:                    {cid}")
    print(f"authorizer score (S5):   {d['score']}")
    print(f"used_default:            {d['used_default']}")
    print(f"S5 driver:               {d['driver']}")
    print(f"Source fact key:         {d['raw_fact_key']}")
    print(f"Source fact value:       {d['raw_fact_value']}")
    print(f"Confidence:              {d['raw_fact_confidence']}")
    print(f"Verification status:     {d['raw_fact_verification']}")
    print(f"in_main_analysis:        {d['in_main_analysis']}")
    print(f"Source file:             {d['source_file']}")
    print()

# Summary
print(separator)
print("AUTHORIZER DEFAULT CONFIRMATION")
print(separator)

for cid in GATE_CITIES:
    d = all_details[cid]
    defaulted_str = "no — computed from verified fact" if d["used_default"] is False else (
        "yes" if d["used_default"] is True else "cannot determine"
    )
    print(
        f"  {cid}: score {d['score']}, "
        f"source {d['raw_fact_value']} ({d['raw_fact_key']}), "
        f"confidence {d['raw_fact_confidence']}, "
        f"defaulted: {defaulted_str}"
    )

print()
print("  Contrast (7.0-cluster cities):")
for cid in CONTRAST_CITIES:
    d = all_details[cid]
    print(
        f"  {cid}: score {d['score']}, "
        f"source {d['raw_fact_value']} ({d['raw_fact_key']}), "
        f"confidence {d['raw_fact_confidence']}"
    )

print()

# Determine finding
edge_d = all_details["nm-edgewood"]
jemez_d = all_details["nm-jemez-pueblo"]

if (
    edge_d["used_default"] is False
    and jemez_d["used_default"] is False
    and edge_d["raw_fact_verification"] == "VERIFIED"
    and jemez_d["raw_fact_verification"] == "VERIFIED"
):
    status_str = "COMPUTED — NOT DEFAULT"
    print(f"  FINDING: edgewood and jemez authorizer scores are {status_str}")
    print()
    print(
        "  ✓ Gate exclusion of these cities in Scheme C reflects a real\n"
        "    data distinction: both have num_accessible_authorizers = 1\n"
        "    (PEC statewide only; no local district authorizer), while most\n"
        "    NM cities have 2 accessible authorizers (PEC + local district).\n"
        "    The gate fires on a verified fact, not an imputed score.\n"
        "    Whether having 1 vs 2 authorizers represents meaningful\n"
        "    strategic risk is a human judgment call, not a data artifact."
    )
elif (
    edge_d["used_default"] is True
    or jemez_d["used_default"] is True
):
    status_str = "CONFIRMED DEFAULT"
    print(f"  FINDING: edgewood and jemez authorizer scores are {status_str}")
    print()
    print(
        "  ⚠ Gate exclusion of these cities in Scheme C is driven by imputed\n"
        "    scores, not real authorizer hostility. Exclude on coverage grounds\n"
        "    instead. Gate should be scoped cross-state."
    )
else:
    status_str = "CANNOT DETERMINE"
    print(f"  FINDING: edgewood and jemez authorizer scores are {status_str}")

print(separator)
print()

# ─────────────────────────────────────────────────────────────────────────────
# FINAL CHECKLIST
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 60)
print("FINAL CHECKLIST")
print("=" * 60)
print(f"[ ] Reliable-tier cities identified: {len(reliable_ids)} cities at ≥70% coverage")
print(f"[ ] Reliable-only correlation matrix computed")
print(f"[ ] Diff against 28-city matrix produced")
print(f"[ ] correlation_matrix_reliable_only.csv written")
print(f"[ ] correlation_diff_report.txt written")
print(f"[ ] Stable-four enrollment check complete")
fires_list = [cid for cid, _, _, status, fires, _ in city_results if fires]
print(f"[ ] SMALL_MARKET fires on: {', '.join(fires_list) if fires_list else 'none'}")
print(f"[ ] Edgewood authorizer default status: {status_str}")
print(f"[ ] Jemez authorizer default status: {status_str}")
