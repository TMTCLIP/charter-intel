"""
Scheme D — Maturity-adjusted reweight
Session 12 analysis script. Standalone — run from ~/Downloads/charter-intel/

charter_saturation and replication_feasibility halved to 0.0625 each.
All other 7 active dims: 0.125 each. Sum = 1.0. facilities_feasibility = 0.0.
"""

import json
import math
from pathlib import Path
import pandas as pd

CACHE_BASE = Path("data/cache/community/nm")
S1_PATH = Path("data/cache/state/nm/s1_community_list_2026-05-28.json")
SENS_MATRIX = Path("analysis/session12/outputs/sensitivity_matrix.csv")
OUTPUT_DIR = Path("analysis/session12/outputs")

ACTIVE_DIMS = [
    "academic_need",
    "competitive_opportunity",
    "charter_saturation",
    "political_climate",
    "authorizer_friendliness",
    "replication_feasibility",
    "population_trends",
    "operational_complexity",
    "funding_environment",
]

STABLE_FOUR = ["nm-questa", "nm-carlsbad", "nm-gallup", "nm-deming"]

# ── Scheme D weights ──────────────────────────────────────────────────────────
# Unnormalized: 7×1.0 + 0.5 + 0.5 = 8.0 total units
# charter_saturation: 0.5/8 = 0.0625
# replication_feasibility: 0.5/8 = 0.0625
# all others: 1.0/8 = 0.125

WEIGHTS_D = {
    "academic_need":           0.125,
    "competitive_opportunity":  0.125,
    "charter_saturation":       0.0625,
    "political_climate":        0.125,
    "authorizer_friendliness":  0.125,
    "replication_feasibility":  0.0625,
    "population_trends":        0.125,
    "operational_complexity":   0.125,
    "funding_environment":      0.125,
    "facilities_feasibility":   0.0,
}

weight_sum = sum(WEIGHTS_D[d] for d in ACTIVE_DIMS)
assert abs(weight_sum - 1.0) < 1e-9, f"Weights sum to {weight_sum}, not 1.0"

# ── Load canonical cities ─────────────────────────────────────────────────────

with open(S1_PATH) as f:
    s1 = json.load(f)
canonical_ids = [
    c["community_id"] for c in s1["communities"]
    if c["community_id"] != "nm-cleveland"
]

# ── Load scorecards ───────────────────────────────────────────────────────────

def load_scores(cid):
    path = CACHE_BASE / cid / "s5_scorecard.json"
    if not path.exists():
        return None
    with open(path) as f:
        sc = json.load(f)
    dims = sc.get("dimensions", {})
    row = {"city": cid}
    missing = []
    for dim in ACTIVE_DIMS:
        if dim in dims:
            row[dim] = dims[dim]["score"]
        else:
            row[dim] = float("nan")
            missing.append(dim)
    present = sum(1 for d in ACTIVE_DIMS if d in dims)
    if present < 8:
        return None  # below inclusion threshold
    row["_missing"] = missing
    return row


records = []
excluded = []
for cid in canonical_ids:
    row = load_scores(cid)
    if row is None:
        excluded.append(cid)
    else:
        records.append(row)

# ── Compute Scheme D composite ────────────────────────────────────────────────

def composite_d(row):
    """Weighted sum with proportional redistribution for missing dims."""
    present_w = 0.0
    wv = 0.0
    for dim in ACTIVE_DIMS:
        w = WEIGHTS_D[dim]
        v = row.get(dim, float("nan"))
        if w > 0 and not math.isnan(v):
            wv += w * v
            present_w += w
    if present_w == 0:
        return float("nan")
    return wv / present_w


results_d = []
for row in records:
    score = composite_d(row)
    results_d.append({"city": row["city"], "score_d": round(score, 2)})

results_d.sort(key=lambda x: -x["score_d"])
for i, r in enumerate(results_d):
    r["rank_d"] = i + 1

# ── Load Scheme A reference from sensitivity_matrix.csv ──────────────────────

sens_df = pd.read_csv(SENS_MATRIX, index_col="city")

def get_rank_a(cid):
    if cid not in sens_df.index:
        return None
    raw = int(sens_df.loc[cid, "rank_A"])
    # Adjust for nm-cleveland removal (was rank 2; all ranks >2 shift up by 1)
    return raw - 1 if raw > 2 else raw

def get_score_a(cid):
    if cid not in sens_df.index:
        return None
    return float(sens_df.loc[cid, "score_scheme_A"])

# ── Build output ──────────────────────────────────────────────────────────────

lines = []

