#!/usr/bin/env python3
"""
scripts/test_s6_haiku.py
S6 Haiku Quality Gate — tests whether claude-haiku-4-5-20251001 can replace
claude-sonnet-4-5 on S6 synthesis + audit without quality loss.

Calls made:
  A — Sonnet synthesis baseline  (replay from fixture, $0.00)
  B — Haiku synthesis candidate  (REAL API call, ~$0.035)
  C — Sonnet audit baseline      (replay from fixture, $0.00)
  D — Haiku audit candidate      (REAL API call, ~$0.018)

Five evaluation criteria:
  1. Numerical fidelity       — key numbers in Haiku brief match Sonnet baseline
  2. Reliability caveats      — scale / data-quality language preserved
  3. SMALL_MARKET flag        — explicit enrollment scale-constraint language present
  4. Style / length           — word count within ±20% of Sonnet; no filler phrases
  5. Audit accuracy           — Haiku-as-auditor matches Sonnet on pass/fail + failures

Usage:
  cd /path/to/charter-intel
  python3 scripts/test_s6_haiku.py

Requires ANTHROPIC_API_KEY in environment or .env file.
"""
from __future__ import annotations

import json
import os
import re
import sys

# ── .env loader ──────────────────────────────────────────────────────────────
# Use unconditional assignment so Claude Code's ANTHROPIC_API_KEY="" is
# overridden by the real key in .env.
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
if os.path.isfile(_ENV_PATH):
    with open(_ENV_PATH) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()

import anthropic  # noqa: E402  (imported after .env is loaded)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_FIXTURES = os.path.join(_ROOT, "tests", "fixtures", "nm-questa")

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-5"

# Pricing per 1M tokens (USD)
PRICE = {
    SONNET_MODEL: (3.00, 15.00),
    HAIKU_MODEL:  (0.80,  4.00),
}

# Filler phrases that should not appear in the brief
FILLER_PHRASES = [
    "it is worth noting",
    "it is important to note",
    "in conclusion",
    "in summary",
    "to summarize",
    "it should be noted",
    "needless to say",
    "as mentioned earlier",
    "as previously stated",
]

# Key numbers that MUST appear in the Haiku brief (from the Sonnet baseline
# and the underlying fixture input data).
KEY_NUMBERS = {
    "composite_score (6.5)":      "6.5",
    "enrollment total (300)":     "300",
    "FRL pct (73)":               "73",
    "ELA proficiency (28)":       "28",
    "math proficiency (5.3)":     "5.3",
    "enrollment growth (11.7)":   "11.7",
}

