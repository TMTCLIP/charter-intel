#!/usr/bin/env python3
"""
scripts/verify_model_parity.py
Prompt 2 verification: Sonnet 4.6 vs 4.5 parity + Haiku A/B on S3 main extraction.

Uses nm-questa S3 cached facts as ground truth.
Reconstructs the S3 main extraction prompt from template + cached inputs,
then runs it against three models for field-level diff.
"""
import os
import json
import anthropic

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

SONNET_4_5  = "claude-sonnet-4-5"
SONNET_4_6  = "claude-sonnet-4-6"
HAIKU_4_5   = "claude-haiku-4-5-20251001"

# Published rates per MTok (input, output)
RATES = {
    SONNET_4_5: (3.0,  15.0),
    SONNET_4_6: (3.0,  15.0),
    HAIKU_4_5:  (0.80,  4.0),
}

S3_SYSTEM = "You are a structured data extraction agent. Respond ONLY with valid JSON."

COMMUNITY_ID   = "nm-questa"
COMMUNITY_NAME = "Questa"
STATE_CODE     = "NM"
STATE_NAME     = "New Mexico"
TODAY_DATE     = "2026-05-28"


def _load_env():
    env_path = os.path.join(REPO_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()  # override=True (same as dotenv)


def _cost(model, inp, out):
    r_in, r_out = RATES.get(model, (3.0, 15.0))
    return inp / 1_000_000 * r_in + out / 1_000_000 * r_out


def _load_template():
    path = os.path.join(REPO_ROOT, "prompts", "extract", "s3_community_facts.md")
    with open(path) as f:
        return f.read()


def _substitute(template, subs):
    text = template
    for k, v in subs.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def _build_prompt():
    """Reconstruct the S3 main extraction prompt for nm-questa from cached inputs."""
    # State context (condensed, same as pipeline does)
    ctx_path = os.path.join(REPO_ROOT, "data", "cache", "state", "nm", "s2_state_context.json")
    with open(ctx_path) as f:
        ctx = json.load(f)
    state_context_condensed = {
        "charter_law": ctx.get("charter_law", {}),
        "governance":  ctx.get("governance", {}),
        "ecosystem": {
            "major_advocacy_orgs": ctx.get("ecosystem", {}).get("major_advocacy_orgs", [])[:3],
            "notable_operators":   ctx.get("ecosystem", {}).get("notable_operators", [])[:5],
        },
    }

    # Known schools
    known_schools = ["Roots and Wings Community School"]

    # VERIFIED_ROSTER — reconstructed from charter CSV data
    verified_roster = (
        "| School Name | Authorizer | Grades Served | Enrollment Cap |\n"
        "|---|---|---|---|\n"
        "| Roots and Wings Community School | Public Education Commission | K-8 | 60 |"
    )

    # VERIFIED_PED_DATA — reconstructed from S3 facts (SY 2025 values)
    verified_ped_data = (
        "VERIFIED PED DATA (SY 2025) — treat as ground truth:\n"
        "- ELA proficiency: 28.0%\n"
        "- Math proficiency: 5.33%\n"
        "- Source: https://webnew.ped.state.nm.us/bureaus/accountability/achievement-data-by-year/"
    )

    # VERIFIED_NCES_DATA — reconstructed from S3 facts (FY 2023 values)
    verified_nces_data = (
        "VERIFIED NCES DATA (FY 2023) — treat as ground truth:\n"
        "- Per-pupil revenue vs. NM state avg: +6.2%\n"
        "- Per-pupil expenditure: $23,577\n"
        "- Revenue mix: 18.8% federal / 62.7% state / 18.5% local\n"
        "- FRL%: 73.0% of students qualify for free/reduced-price lunch\n"
        "- Source: https://nces.ed.gov/ccd/files.asp"
    )

    # VERIFIED_ACS_DATA — Questa has no ACS district mapping (very small community)
    verified_acs_data = (
        "(No verified Census ACS ELL data available — CENSUS_API_KEY not set or no district mapping)"
    )

    template = _load_template()
    prompt = _substitute(template, {
        "COMMUNITY_NAME":    COMMUNITY_NAME,
        "STATE_NAME":        STATE_NAME,
        "STATE_CODE":        STATE_CODE,
        "COMMUNITY_ID":      COMMUNITY_ID,
        "TODAY_DATE":        TODAY_DATE,
        "STATE_CONTEXT_JSON": json.dumps(state_context_condensed, indent=2),
        "KNOWN_SCHOOLS":     json.dumps(known_schools, indent=2),
        "VERIFIED_ROSTER":   verified_roster,
        "VERIFIED_PED_DATA": verified_ped_data,
        "VERIFIED_NCES_DATA": verified_nces_data,
        "VERIFIED_ACS_DATA": verified_acs_data,
    })
    return prompt


def _run_extraction(client, model, prompt):
    print(f"  Running {model}...", end="", flush=True)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8192,
            temperature=0.0,
            system=S3_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        inp = resp.usage.input_tokens
        out = resp.usage.output_tokens
        cost = _cost(model, inp, out)
        raw = resp.content[0].text if resp.content else ""

        # Strip markdown fences if model wraps output
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f" PARSE ERROR")
            return None, inp, out, cost, f"JSONDecodeError: {e}"

        print(f" OK ({inp:,} in / {out:,} out / ${cost:.5f})")
        return parsed, inp, out, cost, None
    except Exception as e:
        print(f" ERROR: {type(e).__name__}: {e}")
        return None, 0, 0, 0.0, f"{type(e).__name__}: {e}"


