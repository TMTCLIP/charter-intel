"""
pipeline/utils/token_logger.py
Token usage logger for the charter intelligence pipeline.

PURPOSE:
  Accumulate per-call token counts across all Claude API calls in a run,
  write them to a CSV, and print a per-stage summary at the end.

  This module is instrumentation-only. It has no effect on pipeline logic.
  All state is held in a module-level singleton (_logger) so every stage
  that imports api_client.py shares the same accumulator.

USAGE (called automatically by api_client.py — do not call from stages):
  from pipeline.utils.token_logger import token_logger
  token_logger.log_call(stage, model, tokens_input, tokens_output, community_id)

  # In main.py after the pipeline run:
  token_logger.finalize(run_id="nm_growth_20260526_143012")

PRICING NOTE:
  Costs are estimated using Anthropic list prices as of 2026-05.
  Verify at https://www.anthropic.com/pricing before using for budget decisions.

  claude-opus-4-5:          $15.00 / $75.00  per MTok (in/out)
  claude-sonnet-4-5:         $3.00 / $15.00  per MTok (in/out)
  claude-haiku-4-5-*:        $0.80 /  $4.00  per MTok (in/out)
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ─────────────────────────────────────────────
# PRICING TABLE
# Keys must match model strings used in pipeline/__init__.py
# Values: (input_price_per_million, output_price_per_million)
# ─────────────────────────────────────────────
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4-5":              (15.00, 75.00),
    "claude-sonnet-4-5":             (3.00, 15.00),
    "claude-haiku-4-5-20251001":     (0.80,  4.00),
}

_DEFAULT_PRICE: tuple[float, float] = (3.00, 15.00)   # Sonnet tier fallback


def _price_for_model(model: str) -> tuple[float, float]:
    """Return (input_$/MTok, output_$/MTok) for a model string.
    Falls back to Sonnet pricing if the model is unrecognised."""
    for key, prices in _PRICE_TABLE.items():
        if model.startswith(key) or key.startswith(model):
            return prices
    return _DEFAULT_PRICE


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Return estimated USD cost for one API call."""
    price_in, price_out = _price_for_model(model)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class _CallRecord:
    timestamp: str
    run_id: str
    community_id: str
    stage: str
    model: str
    tokens_input: int
    tokens_output: int
    tokens_total: int
    estimated_cost_usd: float

    def as_csv_row(self) -> list:
        return [
            self.timestamp,
            self.run_id,
            self.community_id,
            self.stage,
            self.model,
            self.tokens_input,
            self.tokens_output,
            self.tokens_total,
            f"{self.estimated_cost_usd:.6f}",
        ]

    @staticmethod
    def csv_header() -> list[str]:
        return [
            "timestamp",
            "run_id",
            "community_id",
            "stage",
            "model",
            "tokens_input",
            "tokens_output",
            "tokens_total",
            "estimated_cost_usd",
        ]


# ─────────────────────────────────────────────
# SINGLETON LOGGER
# ─────────────────────────────────────────────

