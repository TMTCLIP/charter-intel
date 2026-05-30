#!/usr/bin/env python3
"""
scripts/session18_ranking_delta.py

NO-COST ranking-delta check for Session 18 (Areas C + D).

Re-scores every community that has a cached S4 verified bundle, IN MEMORY,
using the current (post-Session-18) S5 logic, and compares to the cached
s5_scorecard.json (produced by pre-Session-18 logic). Reports:
  - composite score deltas
  - data_coverage_tier changes (esp. new INSUFFICIENT)
  - competitive_opportunity dimension changes (the Area D demand-evidence gate)
  - rank-order changes for maturity_adjusted vs growth

Reads only cached S4 + cached scorecards + config. Writes NOTHING. No API calls.

Usage:  python3 scripts/session18_ranking_delta.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import yaml

from pipeline.s5_scoring import score_dimension

PRESETS = ["maturity_adjusted", "growth"]
_GATED = {"unreliable", "INSUFFICIENT"}


def _load_cfg():
    with open("config/scoring_weights.yaml") as f:
        weights = yaml.safe_load(f)
    with open("config/scoring_presets.yaml") as f:
        presets = yaml.safe_load(f)
    return weights, presets


def _score_in_memory(bundle: dict, weights_cfg: dict, preset_weights: dict) -> dict:
    """Replicate s5_scoring.run() composite logic in memory (no disk writes)."""
    dims = {}
    for dim_name, dim_def in weights_cfg["dimensions"].items():
        w = preset_weights.get(dim_name, 0.0)
        dims[dim_name] = score_dimension(dim_name, dim_def, bundle, w)

    excluded = [n for n, d in dims.items() if d.get("used_default")]
    included = {n: d for n, d in dims.items() if not d.get("used_default")}
    wsum = sum(d["weight"] for d in included.values())
    if wsum > 0:
        composite = round(sum(d["score"] * (d["weight"] / wsum) for d in included.values()), 2)
    else:
        composite = 5.0

    total_w = sum(d["weight"] for d in dims.values())
    real_w = sum(d["weight"] for d in included.values())
    cov_pct = round(real_w / total_w * 100, 1) if total_w > 0 else 0.0
    cov = weights_cfg.get("data_coverage_thresholds", {"reliable": 70, "provisional": 50})
    if cov_pct >= cov["reliable"]:
        tier = "reliable"
    elif cov_pct >= cov["provisional"]:
        tier = "provisional"
    else:
        tier = "unreliable"
    if len(excluded) >= 4:
        tier = "INSUFFICIENT"

    return {
        "composite": composite,
        "data_coverage_tier": tier,
        "excluded": excluded,
        "competitive_opportunity": dims.get("competitive_opportunity", {}),
    }


def main():
    weights_cfg, presets_cfg = _load_cfg()
    s4_files = sorted(glob.glob("data/cache/community/*/*/s4_verified.json"))
    print(f"Found {len(s4_files)} cached S4 bundles.\n")

    for preset in PRESETS:
        preset_weights = presets_cfg[preset]["dimensions"]
        rows = []
        for s4_path in s4_files:
            cid = os.path.basename(os.path.dirname(s4_path))
            with open(s4_path) as f:
                bundle = json.load(f)
            new = _score_in_memory(bundle, weights_cfg, preset_weights)

            # old cached scorecard (pre-Session-18). Only present for some presets.
            sc_path = os.path.join(os.path.dirname(s4_path), "s5_scorecard.json")
            old_comp = old_tier = old_co = None
            if os.path.exists(sc_path):
                with open(sc_path) as f:
                    old = json.load(f)
                if old.get("preset") == preset:
                    old_comp = old.get("composite_score")
                    old_tier = old.get("data_coverage_tier")
                    old_co = (old.get("dimensions", {}).get("competitive_opportunity", {}) or {}).get("score")

            co = new["competitive_opportunity"]
            rows.append({
                "cid": cid,
                "new_comp": new["composite"],
                "old_comp": old_comp,
                "new_tier": new["data_coverage_tier"],
                "old_tier": old_tier,
                "new_co": co.get("score"),
                "old_co": old_co,
                "co_gated": bool(co.get("used_default")),
                "n_excluded": len(new["excluded"]),
            })

        rows.sort(key=lambda r: r["new_comp"], reverse=True)
        print(f"================ PRESET: {preset} ================")
        print(f"{'rank':>4} {'community':<28} {'new':>5} {'old':>5} {'Δ':>6} "
              f"{'cov_tier(new/old)':<26} {'comp_opp(new/old)':<18} gated")
        for i, r in enumerate(rows, 1):
            delta = (round(r["new_comp"] - r["old_comp"], 2)
                     if r["old_comp"] is not None else None)
            tierstr = f"{r['new_tier']}/{r['old_tier'] or '—'}"
            costr = f"{r['new_co']}/{r['old_co'] if r['old_co'] is not None else '—'}"
            mark = " *" if r["co_gated"] else ""
            flag = " <<INSUFF" if r["new_tier"] == "INSUFFICIENT" else ""
            print(f"{i:>4} {r['cid']:<28} {r['new_comp']:>5} "
                  f"{(r['old_comp'] if r['old_comp'] is not None else '—'):>5} "
                  f"{(delta if delta is not None else '—'):>6} "
                  f"{tierstr:<26} {costr:<18}{mark}{flag}")
        n_insuff = sum(1 for r in rows if r["new_tier"] == "INSUFFICIENT")
        n_gated = sum(1 for r in rows if r["co_gated"])
        print(f"\n  INSUFFICIENT tier: {n_insuff}/{len(rows)} communities")
        print(f"  competitive_opportunity gated (Area D): {n_gated}/{len(rows)} communities\n")


if __name__ == "__main__":
    main()
