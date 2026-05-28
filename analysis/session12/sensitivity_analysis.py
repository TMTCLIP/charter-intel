"""
Tasks 3 & 4 — Gate Dimension Distribution + Sensitivity Analysis
Session 12 analysis script. Standalone — run from ~/Downloads/charter-intel/
Task 3 (gate distributions) runs first, then proceeds to sensitivity schemes.
"""

import json
import math
import numpy as np
import pandas as pd
from pathlib import Path

CACHE_BASE = Path("data/cache/community/nm")
S1_PATH = Path("data/cache/state/nm/s1_community_list_2026-05-28.json")
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

ALL_DIMS = ACTIVE_DIMS + ["facilities_feasibility"]

# ═══════════════════════════════════════════════════════════════════════════════
# LOAD DATA (same inclusion rules as Task 2)
# ═══════════════════════════════════════════════════════════════════════════════

with open(S1_PATH) as f:
    s1 = json.load(f)

canonical_ids = [c["community_id"] for c in s1["communities"]]


def load_scores(community_ids):
    records = []
    excluded = []

    for cid in community_ids:
        path = CACHE_BASE / cid / "s5_scorecard.json"
        if not path.exists():
            excluded.append((cid, ["scorecard_file_missing"]))
            continue

        with open(path) as f:
            sc = json.load(f)

        dims = sc.get("dimensions", {})
        row = {"city": cid}
        missing_active = []

        for dim in ACTIVE_DIMS:
            if dim in dims:
                row[dim] = dims[dim]["score"]
            else:
                row[dim] = float("nan")
                missing_active.append(dim)

        # facilities_feasibility loaded but not used in composites
        if "facilities_feasibility" in dims:
            row["facilities_feasibility"] = dims["facilities_feasibility"]["score"]
        else:
            row["facilities_feasibility"] = float("nan")

        active_present = sum(1 for d in ACTIVE_DIMS if d in dims)

        if active_present < 8:
            excluded.append((cid, missing_active))
        else:
            if missing_active:
                row["_missing_active"] = missing_active
            records.append(row)

    return records, excluded


records, global_excluded = load_scores(canonical_ids)

# ── Build working DataFrame ───────────────────────────────────────────────────

rows_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
df = pd.DataFrame(rows_clean).set_index("city")


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 3 — GATE DIMENSION DISTRIBUTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_distribution(series, name):
    s = series.dropna().sort_values()
    cities_sorted = list(s.items())

    p10 = float(np.percentile(s.values, 10))
    p15 = float(np.percentile(s.values, 15))
    p25 = float(np.percentile(s.values, 25))
    p50 = float(np.percentile(s.values, 50))
    p75 = float(np.percentile(s.values, 75))
    p90 = float(np.percentile(s.values, 90))

    mn = float(s.min())
    mx = float(s.max())
    rng = mx - mn
    mean = float(s.mean())
    median = float(s.median())
    std = float(s.std())

    lines = []
    lines.append("─" * 53)
    lines.append(f"DIMENSION: {name}")
    lines.append("─" * 53)
    lines.append(f"Min: {mn:.2f}   Max: {mx:.2f}   Range: {rng:.2f}")
    lines.append(f"Mean: {mean:.2f}   Median: {median:.2f}   Std: {std:.2f}")
    lines.append("")
    lines.append("Percentiles:")
    lines.append(
        f"  p10: {p10:.2f}   p15: {p15:.2f}   p25: {p25:.2f}   "
        f"p50: {p50:.2f}   p75: {p75:.2f}   p90: {p90:.2f}"
    )
    lines.append("")
    lines.append("Cities (sorted ascending):")
    for rank, (city, score) in enumerate(cities_sorted, 1):
        lines.append(f"  {rank:>2}. {city}: {score:.2f}")

    if rng < 2.0:
        lines.append("")
        lines.append("⚠ WARNING: LOW VARIANCE — gate likely inert within NM")

    lines.append("─" * 53)

    return "\n".join(lines), {
        "p10": p10, "p15": p15, "p25": p25,
        "p50": p50, "p75": p75, "p90": p90,
        "min": mn, "max": mx, "range": rng,
        "mean": mean, "median": median, "std": std,
    }