class TokenLogger:
    """Module-level singleton that accumulates token records for one run."""

    def __init__(self) -> None:
        self._records: list[_CallRecord] = []
        self._run_id: str = "unset"

    def set_run_id(self, run_id: str) -> None:
        """Call once from main.py before the pipeline starts."""
        self._run_id = run_id
        self._records = []   # reset for the new run

    def log_call(
        self,
        stage: str,
        model: str,
        tokens_input: int,
        tokens_output: int,
        community_id: str = "",
    ) -> None:
        """Record one API call. Called automatically from api_client.call_claude()."""
        record = _CallRecord(
            timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            run_id=self._run_id,
            community_id=community_id,
            stage=stage,
            model=model,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=tokens_input + tokens_output,
            estimated_cost_usd=_estimate_cost(model, tokens_input, tokens_output),
        )
        self._records.append(record)

    # ── Output ────────────────────────────────

    def write_csv(self, path: str) -> None:
        """Write all records to a CSV file. Creates parent directories as needed."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(_CallRecord.csv_header())
            for r in self._records:
                writer.writerow(r.as_csv_row())

    def print_summary(self) -> None:
        """Print a per-stage cost breakdown to stdout."""
        if not self._records:
            print("\n[TOKEN AUDIT] No API calls recorded for this run.")
            return

        # Aggregate by stage
        by_stage: dict[str, dict] = {}
        for r in self._records:
            if r.stage not in by_stage:
                by_stage[r.stage] = {
                    "calls": 0,
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "cost": 0.0,
                    "models": set(),
                }
            agg = by_stage[r.stage]
            agg["calls"] += 1
            agg["tokens_input"] += r.tokens_input
            agg["tokens_output"] += r.tokens_output
            agg["cost"] += r.estimated_cost_usd
            agg["models"].add(r.model)

        total_in    = sum(r.tokens_input  for r in self._records)
        total_out   = sum(r.tokens_output for r in self._records)
        total_cost  = sum(r.estimated_cost_usd for r in self._records)
        total_calls = len(self._records)

        # Sort stages by cost descending
        sorted_stages = sorted(by_stage.items(), key=lambda x: x[1]["cost"], reverse=True)

        print(f"\n{'='*72}")
        print(f"  TOKEN USAGE SUMMARY  |  run_id: {self._run_id}")
        print(f"{'='*72}")
        print(
            f"  {'Stage':<40} {'Calls':>5} {'In':>8} {'Out':>8} "
            f"{'Total':>8} {'Cost':>10}"
        )
        print(f"  {'-'*40} {'-'*5} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")

        for stage_id, agg in sorted_stages:
            models_str = ", ".join(sorted(agg["models"]))
            total_tok = agg["tokens_input"] + agg["tokens_output"]
            print(
                f"  {stage_id:<40} {agg['calls']:>5} "
                f"{agg['tokens_input']:>8,} {agg['tokens_output']:>8,} "
                f"{total_tok:>8,} ${agg['cost']:>9.4f}"
            )
            print(f"    {'model: ' + models_str:<66}")

        print(f"  {'-'*40} {'-'*5} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")
        print(
            f"  {'TOTAL':<40} {total_calls:>5} "
            f"{total_in:>8,} {total_out:>8,} "
            f"{total_in + total_out:>8,} ${total_cost:>9.4f}"
        )
        print(f"{'='*72}")
        print(f"  Pricing: Anthropic list prices (see token_logger.py for rates).")
        print(f"  NOTE: Does not include S2 cache-hits (state context reused).")
        print(f"{'='*72}\n")

    @property
    def run_id(self) -> str:
        return self._run_id

    def get_community_summary(self, community_id: str) -> list[dict]:
        """Return per-stage token summary for one community, sorted by cost desc."""
        by_stage: dict[str, dict] = {}
        for r in self._records:
            if r.community_id != community_id:
                continue
            if r.stage not in by_stage:
                by_stage[r.stage] = {
                    "stage": r.stage,
                    "model": r.model,
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "cost": 0.0,
                }
            agg = by_stage[r.stage]
            agg["tokens_input"] += r.tokens_input
            agg["tokens_output"] += r.tokens_output
            agg["cost"] += r.estimated_cost_usd
        return sorted(by_stage.values(), key=lambda x: x["cost"], reverse=True)

    def print_scan_summary(self, community_count: int) -> None:
        """Print compact scan mode cost summary after a statewide scan run."""
        total_cost   = sum(r.estimated_cost_usd for r in self._records)
        total_tokens = sum(r.tokens_total        for r in self._records)
        avg_cost     = total_cost / community_count if community_count > 0 else 0.0

        print(f"\n{'='*50}")
        print(f"  SCAN MODE COST SUMMARY")
        print(f"{'='*50}")
        print(f"  Cities:         {community_count}")
        print(f"  Total tokens:   {total_tokens:,}")
        print(f"  Total cost:     ${total_cost:.2f}")
        print(f"  Avg cost/city:  ${avg_cost:.2f}")
        print(f"{'='*50}\n")

    def finalize(self, run_id: Optional[str] = None) -> None:
        """
        Write CSV and print summary. Call once from main.py at end of run.

        Args:
            run_id: If provided, also sets the run_id (useful if run_id was
                    built in main.py after set_run_id was called).
        """
        if run_id:
            self._run_id = run_id
        csv_path = f"data/logs/token_usage_{self._run_id}.csv"
        self.write_csv(csv_path)
        self.print_summary()
        print(f"  Token log written to: {csv_path}\n")


# Module-level singleton — imported by api_client.py
token_logger = TokenLogger()