def _extract_facts_by_key(parsed):
    """Return dict of fact_key → fact dict from parsed S3 output."""
    if not parsed:
        return {}
    facts = parsed.get("facts", [])
    result = {}
    for f in facts:
        key = f.get("fact_key")
        if key:
            result[key] = f
    return result


def _val_matches(v1, v2, tol=0.05):
    """Loose match: same value or numerically within 5% for floats."""
    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False
    if type(v1) != type(v2):
        try:
            return abs(float(v1) - float(v2)) / (abs(float(v1)) + 1e-9) < tol
        except (TypeError, ValueError):
            return False
    if isinstance(v1, float):
        return abs(v1 - v2) / (abs(v1) + 1e-9) < tol
    return v1 == v2


def main():
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    # Load baseline (cached nm-questa S3 output)
    cache_path = os.path.join(
        REPO_ROOT, "data", "cache", "community", "nm", "nm-questa", "s3_facts_raw.json"
    )
    with open(cache_path) as f:
        baseline_cache = json.load(f)
    baseline_facts = _extract_facts_by_key(baseline_cache)
    print(f"Loaded nm-questa S3 cache: {len(baseline_facts)} fact keys")

    # Build the reconstruction prompt
    print("Reconstructing S3 main extraction prompt from template + cached inputs...")
    prompt = _build_prompt()
    print(f"Prompt length: {len(prompt):,} chars\n")

    # Run all three models
    print("=" * 60)
    print("RUNNING EXTRACTIONS (no web search — main extraction call only)")
    print("=" * 60)

    r45, inp45, out45, cost45, err45 = _run_extraction(client, SONNET_4_5, prompt)
    r46, inp46, out46, cost46, err46 = _run_extraction(client, SONNET_4_6, prompt)
    rHk, inpHk, outHk, costHk, errHk = _run_extraction(client, HAIKU_4_5, prompt)

    # Parse each into fact_key dicts
    facts45 = _extract_facts_by_key(r45)
    facts46 = _extract_facts_by_key(r46)
    factsHk = _extract_facts_by_key(rHk)

    # All known keys (union of baseline cache + all three model outputs)
    all_keys = sorted(set(list(baseline_facts.keys()) + list(facts45.keys()) + list(facts46.keys()) + list(factsHk.keys())))

    print("\n" + "=" * 60)
    print("FIELD-LEVEL PARITY TABLE")
    print("(Baseline = cached S3 run; models run against reconstructed prompt)")
    print("=" * 60)

    haiku_divergences = []
    s46_divergences   = []

    header = f"{'fact_key':<42} {'baseline':>10} {'4-5':>10} {'4-6':>10} {'Haiku':>10}  {'4-6 match':>10}  {'Haiku match':>11}"
    print(header)
    print("-" * len(header))

    for key in all_keys:
        bv = baseline_facts.get(key, {}).get("value")
        v45 = facts45.get(key, {}).get("value")
        v46 = facts46.get(key, {}).get("value")
        vHk = factsHk.get(key, {}).get("value")

        # Compare 4-6 vs 4-5 (if we got 4-5 output; else compare vs baseline)
        ref_val = v45 if v45 is not None else bv
        match46 = "OK" if _val_matches(ref_val, v46) else "DIFF"
        matchHk = "OK" if _val_matches(ref_val, vHk) else "DIFF"

        if match46 == "DIFF":
            s46_divergences.append({"key": key, "ref": ref_val, "v46": v46})
        if matchHk == "DIFF":
            haiku_divergences.append({"key": key, "ref": ref_val, "haiku": vHk})

        def fmt(v):
            if v is None:
                return "None"
            if isinstance(v, float):
                return f"{v:.2f}"
            return str(v)[:10]

        print(
            f"{key:<42} {fmt(bv):>10} {fmt(v45):>10} {fmt(v46):>10} {fmt(vHk):>10}"
            f"  {match46:>10}  {matchHk:>11}"
        )

    # Divergence detail
    if s46_divergences:
        print(f"\n{'='*60}")
        print(f"SONNET 4-6 DIVERGENCES ({len(s46_divergences)} fields)")
        print(f"{'='*60}")
        for d in s46_divergences:
            print(f"  {d['key']}: ref={d['ref']}  4-6={d['v46']}")
    else:
        print(f"\nSonnet 4-6 divergences: NONE (full parity with 4-5 / baseline)")

    if haiku_divergences:
        print(f"\n{'='*60}")
        print(f"HAIKU DIVERGENCES ({len(haiku_divergences)} fields)")
        print(f"{'='*60}")
        for d in haiku_divergences:
            print(f"  {d['key']}: ref={d['ref']}  haiku={d['haiku']}")
    else:
        print(f"\nHaiku divergences: NONE (full parity)")

    # Errors
    for label, err in [("Sonnet 4-5", err45), ("Sonnet 4-6", err46), ("Haiku", errHk)]:
        if err:
            print(f"\n{label} ERROR: {err}")

    # Token + cost summary
    print(f"\n{'='*60}")
    print("TOKEN AND COST COMPARISON")
    print(f"{'='*60}")
    print(f"{'Model':<30} {'Input':>8} {'Output':>8} {'Cost':>12}")
    print(f"{'-'*60}")
    for label, inp, out, cost, err in [
        (SONNET_4_5, inp45, out45, cost45, err45),
        (SONNET_4_6, inp46, out46, cost46, err46),
        (HAIKU_4_5,  inpHk, outHk, costHk, errHk),
    ]:
        if err:
            print(f"{label:<30} {'ERROR':>8}")
        else:
            print(f"{label:<30} {inp:>8,} {out:>8,} ${cost:>10.5f}")

    # Assessment
    print(f"\n{'='*60}")
    print("ASSESSMENT")
    print(f"{'='*60}")
    if not err46:
        safe46 = len(s46_divergences) == 0
        print(f"Sonnet 4-6 safe drop-in for 4-5: {'YES — zero field divergences' if safe46 else f'CAUTION — {len(s46_divergences)} field(s) differ'}")
    else:
        print(f"Sonnet 4-6: CALL FAILED — cannot assess")

    if not errHk:
        safe_haiku = len(haiku_divergences) <= 2
        print(f"Haiku safe for S3 main extraction: {'POSSIBLY — low divergence count, verify manually' if safe_haiku else f'NO — {len(haiku_divergences)} diverging fields'}")
    else:
        print(f"Haiku: CALL FAILED — cannot assess")
    print()


if __name__ == "__main__":
    main()