auth_text, auth_stats = compute_distribution(df["authorizer_friendliness"], "authorizer_friendliness")
pol_text, pol_stats = compute_distribution(df["political_climate"], "political_climate")

# Variables used by Scheme C
auth_p10 = auth_stats["p10"]
auth_p15 = auth_stats["p15"]
pol_p10 = pol_stats["p10"]
pol_p15 = pol_stats["p15"]

gate_output = auth_text + "\n\n" + pol_text

gate_path = OUTPUT_DIR / "gate_distributions.txt"
gate_path.write_text(gate_output)

print(gate_output)
print(f"\nGate distributions written to {gate_path}")
print(f"\nScheme C anchors:")
print(f"  auth_p10 = {auth_p10:.2f}   auth_p15 = {auth_p15:.2f}")
print(f"  pol_p10  = {pol_p10:.2f}   pol_p15  = {pol_p15:.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# TASK 4 — SENSITIVITY ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_weights(raw_weights):
    """Normalize a dim→float dict so active dims sum to 1.0, facilities=0.0."""
    total = sum(v for k, v in raw_weights.items() if k != "facilities_feasibility")
    result = {}
    for dim in ACTIVE_DIMS:
        result[dim] = raw_weights.get(dim, 0.0) / total
    result["facilities_feasibility"] = 0.0
    return result


def compute_composite(row, weights):
    """Weighted sum over active dims; skip NaN (treated as missing)."""
    total_w = 0.0
    total_wv = 0.0
    for dim in ACTIVE_DIMS:
        w = weights[dim]
        v = row.get(dim, float("nan"))
        if w > 0 and not math.isnan(v):
            total_wv += w * v
            total_w += w
    if total_w == 0:
        return float("nan")
    return total_wv / total_w * (1.0 / (total_w / sum(weights[d] for d in ACTIVE_DIMS)))


def compute_composite_v2(row, weights):
    """
    Composite = weighted sum / normalized weight sum.
    Handles NaN scores by redistributing weight among present dims.
    """
    present_w = 0.0
    total_nominal_w = sum(weights[d] for d in ACTIVE_DIMS)
    wv = 0.0
    for dim in ACTIVE_DIMS:
        w = weights[dim]
        v = row.get(dim, float("nan"))
        if w > 0 and not math.isnan(v):
            wv += w * v
            present_w += w
    if present_w == 0:
        return float("nan")
    # Re-normalize to present dims
    return wv / present_w


def run_scheme(label, description, weights, df_in, gate_dim=None, gate_floor=None,
               drag_dim=None, drag_floor=None, drag_multiplier=None):
    """
    Returns:
      - ranked_df: DataFrame with city, score, rank (excluded cities absent)
      - excluded_list: [(city, reason, score)]
      - drag_applied: [(city, raw, dragged, drag_dim_score)]
      - weights_used: dict
    """
    norm_w = normalize_weights(weights)

    # Verify facilities_feasibility = 0.0
    assert norm_w.get("facilities_feasibility", 0.0) == 0.0, \
        "facilities_feasibility weight must be 0.0"

    excluded_list = []
    drag_applied = []
    results = []

    for city, row in df_in.iterrows():
        # Gate check
        if gate_dim is not None and gate_floor is not None:
            gate_score = row.get(gate_dim, float("nan"))
            if math.isnan(gate_score) or gate_score < gate_floor:
                excluded_list.append(
                    (city, f"Failed authorizer gate at {gate_floor}", gate_score)
                )
                continue

        composite = compute_composite_v2(row, norm_w)

        # Drag check
        final_composite = composite
        if drag_dim is not None and drag_floor is not None and drag_multiplier is not None:
            drag_score = row.get(drag_dim, float("nan"))
            if not math.isnan(drag_score) and drag_score < drag_floor:
                final_composite = composite * drag_multiplier
                drag_applied.append((city, composite, final_composite, drag_score))

        results.append({"city": city, "score": round(final_composite, 2)})

    ranked = sorted(results, key=lambda x: -x["score"])
    for i, r in enumerate(ranked):
        r["rank"] = i + 1

    ranked_df = pd.DataFrame(ranked).set_index("city")
    return ranked_df, excluded_list, drag_applied, norm_w


# ── Scheme A — Flat ───────────────────────────────────────────────────────────

weights_a = {dim: 1.0 for dim in ACTIVE_DIMS}
weights_a["facilities_feasibility"] = 0.0
ranked_a, excl_a, drag_a, norm_w_a = run_scheme(
    "A", "Flat equal weights",
    weights_a, df
)

# ── Scheme B — Demand-heavy ───────────────────────────────────────────────────

weights_b = {dim: 1.0 for dim in ACTIVE_DIMS}
weights_b["academic_need"] = 2.0
weights_b["competitive_opportunity"] = 2.0
weights_b["charter_saturation"] = 2.0
weights_b["facilities_feasibility"] = 0.0
ranked_b, excl_b, drag_b, norm_w_b = run_scheme(
    "B", "Demand-heavy",
    weights_b, df
)

# ── Scheme C — Gate + drag, swept (15 runs) ───────────────────────────────────

gate_floors = [
    ("2.0", 2.0),
    ("3.0", 3.0),
    ("4.0", 4.0),
    ("p10", auth_p10),
    ("p15", auth_p15),
]

drag_multipliers = [0.5, 0.7, 0.85]

scheme_c_runs = []
for gate_label, gate_floor in gate_floors:
    for drag_mult in drag_multipliers:
        label = f"C_gate{gate_label}_drag{drag_mult}"
        description = (
            f"Gate: authorizer_friendliness < {gate_floor:.2f} ({gate_label}) | "
            f"Drag: political_climate < 3.0 × {drag_mult}"
        )
        weights_c = {dim: 1.0 for dim in ACTIVE_DIMS}
        weights_c["facilities_feasibility"] = 0.0
        ranked, excl, drag, norm_w = run_scheme(
            label, description,
            weights_c, df,
            gate_dim="authorizer_friendliness", gate_floor=gate_floor,
            drag_dim="political_climate", drag_floor=3.0,
            drag_multiplier=drag_mult,
        )
        scheme_c_runs.append({
            "label": label,
            "description": description,
            "gate_label": gate_label,
            "gate_floor": gate_floor,
            "drag_multiplier": drag_mult,
            "ranked": ranked,
            "excluded": excl,
            "drag_applied": drag,
            "weights": norm_w,
        })

assert len(scheme_c_runs) == 15, f"Expected 15 Scheme C runs, got {len(scheme_c_runs)}"


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════

def format_weights(w, active_only=True):
    dims = ACTIVE_DIMS if active_only else list(w.keys())
    parts = [f"{d}: {w[d]:.4f}" for d in dims if w.get(d, 0) > 0]
    return ", ".join(parts)


def format_scheme_block(label, description, weights, ranked_df, excluded_list,
                         drag_applied, is_scheme_c=False, gate_floor_label=None):
    lines = []
    lines.append("═" * 54)
    lines.append(f"SCHEME {label} — {description}")
    lines.append(f"Weights: {format_weights(weights)}")
    lines.append("  facilities_feasibility: 0.0000 (excluded)")
    lines.append("═" * 54)
    lines.append(f"{'Rank':<5} | {'City':<32} | {'Score':<6}")
    lines.append(f"{'─'*5}|{'─'*34}|{'─'*7}")

    for city, row in ranked_df.iterrows():
        lines.append(f"{int(row['rank']):<5} | {city:<32} | {row['score']:.2f}")

    if is_scheme_c and excluded_list:
        lines.append("")
        lines.append(
            f"EXCLUDED (failed authorizer gate at {gate_floor_label}):"
        )
        for city, reason, score in excluded_list:
            s = f"{score:.2f}" if not math.isnan(score) else "N/A"
            lines.append(f"  {city}: authorizer_friendliness = {s}")

    if is_scheme_c and drag_applied:
        lines.append("")
        lines.append("DRAG APPLIED:")
        for city, raw, dragged, drag_score in drag_applied:
            lines.append(
                f"  {city}: raw {raw:.2f} → dragged to {dragged:.2f} "
                f"(political_climate = {drag_score:.2f})"
            )

    lines.append("")
    return "\n".join(lines)


output_blocks = []

# Scheme A
output_blocks.append(
    format_scheme_block("A", "Flat equal weights", norm_w_a, ranked_a, excl_a, drag_a)
)

# Scheme B
output_blocks.append(
    format_scheme_block("B", "Demand-heavy", norm_w_b, ranked_b, excl_b, drag_b)
)

# Scheme C runs
for run in scheme_c_runs:
    output_blocks.append(
        format_scheme_block(
            run["label"], run["description"], run["weights"],
            run["ranked"], run["excluded"], run["drag_applied"],
            is_scheme_c=True,
            gate_floor_label=f"{run['gate_floor']:.2f} ({run['gate_label']})",
        )
    )

# ── Excluded cities section (global) ─────────────────────────────────────────

if global_excluded:
    excl_block = "\nEXCLUDED CITIES (< 8 active dimensions with data):\n"
    for cid, missing in global_excluded:
        excl_block += f"  {cid}: missing {', '.join(missing)}\n"
    output_blocks.insert(0, excl_block)


# ═══════════════════════════════════════════════════════════════════════════════
# STABILITY SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

all_cities = list(df.index)
all_scheme_labels = (
    ["A", "B"]
    + [run["label"] for run in scheme_c_runs]
)

# Build rank matrix: city → scheme → rank (99 if excluded)
rank_matrix = {}
for city in all_cities:
    rank_matrix[city] = {}

for city in all_cities:
    # Scheme A
    rank_matrix[city]["A"] = int(ranked_a.loc[city, "rank"]) if city in ranked_a.index else 99
    # Scheme B
    rank_matrix[city]["B"] = int(ranked_b.loc[city, "rank"]) if city in ranked_b.index else 99

for run in scheme_c_runs:
    label = run["label"]
    excluded_cities = {e[0] for e in run["excluded"]}
    for city in all_cities:
        if city in excluded_cities:
            rank_matrix[city][label] = 99
        elif city in run["ranked"].index:
            rank_matrix[city][label] = int(run["ranked"].loc[city, "rank"])
        else:
            rank_matrix[city][label] = 99


def in_top5(rank):
    return rank != 99 and rank <= 5


# TOP 5 STABLE — in top 5 across ALL 17 schemes
stable_all = [
    city for city in all_cities
    if all(in_top5(rank_matrix[city][s]) for s in all_scheme_labels)
]

# TOP 5 STABLE (A + B only)
stable_ab = [
    city for city in all_cities
    if in_top5(rank_matrix[city]["A"]) and in_top5(rank_matrix[city]["B"])
]

# TOP 5 STABLE (A + B + ≥10 of 15 C runs)
c_labels = [run["label"] for run in scheme_c_runs]
stable_ab_10c = [
    city for city in all_cities
    if (
        in_top5(rank_matrix[city]["A"])
        and in_top5(rank_matrix[city]["B"])
        and sum(1 for c in c_labels if in_top5(rank_matrix[city][c])) >= 10
    )
]

# FRAGILE SIGNALS — rank swing > 5 positions across any two runs
fragile = []
for city in all_cities:
    ranks = [rank_matrix[city][s] for s in all_scheme_labels if rank_matrix[city][s] != 99]
    if not ranks:
        continue
    min_r = min(ranks)
    max_r = max(ranks)
    if max_r - min_r > 5:
        # Find which schemes gave min and max
        min_scheme = min(all_scheme_labels, key=lambda s: rank_matrix[city][s] if rank_matrix[city][s] != 99 else 999)
        max_scheme = max(all_scheme_labels, key=lambda s: rank_matrix[city][s] if rank_matrix[city][s] != 99 else -1)
        fragile.append((city, min_r, max_r, min_scheme, max_scheme))

# GATE-SENSITIVE — top 5 in A and B but excluded or drops in Scheme C
gate_sensitive = []
for city in all_cities:
    if not (in_top5(rank_matrix[city]["A"]) and in_top5(rank_matrix[city]["B"])):
        continue
    excluded_in_c = sum(1 for c in c_labels if rank_matrix[city][c] == 99)
    drops_in_c = sum(1 for c in c_labels if rank_matrix[city][c] != 99 and not in_top5(rank_matrix[city][c]))
    if excluded_in_c > 0 or drops_in_c > 0:
        gate_sensitive.append((city, excluded_in_c, drops_in_c))


def format_stability_summary():
    lines = []
    lines.append("═" * 54)
    lines.append("STABILITY SUMMARY")
    lines.append("═" * 54)
    lines.append("")
    lines.append("TOP 5 STABLE — in top 5 across ALL 17 schemes/runs:")
    if stable_all:
        for c in stable_all:
            lines.append(f"  {c}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("TOP 5 STABLE (A + B only):")
    if stable_ab:
        for c in stable_ab:
            lines.append(f"  {c}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("TOP 5 STABLE (A + B + ≥ 10 of 15 Scheme C runs):")
    if stable_ab_10c:
        for c in stable_ab_10c:
            lines.append(f"  {c}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("FRAGILE SIGNALS — rank swings > 5 positions across any two runs:")
    if fragile:
        for city, min_r, max_r, min_s, max_s in fragile:
            lines.append(f"  {city}: rank range {min_r}–{max_r}")
            lines.append(f"    Schemes: {min_s} to {max_s}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append(
        "GATE-SENSITIVE — top 5 in A and B but excluded or drops out of "
        "top 5 in any Scheme C run:"
    )
    if gate_sensitive:
        for city, excl_n, drop_n in gate_sensitive:
            lines.append(
                f"  {city}: excluded in {excl_n}/15 C runs | "
                f"drops out of top 5 in {drop_n}/15 runs"
            )
    else:
        lines.append("  none")
    lines.append("")
    lines.append("─" * 54)
    lines.append("This summary is factual. It reports positions only.")
    lines.append("It makes no weight recommendations or strategic conclusions.")
    lines.append("═" * 54)
    return "\n".join(lines)


stability_text = format_stability_summary()


# ═══════════════════════════════════════════════════════════════════════════════
# WRITE OUTPUT FILES
# ═══════════════════════════════════════════════════════════════════════════════

# sensitivity_results.txt
results_path = OUTPUT_DIR / "sensitivity_results.txt"
full_results = "\n".join(output_blocks) + "\n\n" + stability_text
results_path.write_text(full_results)
print(f"\nSensitivity results written to {results_path}")

# stability_summary.txt
stability_path = OUTPUT_DIR / "stability_summary.txt"
stability_path.write_text(stability_text)
print(f"Stability summary written to {stability_path}")

# sensitivity_matrix.csv
matrix_rows = []
for city in all_cities:
    row = {"city": city}
    row["score_scheme_A"] = (
        ranked_a.loc[city, "score"] if city in ranked_a.index else float("nan")
    )
    row["rank_A"] = rank_matrix[city]["A"]
    row["rank_B"] = rank_matrix[city]["B"]
    for run in scheme_c_runs:
        row[f"rank_{run['label']}"] = rank_matrix[city][run["label"]]
    matrix_rows.append(row)

matrix_df = pd.DataFrame(matrix_rows).set_index("city")
matrix_path = OUTPUT_DIR / "sensitivity_matrix.csv"
matrix_df.to_csv(matrix_path)
print(f"Sensitivity matrix written to {matrix_path}")

# ── Console summary ───────────────────────────────────────────────────────────

print("\n" + stability_text)

# ── Verification output ───────────────────────────────────────────────────────

print("\n" + "═" * 54)
print("VERIFICATION")
print("═" * 54)
print(f"Cities included in analysis: {len(df)}")
print(f"Cities excluded (global): {len(global_excluded)}")
print(f"Scheme C runs completed: {len(scheme_c_runs)} of 15")

# Verify facilities_feasibility = 0.0 in all schemes
all_weights = [norm_w_a, norm_w_b] + [r["weights"] for r in scheme_c_runs]
ff_check = all(w.get("facilities_feasibility", 0.0) == 0.0 for w in all_weights)
print(f"facilities_feasibility confirmed at weight 0.0 in all schemes: {'YES' if ff_check else 'NO — CHECK FAILED'}")
print("═" * 54)
