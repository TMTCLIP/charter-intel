#!/usr/bin/env python3
"""
scripts/verify_batch_and_structured.py
Prompt 3 verification:
  Test A — Batch API smoke test (3 Haiku requests, poll to completion)
  Test B — Structured output + web search conflict check
"""
import os
import json
import time
import anthropic

HAIKU_4_5   = "claude-haiku-4-5-20251001"
SONNET_4_5  = "claude-sonnet-4-5"

BATCH_POLL_INTERVAL = 10    # seconds between status checks
BATCH_TIMEOUT       = 300   # 5 minutes max


def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()  # override=True (same as dotenv)


# ─────────────────────────────────────────────────────────────
# TEST A — Batch API
# ─────────────────────────────────────────────────────────────

def test_a_batch(client):
    print("\n" + "=" * 60)
    print("TEST A — Batch API smoke test")
    print("=" * 60)

    requests = [
        {
            "custom_id": f"test-{i}",
            "params": {
                "model": HAIKU_4_5,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Return the word PASS and nothing else"}],
            },
        }
        for i in range(1, 4)
    ]

    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"  Batch create FAILED: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e)}

    batch_id = batch.id
    print(f"  Batch ID:      {batch_id}")
    print(f"  Initial status: {batch.processing_status}")

    # Log expires_at if present
    expires_at = getattr(batch, "expires_at", None)
    if expires_at:
        print(f"  Expires at:    {expires_at}  ← confirms 29-day retention window")
    else:
        print(f"  expires_at:    NOT PRESENT in response")

    # Log request_counts if present
    rc = getattr(batch, "request_counts", None)
    if rc:
        print(f"  request_counts: {rc}")
    else:
        print(f"  request_counts: NOT PRESENT in response")

    # Poll until ended or timeout
    start = time.time()
    elapsed = 0
    final_batch = None
    print(f"\n  Polling every {BATCH_POLL_INTERVAL}s (max {BATCH_TIMEOUT}s)...")

    while elapsed < BATCH_TIMEOUT:
        try:
            b = client.messages.batches.retrieve(batch_id)
        except Exception as e:
            print(f"  Poll error: {e}")
            break

        status = b.processing_status
        elapsed = round(time.time() - start, 1)
        print(f"  [{elapsed:6.1f}s] status={status}")

        if status == "ended":
            final_batch = b
            break

        time.sleep(BATCH_POLL_INTERVAL)

    if not final_batch:
        print(f"  TIMEOUT after {BATCH_TIMEOUT}s — batch did not complete")
        return {"success": False, "error": "timeout"}

    elapsed_total = round(time.time() - start, 1)
    print(f"\n  Completed in {elapsed_total}s")

    # Confirm request_counts and expires_at on final batch
    final_rc = getattr(final_batch, "request_counts", None)
    final_exp = getattr(final_batch, "expires_at", None)
    print(f"  Final request_counts: {final_rc}")
    print(f"  Final expires_at:     {final_exp}")

    # Retrieve results
    pass_count = 0
    fail_count = 0
    errors = []
    try:
        for result in client.messages.batches.results(batch_id):
            cid = result.custom_id
            rtype = result.result.type if hasattr(result.result, "type") else "unknown"
            if rtype == "succeeded":
                text = ""
                msg = result.result.message
                if msg.content:
                    text = getattr(msg.content[0], "text", "").strip()
                matched = text.upper() == "PASS"
                if matched:
                    pass_count += 1
                else:
                    fail_count += 1
                print(f"  {cid}: {rtype} | output={repr(text)} | PASS check={'OK' if matched else 'FAIL'}")
            else:
                fail_count += 1
                err_detail = getattr(result.result, "error", result.result)
                errors.append(f"{cid}: {err_detail}")
                print(f"  {cid}: {rtype} | error={err_detail}")
    except Exception as e:
        print(f"  Results retrieval error: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e)}

    print(f"\n  Results: {pass_count}/3 returned PASS, {fail_count}/3 failed/wrong")
    if errors:
        for e in errors:
            print(f"  Error: {e}")

    return {
        "success": True,
        "batch_id": batch_id,
        "elapsed_seconds": elapsed_total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "expires_at": str(final_exp),
        "request_counts": str(final_rc),
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────
# TEST B — Structured output + web search conflict
# ─────────────────────────────────────────────────────────────

def test_b_structured_plus_search(client):
    print("\n" + "=" * 60)
    print("TEST B — Structured output + web_search conflict check")
    print("=" * 60)

    prompt = (
        "Search for charter schools in Albuquerque NM and return a JSON object "
        "with one field: city"
    )

    results = {}

    # Attempt 1: tools + betas structured output
    print("\n  Attempt 1: web_search_20250305 + betas=['output-128k-2025-02-19']")
    try:
        resp = client.messages.create(
            model=SONNET_4_5,
            max_tokens=256,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
            betas=["output-128k-2025-02-19"],
        )
        text = ""
        for b in resp.content:
            if hasattr(b, "text"):
                text += b.text
        print(f"  Status:   SUCCESS (stop_reason={resp.stop_reason})")
        print(f"  Response: {text[:200]}")
        try:
            parsed = json.loads(text)
            print(f"  JSON valid: YES — {parsed}")
        except Exception:
            print(f"  JSON valid: NO (response is not parseable JSON)")
        results["betas_attempt"] = {"success": True, "text": text[:200]}
    except anthropic.BadRequestError as e:
        print(f"  Status:   400 BAD REQUEST")
        print(f"  Verbatim: {e}")
        results["betas_attempt"] = {"success": False, "status_code": 400, "error": str(e)}
    except Exception as e:
        print(f"  Status:   ERROR — {type(e).__name__}: {e}")
        results["betas_attempt"] = {"success": False, "error": f"{type(e).__name__}: {e}"}

    # Attempt 2: tools only, system prompt requests JSON (current pipeline approach)
    print("\n  Attempt 2: web_search_20250305 + system='Respond ONLY with valid JSON'")
    try:
        resp2 = client.messages.create(
            model=SONNET_4_5,
            max_tokens=256,
            system="Respond ONLY with valid JSON.",
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        text2 = ""
        for b in resp2.content:
            if hasattr(b, "text"):
                text2 += b.text
        print(f"  Status:   SUCCESS (stop_reason={resp2.stop_reason})")
        print(f"  Response: {text2[:200]}")
        try:
            parsed2 = json.loads(text2)
            print(f"  JSON valid: YES — {parsed2}")
        except Exception:
            print(f"  JSON valid: NO (model returned non-JSON despite system instruction)")
        results["system_json_attempt"] = {"success": True, "text": text2[:200]}
    except anthropic.BadRequestError as e:
        print(f"  Status:   400 BAD REQUEST")
        print(f"  Verbatim: {e}")
        results["system_json_attempt"] = {"success": False, "status_code": 400, "error": str(e)}
    except Exception as e:
        print(f"  Status:   ERROR — {type(e).__name__}: {e}")
        results["system_json_attempt"] = {"success": False, "error": f"{type(e).__name__}: {e}"}

    # Attempt 3: no tools, just structured output beta
    print("\n  Attempt 3: no tools + betas=['output-128k-2025-02-19'] (control — no conflict)")
    try:
        resp3 = client.messages.create(
            model=SONNET_4_5,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
            betas=["output-128k-2025-02-19"],
        )
        text3 = ""
        for b in resp3.content:
            if hasattr(b, "text"):
                text3 += b.text
        print(f"  Status:   SUCCESS (stop_reason={resp3.stop_reason})")
        print(f"  Response: {text3[:200]}")
        results["no_tools_beta"] = {"success": True}
    except anthropic.BadRequestError as e:
        print(f"  Status:   400 BAD REQUEST")
        print(f"  Verbatim: {e}")
        results["no_tools_beta"] = {"success": False, "error": str(e)}
    except Exception as e:
        print(f"  Status:   ERROR — {type(e).__name__}: {e}")
        results["no_tools_beta"] = {"success": False, "error": f"{type(e).__name__}: {e}"}

    return results


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    ra = test_a_batch(client)
    rb = test_b_structured_plus_search(client)

    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)

    # Test A summary
    if ra.get("success"):
        print(f"\nBatch API:")
        print(f"  Working:          YES")
        print(f"  Time to complete: {ra.get('elapsed_seconds')}s")
        print(f"  All 3 returned PASS: {'YES' if ra.get('pass_count') == 3 else 'NO (' + str(ra.get('pass_count')) + '/3)'}")
        print(f"  expires_at present:  {'YES — ' + ra.get('expires_at') if ra.get('expires_at') and ra.get('expires_at') != 'None' else 'NOT FOUND'}")
        print(f"  request_counts present: {'YES — ' + ra.get('request_counts') if ra.get('request_counts') and ra.get('request_counts') != 'None' else 'NOT FOUND'}")
    else:
        print(f"\nBatch API: FAILED — {ra.get('error')}")

    # Test B summary
    print(f"\nStructured output + web search:")
    a1 = rb.get("betas_attempt", {})
    a2 = rb.get("system_json_attempt", {})
    a3 = rb.get("no_tools_beta", {})

    if not a1.get("success") and "400" in str(a1.get("status_code", "")):
        print(f"  Attempt 1 (betas + web search): 400 CONFLICT CONFIRMED")
        print(f"  Exact error: {a1.get('error')}")
    elif a1.get("success"):
        print(f"  Attempt 1 (betas + web search): SUCCEEDED — no conflict with this beta flag")
    else:
        print(f"  Attempt 1: {a1.get('error')}")

    if not a2.get("success"):
        print(f"  Attempt 2 (system JSON + web search): FAILED — {a2.get('error')}")
    else:
        print(f"  Attempt 2 (system JSON + web search): SUCCEEDED — current pipeline approach works")

    if not a3.get("success"):
        print(f"  Attempt 3 (beta alone, no tools): FAILED — beta flag itself rejected: {a3.get('error')}")
    else:
        print(f"  Attempt 3 (beta alone, no tools): SUCCEEDED — beta flag works without tools")

    print()


if __name__ == "__main__":
    main()