lines.append("══════════════════════════════════════════════════════")
lines.append("SCHEME D — Maturity-adjusted")
lines.append("charter_saturation + replication_feasibility halved to 0.0625 each")
lines.append("All other active dimensions: 0.125")
lines.append(f"Weights sum: {weight_sum:.4f} (facilities_feasibility: 0.0)")
lines.append("══════════════════════════════════════════════════════")

header = f"{'Rank':>5} | {'City':<32} | {'Score D':>7} | {'Score A':>7} | {'Rank A':>6} | {'Δ rank':>6}"
divider = f"{'─'*5}|{'─'*34}|{'─'*9}|{'─'*9}|{'─'*8}|{'─'*8}"
lines.append(header)
lines.append(divider)

for r in results_d:
    cid = r["city"]
    rank_d = r["rank_d"]
    score_d = r["score_d"]
    rank_a = get_rank_a(cid)
    score_a = get_score_a(cid)
    if rank_a is None:
        delta_str = "N/A"
        rank_a_str = "N/A"
        score_a_str = "N/A"
    else:
        delta = rank_a - rank_d  # positive = moved up in D vs A
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        rank_a_str = str(rank_a)
        score_a_str = f"{score_a:.2f}"
    lines.append(
        f"{rank_d:>5} | {cid:<32} | {score_d:>7.2f} | {score_a_str:>7} | {rank_a_str:>6} | {delta_str:>6}"
    )

lines.append("")

# ── Questa result ─────────────────────────────────────────────────────────────

questa_d = next(r for r in results_d if r["city"] == "nm-questa")
questa_rank_d = questa_d["rank_d"]
questa_score_d = questa_d["score_d"]
questa_rank_a = get_rank_a("nm-questa")
questa_score_a = get_score_a("nm-questa")

lines.append("══════════════════════════════════════════════════════")
lines.append("QUESTA RESULT")
lines.append("══════════════════════════════════════════════════════")
lines.append(
    f"Scheme A rank: {questa_rank_a}  |  Scheme D rank: {questa_rank_d}  |  "
    f"Δ: {questa_rank_a - questa_rank_d:+d}"
)
lines.append(
    f"Scheme A score: {questa_score_a:.2f}  |  Scheme D score: {questa_score_d:.2f}"
)
lines.append("")

if questa_rank_d == 1:
    interp = "Questa holds rank 1 under Scheme D; halving the two maturity dimensions does not displace it from the top position."
elif questa_rank_d <= 5:
    interp = f"Questa drops from rank {questa_rank_a} (A) to rank {questa_rank_d} (D) but remains in the top 5 when the maturity-correlated dimensions are down-weighted."
else:
    interp = f"Questa drops from rank {questa_rank_a} (A) to rank {questa_rank_d} (D), falling out of the top 5 when the maturity-correlated dimensions are down-weighted."

lines.append(f"Interpretation (factual only):")
lines.append(f"→ {interp}")
lines.append("")

# ── Stability cross-check for original stable four ───────────────────────────

lines.append("══════════════════════════════════════════════════════")
lines.append("STABILITY CROSS-CHECK — original stable four under Scheme D")
lines.append("══════════════════════════════════════════════════════")

for cid in STABLE_FOUR:
    row_d = next((r for r in results_d if r["city"] == cid), None)
    rank_a = get_rank_a(cid)
    if row_d is None:
        lines.append(f"  {cid:<36} NOT IN RESULTS")
    else:
        rank_d = row_d["rank_d"]
        delta = rank_a - rank_d
        delta_str = f"{delta:+d}"
        lines.append(
            f"  {cid:<36} Scheme A rank {rank_a} → Scheme D rank {rank_d}  ({delta_str})"
        )

lines.append("")

# Cities entering top 5 under D that were not in A top 5
d_top5 = {r["city"] for r in results_d if r["rank_d"] <= 5}
a_top5_cities = set()
for cid in canonical_ids:
    ra = get_rank_a(cid)
    if ra is not None and ra <= 5:
        a_top5_cities.add(cid)

new_entrants = d_top5 - a_top5_cities
lines.append("Cities entering top 5 under D that were not in top 5 under A:")
if new_entrants:
    for cid in sorted(new_entrants):
        rank_a = get_rank_a(cid)
        row_d = next(r for r in results_d if r["city"] == cid)
        lines.append(f"  {cid}: A rank {rank_a} → D rank {row_d['rank_d']}")
else:
    lines.append("  none")

output_text = "\n".join(lines)

# ── Print and write ───────────────────────────────────────────────────────────

print(output_text)

out_path = OUTPUT_DIR / "questa_reweight_results.txt"
out_path.write_text(output_text)
print(f"\nResults written to {out_path}")
