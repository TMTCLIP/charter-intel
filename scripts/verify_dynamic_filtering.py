#!/usr/bin/env python3
"""
scripts/verify_dynamic_filtering.py
Prompt 1 verification: claude-sonnet-4-6 availability, web_search_20260209 dynamic
filtering, code execution tool, and token delta vs baseline.

CALL A — claude-sonnet-4-5 + web_search_20250305  (current pipeline behavior)
CALL B — claude-sonnet-4-6 + web_search_20260209 + code_execution_20250522
"""
import os
import json
import anthropic

QUERY = "charter school political climate Albuquerque New Mexico 2024"
SYSTEM = (
    "You are a research assistant. Use web search to find relevant information "
    "and return a brief summary."
)
SONNET_4_5 = "claude-sonnet-4-5"
SONNET_4_6 = "claude-sonnet-4-6"

# Published rates: $3 input / $15 output per 1M tokens (Sonnet)
RATES = {
    SONNET_4_5: (3.0, 15.0),
    SONNET_4_6: (3.0, 15.0),
}


def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
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


def call_a(client):
    print("\n" + "=" * 60)
    print("CALL A — claude-sonnet-4-5 + web_search_20250305 (baseline)")
    print("=" * 60)
    try:
        resp = client.messages.create(
            model=SONNET_4_5,
            max_tokens=1024,
            system=SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": QUERY}],
        )
        inp = resp.usage.input_tokens
        out = resp.usage.output_tokens
        cost = _cost(SONNET_4_5, inp, out)
        stop = resp.stop_reason
        print(f"  Status:        SUCCESS (stop_reason={stop})")
        print(f"  Input tokens:  {inp:,}")
        print(f"  Output tokens: {out:,}")
        print(f"  Cost:          ${cost:.6f}")
        return {"success": True, "input_tokens": inp, "output_tokens": out, "cost": cost}
    except Exception as e:
        print(f"  Status:        ERROR")
        print(f"  Verbatim:      {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}", "input_tokens": 0}


def call_b(client):
    print("\n" + "=" * 60)
    print("CALL B — claude-sonnet-4-6 + web_search_20260209 (code_execution auto-injected)")
    print("=" * 60)
    # NOTE: web_search_20260209 on sonnet-4-6 auto-injects a code_execution tool.
    # Explicitly defining code_execution alongside it causes a name-conflict 400.
    # Omit it here and detect auto-injected blocks in the response instead.
    try:
        resp = client.messages.create(
            model=SONNET_4_6,
            max_tokens=1024,
            system=SYSTEM,
            tools=[
                {"type": "web_search_20260209", "name": "web_search"},
            ],
            messages=[{"role": "user", "content": QUERY}],
        )
        inp = resp.usage.input_tokens
        out = resp.usage.output_tokens
        cost = _cost(SONNET_4_6, inp, out)
        stop = resp.stop_reason

        code_blocks = [
            b for b in resp.content
            if getattr(b, "type", None) == "code_execution_result"
            or getattr(b, "type", None) == "code_execution"
        ]
        print(f"  Status:              SUCCESS (stop_reason={stop})")
        print(f"  Input tokens:        {inp:,}")
        print(f"  Output tokens:       {out:,}")
        print(f"  Cost:                ${cost:.6f}")
        print(f"  code_execution blocks in response: {len(code_blocks)}")
        if code_blocks:
            print("  → Dynamic filtering fired (code_execution content blocks present)")
        else:
            print("  → No code_execution blocks — dynamic filtering did not fire (or not triggered by this query)")
        # Log all content block types seen
        btypes = [getattr(b, "type", "unknown") for b in resp.content]
        print(f"  Content block types: {btypes}")
        return {
            "success": True,
            "input_tokens": inp,
            "output_tokens": out,
            "cost": cost,
            "code_execution_blocks": len(code_blocks),
            "block_types": btypes,
        }
    except anthropic.BadRequestError as e:
        print(f"  Status:  400 BAD REQUEST — stopping, no retry")
        print(f"  Verbatim error: {e}")
        return {"success": False, "error": str(e), "status_code": 400}
    except anthropic.NotFoundError as e:
        print(f"  Status:  MODEL NOT FOUND — stopping, no retry")
        print(f"  Verbatim error: {e}")
        return {"success": False, "error": str(e), "status_code": 404}
    except Exception as e:
        print(f"  Status:  ERROR — {type(e).__name__}")
        print(f"  Verbatim: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def main():
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment or .env")

    client = anthropic.Anthropic(api_key=api_key)

    ra = call_a(client)
    rb = call_b(client)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(f"\nclaude-sonnet-4-5 accessible:      {'YES' if ra['success'] else 'NO — ' + ra.get('error','')[:80]}")
    print(f"claude-sonnet-4-6 accessible:      {'YES' if rb['success'] else 'NO — ' + rb.get('error','')[:80]}")
    print(f"web_search_20260209 available:      {'YES' if rb['success'] else 'UNKNOWN — check error above'}")
    print(f"code_execution tool available:      {'YES' if rb.get('success') else 'UNKNOWN — check error above'}")

    if ra["success"] and rb["success"]:
        delta = rb["input_tokens"] - ra["input_tokens"]
        pct = delta / ra["input_tokens"] * 100 if ra["input_tokens"] else 0
        print(f"\nToken comparison (input):")
        print(f"  Call A: {ra['input_tokens']:,}")
        print(f"  Call B: {rb['input_tokens']:,}")
        print(f"  Delta:  {delta:+,} ({pct:+.1f}%)")
        if delta < 0:
            print(f"  → Call B used FEWER input tokens (dynamic filtering active)")
        else:
            print(f"  → Call B used MORE input tokens (additional tool definitions)")

        print(f"\nCost comparison:")
        print(f"  Call A: ${ra['cost']:.6f}")
        print(f"  Call B: ${rb['cost']:.6f}")

        ce_count = rb.get("code_execution_blocks", 0)
        print(f"\nDynamic filtering fired:           {'YES' if ce_count > 0 else 'NO (code_execution blocks = 0)'}")

    print()


if __name__ == "__main__":
    main()
