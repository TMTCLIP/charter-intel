"""
Task 2 — Correlation Matrix
Session 12 analysis script. Standalone — run from ~/Downloads/charter-intel/
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path

CACHE_BASE = Path("data/cache/community/nm")
S1_PATH = Path("data/cache/state/nm/s1_community_list_2026-05-28.json")
OUTPUT_DIR = Path("analysis/session12/outputs")

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

# ── Load canonical community list ────────────────────────────────────────────

with open(S1_PATH) as f:
    s1 = json.load(f)

canonical_ids = [c["community_id"] for c in s1["communities"]]


# ── Load scorecards ───────────────────────────────────────────────────────────

records = []
excluded = []

for cid in canonical_ids:
    path = CACHE_BASE / cid / "s5_scorecard.json"
    if not path.exists():
        excluded.append((cid, ["scorecard_file_missing"]))
        continue

    with open(path) as f:
        sc = json.load(f)

    dims = sc.get("dimensions", {})
    row = {"city": cid}
    missing_dims = []

    for dim in DIMENSIONS:
        if dim in dims:
            row[dim] = dims[dim]["score"]
        else:
            row[dim] = float("nan")
            missing_dims.append(dim)

    present_count = sum(1 for d in DIMENSIONS if d in dims)

    if present_count < 8:
        excluded.append((cid, missing_dims))
    else:
        if missing_dims:
            row["_missing"] = missing_dims
        records.append(row)

print(f"\nTask 1 Summary:")
print(f"  File path pattern: data/cache/community/nm/{{community_id}}/s5_scorecard.json")
print(f"  Total NM cities with score data: {len(records) + len(excluded)}")
print(f"  Cities passing inclusion threshold (≥8 dims): {len(records)}")
print(f"\nExample record ({records[0]['city']}):")
for dim in DIMENSIONS:
    print(f"  {dim}: {records[0].get(dim, 'NaN')}")

# ── Build DataFrame ───────────────────────────────────────────────────────────

rows_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
df = pd.DataFrame(rows_clean).set_index("city")
df = df[DIMENSIONS]

# ── Pearson correlation (pairwise complete observations) ─────────────────────

corr = df.corr(method="pearson", min_periods=2).round(2)
n_cities = len(df)

# ── Write CSV ─────────────────────────────────────────────────────────────────

csv_path = OUTPUT_DIR / "correlation_matrix.csv"
with open(csv_path, "w") as f:
    f.write(f"# Pearson correlation matrix — {n_cities} NM cities included\n")
corr.to_csv(csv_path, mode="a")
print(f"\nCorrelation matrix written to {csv_path}")

# ── Build report ──────────────────────────────────────────────────────────────

lines = []
lines.append("─" * 53)
lines.append("CORRELATION MATRIX REPORT")
lines.append(f"Cities included: {n_cities} | Cities excluded: {len(excluded)}")
lines.append("─" * 53)
lines.append("")

# Full matrix — aligned columns
lines.append("FULL MATRIX (10×10):")
lines.append("")

col_w = 24
dim_abbr = {d: d[:col_w] for d in DIMENSIONS}

header = " " * 26 + "  ".join(f"{d[:8]:>8}" for d in DIMENSIONS)
lines.append(header)
lines.append("-" * len(header))

for row_dim in DIMENSIONS:
    row_label = f"{row_dim:<24}"
    vals = "  ".join(
        f"{corr.loc[row_dim, col_dim]:>8.2f}" for col_dim in DIMENSIONS
    )
    lines.append(f"{row_label}  {vals}")

lines.append("")

# High correlation pairs
high_pairs = []
mod_pairs = []

for i, d1 in enumerate(DIMENSIONS):
    for d2 in DIMENSIONS[i + 1 :]:
        r = corr.loc[d1, d2]
        if pd.isna(r):
            continue
        if abs(r) > 0.70:
            high_pairs.append((d1, d2, r))
        elif abs(r) > 0.50:
            mod_pairs.append((d1, d2, r))

lines.append("HIGH CORRELATION PAIRS (r > 0.70):")
if high_pairs:
    for d1, d2, r in sorted(high_pairs, key=lambda x: -abs(x[2])):
        lines.append(f"  [{d1}] × [{d2}]: r = {r:.2f}")
        lines.append(
            "  → These dimensions may be measuring the same underlying construct."
        )
        lines.append(
            "    Double-counting risk in composite if both carry weight."
        )
else:
    lines.append("  (none)")
lines.append("")

lines.append("MODERATE CORRELATION PAIRS (0.50 < r ≤ 0.70):")
if mod_pairs:
    for d1, d2, r in sorted(mod_pairs, key=lambda x: -abs(x[2])):
        lines.append(f"  [{d1}] × [{d2}]: r = {r:.2f}")
else:
    lines.append("  (none)")
lines.append("")

lines.append("NOTE ON facilities_feasibility:")
lines.append("  → Shown for informational purposes only.")
lines.append("    Excluded from all composite calculations.")
lines.append("")

lines.append("EXCLUDED CITIES:")
if excluded:
    for cid, missing in excluded:
        lines.append(f"  {cid}: missing {', '.join(missing)}")
else:
    lines.append("  (none)")
lines.append("─" * 53)

report_text = "\n".join(lines)

report_path = OUTPUT_DIR / "correlation_report.txt"
report_path.write_text(report_text)
print(f"Correlation report written to {report_path}")

print("\n" + report_text)