SEP  = "─" * 72
SEP2 = "═" * 72


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_fixture(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _strip_fences(text: str) -> str:
    """Remove ```json … ``` or ``` … ``` wrapping, if present."""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text.strip()


def _parse_json(text: str) -> dict | None:
    """Parse JSON from text, stripping markdown fences first."""
    try:
        return json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None


def _cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICE[model]
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def _call_api(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    messages: list,
    max_tokens: int,
    temperature: float,
) -> tuple[str, int, int]:
    """Make a real API call. Returns (text, tokens_in, tokens_out)."""
    response = client.messages.create(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = "".join(
        b.text for b in response.content if hasattr(b, "text")
    )
    return text, response.usage.input_tokens, response.usage.output_tokens


# ─────────────────────────────────────────────────────────────────────────────
# EVALUATION — five criteria
# ─────────────────────────────────────────────────────────────────────────────

def eval_numerical_fidelity(haiku_text: str) -> tuple[bool, list[str]]:
    """Criterion 1: key numbers from Sonnet baseline appear in Haiku brief."""
    issues = []
    for label, value_str in KEY_NUMBERS.items():
        if value_str not in haiku_text:
            issues.append(f"Missing {label}: {value_str!r} not found in text")
    return (len(issues) == 0), issues


def eval_reliability_caveats(haiku_text: str) -> tuple[bool, list[str]]:
    """Criterion 2: data-quality / caveat language preserved.

    Checks for the three classes of reliability framing that appear in the
    Sonnet baseline for Questa.  We do NOT require 'insufficient/unverified'
    language — that only appears when source data is thin, and Questa's brief
    does not contain it (the Sonnet model correctly omitted it because the
    data coverage was adequate).
    """
    checks = [
        (
            "confidence / overstate / needs-verification language",
            r"confidence_overall|may overstate|needs_verification|overstate|MODERATE|LOW confidence",
        ),
        (
            "FRL / poverty / economic-need language",
            r"FRL|free.{0,5}reduced|poverty|low.income|economically.disadvantaged",
        ),
        (
            "operational-risk language (rural / complexity / challenge / viability)",
            r"rural|operational.complex|challenge.{0,10}viabilit|viabilit.{0,10}challenge"
            r"|isolation|high.poverty",
        ),
    ]
    issues = []
    for label, pattern in checks:
        if not re.search(pattern, haiku_text, re.IGNORECASE):
            issues.append(f"Missing: {label}")
    return (len(issues) == 0), issues


def eval_small_market_flag(haiku_text: str) -> tuple[bool, list[str]]:
    """Criterion 3: SMALL_MARKET keyword + explicit enrollment-scale constraint."""
    issues = []
    if not re.search(r"SMALL_MARKET", haiku_text):
        issues.append("SMALL_MARKET keyword absent from brief")
    if not re.search(
        r"300|1[,.]?500|1500|threshold|overstate|scale|addressable.demand",
        haiku_text, re.IGNORECASE,
    ):
        issues.append(
            "No enrollment scale-constraint language found "
            "(expected: 300, threshold, overstate, or similar)"
        )
    return (len(issues) == 0), issues


def eval_style_length(sonnet_text: str, haiku_text: str) -> tuple[bool, list[str]]:
    """Criterion 4: word count within ±20% of Sonnet; no filler phrases."""
    issues = []

    # Strip fences before comparing word counts so JSON content is compared
    s_words = _word_count(_strip_fences(sonnet_text))
    h_words = _word_count(_strip_fences(haiku_text))
    ratio = h_words / s_words if s_words else 0.0

    if not (0.80 <= ratio <= 1.20):
        issues.append(
            f"Word count out of ±20% range: Haiku {h_words} words, "
            f"Sonnet {s_words} words ({ratio:.0%})"
        )

    haiku_lower = haiku_text.lower()
    for phrase in FILLER_PHRASES:
        if phrase in haiku_lower:
            issues.append(f'Filler phrase detected: "{phrase}"')

    return (len(issues) == 0), issues


def eval_audit_accuracy(
    sonnet_audit: dict | None,
    haiku_audit: dict | None,
) -> tuple[bool, list[str]]:
    """Criterion 5: Haiku-as-auditor matches Sonnet on pass/fail + failure count."""
    issues = []

    if haiku_audit is None:
        return False, ["Haiku audit response could not be parsed as JSON"]

    # audit_passed must match
    s_passed = (sonnet_audit or {}).get("audit_passed", True)
    h_passed = haiku_audit.get("audit_passed")
    if h_passed is None:
        issues.append("audit_passed field missing from Haiku audit response")
    elif h_passed != s_passed:
        issues.append(
            f"audit_passed mismatch: Sonnet={s_passed}, Haiku={h_passed}"
        )

    # Failure count must match exactly
    s_fail = len((sonnet_audit or {}).get("failures", []))
    h_fail = len(haiku_audit.get("failures", []))
    if h_fail != s_fail:
        issues.append(
            f"Failures count mismatch: Sonnet={s_fail}, Haiku={h_fail}"
        )

    # Warnings: allow ±2 variance (qualitative, model-dependent)
    s_warn = len((sonnet_audit or {}).get("warnings", []))
    h_warn = len(haiku_audit.get("warnings", []))
    if abs(h_warn - s_warn) > 2:
        issues.append(
            f"Warnings count far off: Sonnet={s_warn}, Haiku={h_warn} "
            f"(delta={abs(h_warn - s_warn)}, threshold=2)"
        )

    return (len(issues) == 0), issues


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY not set.\n"
            "  Add it to .env in the project root or export it.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Check fixtures exist ─────────────────────────────────────────────────
    syn_path   = os.path.join(_FIXTURES, "s6_synthesis",       "call_0.json")
    audit_path = os.path.join(_FIXTURES, "s6_synthesis_audit", "call_0.json")
    for path in (syn_path, audit_path):
        if not os.path.isfile(path):
            print(
                f"ERROR: fixture not found: {path}\n"
                "  Run:  python3 main.py 'Questa' --depth standard "
                "--preset maturity_adjusted --record",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── Load fixtures ────────────────────────────────────────────────────────
    print("Loading fixtures …", flush=True)
    syn_fix   = _load_fixture(syn_path)
    audit_fix = _load_fixture(audit_path)

    syn_req   = syn_fix["request"]
    audit_req = audit_fix["request"]

    # ── Call A — Sonnet synthesis baseline (fixture replay, $0.00) ───────────
    print("Call A — Sonnet synthesis baseline   (fixture replay, $0.00)")
    sonnet_syn_text = syn_fix["response"]["content"][0]["text"]
    sonnet_syn_in   = syn_fix["response"]["usage"]["input_tokens"]
    sonnet_syn_out  = syn_fix["response"]["usage"]["output_tokens"]
    sonnet_brief    = _parse_json(sonnet_syn_text)

    # ── Call B — Haiku synthesis (REAL API CALL) ─────────────────────────────
    print(f"Call B — Haiku synthesis candidate   (REAL API call) …", flush=True)
    client = anthropic.Anthropic(api_key=api_key)
    haiku_syn_text, haiku_syn_in, haiku_syn_out = _call_api(
        client,
        model=HAIKU_MODEL,
        system=syn_req["system"],
        messages=syn_req["messages"],
        max_tokens=syn_req["max_tokens"],
        temperature=syn_req["temperature"],
    )
    haiku_brief = _parse_json(haiku_syn_text)
    print(f"       → {haiku_syn_in:,} in / {haiku_syn_out:,} out tokens")

    # ── Call C — Sonnet audit baseline (fixture replay, $0.00) ──────────────
    print("Call C — Sonnet audit baseline       (fixture replay, $0.00)")
    sonnet_audit_text = audit_fix["response"]["content"][0]["text"]
    sonnet_audit_in   = audit_fix["response"]["usage"]["input_tokens"]
    sonnet_audit_out  = audit_fix["response"]["usage"]["output_tokens"]
    sonnet_audit      = _parse_json(sonnet_audit_text)

    # ── Call D — Haiku audit (REAL API CALL) ─────────────────────────────────
    # Haiku audits the same Sonnet-generated brief (identical messages payload
    # as Call C) — so we test the auditor in isolation, not the synthesizer.
    print(f"Call D — Haiku audit candidate       (REAL API call) …", flush=True)
    haiku_audit_text, haiku_audit_in, haiku_audit_out = _call_api(
        client,
        model=HAIKU_MODEL,
        system=audit_req["system"],
        messages=audit_req["messages"],
        max_tokens=audit_req["max_tokens"],
        temperature=audit_req["temperature"],
    )
    haiku_audit = _parse_json(haiku_audit_text)
    print(f"       → {haiku_audit_in:,} in / {haiku_audit_out:,} out tokens")

    # ── Cost accounting ──────────────────────────────────────────────────────
    sonnet_syn_cost   = _cost_usd(SONNET_MODEL, sonnet_syn_in,  sonnet_syn_out)
    sonnet_audit_cost = _cost_usd(SONNET_MODEL, sonnet_audit_in, sonnet_audit_out)
    haiku_syn_cost    = _cost_usd(HAIKU_MODEL,  haiku_syn_in,   haiku_syn_out)
    haiku_audit_cost  = _cost_usd(HAIKU_MODEL,  haiku_audit_in, haiku_audit_out)

    sonnet_total = sonnet_syn_cost + sonnet_audit_cost
    haiku_total  = haiku_syn_cost  + haiku_audit_cost
    savings_pct  = (1.0 - haiku_total / sonnet_total) * 100 if sonnet_total else 0.0

    # ── Run all five criteria ────────────────────────────────────────────────
    results: list[tuple[str, bool, list[str]]] = [
        ("1. Numerical fidelity",
         *eval_numerical_fidelity(haiku_syn_text)),
        ("2. Reliability caveats",
         *eval_reliability_caveats(haiku_syn_text)),
        ("3. SMALL_MARKET flag language",
         *eval_small_market_flag(haiku_syn_text)),
        ("4. Style / length",
         *eval_style_length(sonnet_syn_text, haiku_syn_text)),
        ("5. Audit accuracy",
         *eval_audit_accuracy(sonnet_audit, haiku_audit)),
    ]

    all_passed = all(r[1] for r in results)
    n_pass = sum(1 for r in results if r[1])
    n_fail = len(results) - n_pass

    # ── Print report ─────────────────────────────────────────────────────────
    print()
    print(SEP2)
    print("  S6 HAIKU QUALITY GATE — nm-questa")
    print(SEP2)

    print()
    print("COST COMPARISON")
    print(SEP)
    print(f"  Sonnet synthesis:  {sonnet_syn_in:>6,} in / {sonnet_syn_out:>5,} out  →  ${sonnet_syn_cost:.4f}")
    print(f"  Haiku  synthesis:  {haiku_syn_in:>6,} in / {haiku_syn_out:>5,} out  →  ${haiku_syn_cost:.4f}")
    print(f"  Sonnet audit:      {sonnet_audit_in:>6,} in / {sonnet_audit_out:>5,} out  →  ${sonnet_audit_cost:.4f}")
    print(f"  Haiku  audit:      {haiku_audit_in:>6,} in / {haiku_audit_out:>5,} out  →  ${haiku_audit_cost:.4f}")
    print(SEP)
    print(f"  Sonnet S6 total  (fixture baseline):  ${sonnet_total:.4f}")
    print(f"  Haiku  S6 total  (this test):         ${haiku_total:.4f}")
    print(f"  Estimated savings per city:            {savings_pct:.1f}%")

    print()
    print("QUALITY CRITERIA")
    print(SEP)
    for name, passed, issues in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        for issue in issues:
            print(f"         ✗ {issue}")

    print()
    print("VERDICT")
    print(SEP)
    if all_passed:
        print(f"  ✅  ALL {len(results)} CRITERIA PASSED")
        print(f"  Haiku appears viable for S6 synthesis + audit on this sample.")
        print(f"  Estimated savings: {savings_pct:.1f}% vs Sonnet baseline.")
    else:
        print(f"  ❌  {n_fail}/{len(results)} CRITERIA FAILED")
        print(f"  Haiku does NOT meet the quality bar for S6 on this sample.")

    # ── Full output dumps ────────────────────────────────────────────────────
    print()
    print(SEP2)
    print("  HAIKU SYNTHESIS BRIEF (full response)")
    print(SEP2)
    print(haiku_syn_text)

    print()
    print(SEP2)
    print("  SONNET SYNTHESIS BRIEF (full response — fixture baseline)")
    print(SEP2)
    print(sonnet_syn_text)

    print()
    print(SEP2)
    print("  HAIKU AUDIT RESPONSE (full response)")
    print(SEP2)
    print(haiku_audit_text)

    print()
    print(SEP2)
    print("  SONNET AUDIT RESPONSE (full response — fixture baseline)")
    print(SEP2)
    print(sonnet_audit_text)

    # Exit 0 = all pass, 1 = any fail
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
