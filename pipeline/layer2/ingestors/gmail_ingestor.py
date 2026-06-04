"""
pipeline/layer2/ingestors/gmail_ingestor.py

External-signal-only Gmail ingestor for Layer 2 (Phase 2).

Key constraints (immutable without explicit operator sign-off):
  - External-only: threads where every participant is @themindtrust.org are
    rejected before any processing.
  - Pipeline artifact subjects (briefs, POC, Claude prompts, landscape drafts)
    are excluded at the gate.
  - No LLM extraction — signals are review-tier stubs for operator classification.
  - confidence is always None; confidence_tier is always "review".
  - Evidence text = thread snippet only, ≤300 chars — no full body reads.
  - dimension is always None — operator must assign before blend.

Run modes:
  # dry-run against live Gmail (requires GMAIL_* env vars):
  python -m pipeline.layer2.ingestors.gmail_ingestor --dry-run

  # dry-run against pre-fetched fixture data (no credentials needed):
  python -m pipeline.layer2.ingestors.gmail_ingestor --dry-run --fixture-file FILE

  # live write to Notion (requires NOTION_API_KEY + GMAIL_* env vars):
  python -m pipeline.layer2.ingestors.gmail_ingestor
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── config paths ───────────────────────────────────────────────────────────────
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_DIR = os.path.abspath(os.path.join(_MODULE_DIR, "..", "..", "..", "config"))
_CACHE_PATH = os.path.join(_CONFIG_DIR, "ingest_cache.json")

# ── Gmail REST ─────────────────────────────────────────────────────────────────
_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_SEARCH_QUERY = "charter school newer_than:30d"

# ── external signal filtering ──────────────────────────────────────────────────
_INTERNAL_DOMAIN = "themindtrust.org"

# Case-insensitive substring patterns that flag a subject as a pipeline artifact.
_SUBJECT_EXCLUSION_PATTERNS: tuple[str, ...] = (
    "albuquerque brief",
    "las cruces brief",
    "landscape analysis drafts",
    "proof of concept",
    "claude opus prompt",
)

# Derived from config/states.yaml nm_district_map for the target cities.
# Longest entries first so multi-word names match before single-word prefixes.
_CITY_KEYWORD_SLUG_MAP: dict[str, str] = {
    "truth or consequences": "nm-truth-or-consequences",
    "rio rancho":            "nm-rio-rancho",
    "santa fe":              "nm-santa-fe",
    "las cruces":            "nm-las-cruces",
    "albuquerque":           "nm-albuquerque",
    "carlsbad":              "nm-carlsbad",
    "española":              "nm-espanola",
    "espanola":              "nm-espanola",
    "gallup":                "nm-gallup",
    "roswell":               "nm-roswell",
    "taos":                  "nm-taos",
}

# Personal/non-org addresses that are not community contacts or external partners.
# A thread qualifies only if it has at least one external participant NOT in this set.
_PERSONAL_ADDRESS_EXCLUDE: frozenset[str] = frozenset({
    "bauslander8@gmail.com",
    "b.auslander@ufl.edu",
})

# High-signal phrases for signal_type classification (no LLM).
_POLICY_CLIMATE_PHRASES: frozenset[str] = frozenset({
    "charter law", "authorization", "authorizer", "legislation",
    "per-pupil", "funding formula", "state policy", "renewal", "revocation",
    "regulatory", "ordinance", "statute", "match requirement", "grant does not",
    "grant requires",
})
_COMMUNITY_INTEREST_PHRASES: frozenset[str] = frozenset({
    "community engagement", "outreach", "enrollment", "families", "parents",
    "community interest", "residents", "neighborhood", "community meeting",
    "community support", "family engagement", "team nea",
})


# ── participant helpers ────────────────────────────────────────────────────────

def _email_domain(address: str) -> str:
    at = address.rfind("@")
    return address[at + 1:].lower().strip() if at >= 0 else ""


def _is_internal(address: str) -> bool:
    return _email_domain(address) == _INTERNAL_DOMAIN


def _all_participants(thread: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for msg in thread.get("messages") or []:
        for field in ("sender", "toRecipients", "ccRecipients", "bccRecipients"):
            val = msg.get(field)
            if not val:
                continue
            addrs = [val] if isinstance(val, str) else list(val)
            for addr in addrs:
                addr = addr.strip()
                if addr and addr not in seen:
                    seen.add(addr)
                    out.append(addr)
    return out


def _external_participants(thread: dict) -> list[str]:
    return [a for a in _all_participants(thread) if not _is_internal(a)]


# ── thread metadata helpers ────────────────────────────────────────────────────

def _thread_subject(thread: dict) -> str:
    for msg in thread.get("messages") or []:
        s = (msg.get("subject") or "").strip()
        if s:
            return s
    return ""


def _thread_snippet(thread: dict) -> str:
    for msg in thread.get("messages") or []:
        s = (msg.get("snippet") or "").strip()
        if s:
            return s
    return ""


def _thread_date(thread: dict) -> Optional[date]:
    for msg in thread.get("messages") or []:
        raw = msg.get("date")
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return None


# ── filtering logic ────────────────────────────────────────────────────────────

def _is_excluded_subject(subject: str) -> tuple[bool, str]:
    """Return (excluded, reason). All pattern matching is case-insensitive substring."""
    s = subject.lower()
    for pat in _SUBJECT_EXCLUSION_PATTERNS:
        if pat in s:
            return True, f"subject contains pipeline artifact pattern '{pat}'"
    # Catch any subject with 'brief' + an NM city name
    if "brief" in s:
        for city_kw in _CITY_KEYWORD_SLUG_MAP:
            if city_kw in s:
                return True, f"subject contains 'brief' + NM city name '{city_kw}'"
    return False, ""


def _match_city_slug(text: str) -> Optional[str]:
    text_lower = text.lower()
    for name, slug in _CITY_KEYWORD_SLUG_MAP.items():
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            return slug
    return None


def _classify_signal_type(text: str) -> str:
    text_lower = text.lower()
    for phrase in _POLICY_CLIMATE_PHRASES:
        if phrase in text_lower:
            return "policy_climate"
    for phrase in _COMMUNITY_INTEREST_PHRASES:
        if phrase in text_lower:
            return "community_interest"
    return "external_observation"


# ── signal construction ────────────────────────────────────────────────────────

def _build_signal(thread: dict, external_participants: list[str], dry_run: bool) -> dict:
    """Build a single review-tier signal stub from an external thread."""
    thread_id = thread.get("id", "")
    subject = _thread_subject(thread)
    snippet = _thread_snippet(thread)
    thread_dt = _thread_date(thread)

    search_text = f"{subject} {snippet}"
    city_slug = _match_city_slug(search_text)
    signal_type = _classify_signal_type(search_text)

    # evidence_text: verbatim snippet capped at SignalStore's 300-char limit
    evidence_text = (snippet or subject)[:300]
    # raw_text: up to 500 chars stored in metadata only
    raw_text = (snippet or subject)[:500]

    return {
        # SignalStore canonical fields
        "city":              city_slug,
        "dimension":         None,       # operator must assign; no LLM extraction
        "direction":         "neutral",
        "magnitude":         None,
        "confidence":        None,       # always None for email-sourced signals
        "evidence_text":     evidence_text,
        "source_id":         "[DRY-RUN]" if dry_run else None,
        "source_date":       thread_dt or date.today(),
        "ingested_at":       datetime.now(timezone.utc),
        "status":            "active",
        "confidence_tier":   "review",   # never auto; human review required
        "operator_verified": False,
        "operator_note":     "",
        "superseded_by":     None,
        "source_metadata": {
            "signal_type":          signal_type,
            "source_type":          "gmail",
            "external_participants": external_participants,
            "needs_review":         True,
            "raw_text":             raw_text,
        },
        # display-only metadata stripped before any Notion write
        "_thread_id": thread_id,
        "_subject":   subject,
    }


def _filter_threads(
    threads: list[dict],
) -> tuple[list[tuple[dict, list[str]]], list[tuple[str, str, str]]]:
    """
    Returns:
      included: list of (thread, external_participants) pairs
      excluded: list of (thread_id, subject, reason) triples
    """
    included: list[tuple[dict, list[str]]] = []
    excluded: list[tuple[str, str, str]] = []

    for thread in threads:
        thread_id = thread.get("id", "")
        subject = _thread_subject(thread)

        exc, reason = _is_excluded_subject(subject)
        if exc:
            excluded.append((thread_id, subject, reason))
            continue

        ext = _external_participants(thread)
        if not ext:
            excluded.append((thread_id, subject, "all participants are @themindtrust.org (internal-only thread)"))
            continue

        # Require at least one external participant that isn't a personal/excluded address.
        real_ext = [a for a in ext if a.lower() not in _PERSONAL_ADDRESS_EXCLUDE]
        if not real_ext:
            excluded.append((
                thread_id, subject,
                f"external participants are personal/excluded addresses only: {ext}",
            ))
            continue

        included.append((thread, real_ext))

    return included, excluded


# ── Gmail REST API ─────────────────────────────────────────────────────────────

def _exchange_refresh_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    body = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"[gmail-ext] STOP — token exchange failed: HTTP {exc.code}", file=sys.stderr)
        raise SystemExit(1)
    token = data.get("access_token")
    if not token:
        print(f"[gmail-ext] STOP — no access_token in response: {data}", file=sys.stderr)
        raise SystemExit(1)
    return token


def _http_get(url: str, params: dict, access_token: str) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params) if params else url
    req = urllib.request.Request(full_url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _fetch_threads_rest(query: str = _GMAIL_SEARCH_QUERY) -> list[dict]:
    client_id     = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    if not (client_id and client_secret and refresh_token):
        missing = [n for n, v in [
            ("GMAIL_CLIENT_ID",     client_id),
            ("GMAIL_CLIENT_SECRET", client_secret),
            ("GMAIL_REFRESH_TOKEN", refresh_token),
        ] if not v]
        print(
            f"[gmail-ext] STOP — missing env vars: {missing}\n"
            "  Provide OAuth credentials or use --fixture-file for dry-run testing.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    access_token = _exchange_refresh_token(client_id, client_secret, refresh_token)
    resp = _http_get(
        f"{_GMAIL_API_BASE}/threads",
        {"q": query, "maxResults": 200},
        access_token,
    )
    stubs = resp.get("threads") or []
    threads: list[dict] = []
    for stub in stubs:
        tid = str(stub.get("id") or "")
        if not tid:
            continue
        try:
            full = _http_get(
                f"{_GMAIL_API_BASE}/threads/{tid}",
                {"format": "metadata", "metadataHeaders": "Subject,From,To,Cc,Date"},
                access_token,
            )
            threads.append(full)
        except Exception as exc:
            logger.warning("[gmail-ext] get_thread %s failed: %s — using stub", tid, exc)
            threads.append(stub)
    return threads


# ── dedup cache (mirrors pattern from gmail.py) ────────────────────────────────

def _load_cache(path: str = _CACHE_PATH) -> dict:
    if not os.path.exists(path):
        return {"granola": [], "gmail": [], "gmail_ext": []}
    try:
        with open(path) as f:
            data = json.load(f)
        data.setdefault("gmail_ext", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"granola": [], "gmail": [], "gmail_ext": []}


def _save_cache(cache: dict, path: str = _CACHE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


# ── dry-run output ─────────────────────────────────────────────────────────────

def _print_dry_run(signals: list[dict], excluded: list[tuple[str, str, str]]) -> None:
    W = 72
    SEP = "─" * W

    print(f"\n{'═' * W}")
    print("  GMAIL EXTERNAL INGESTOR — DRY RUN")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'═' * W}\n")

    print(f"SIGNALS THAT WOULD BE WRITTEN ({len(signals)}):")
    print(SEP)

    for i, sig in enumerate(signals, 1):
        meta = sig["source_metadata"]
        ext = meta.get("external_participants", [])
        print(f"\n[Signal {i}]")
        print(f"  thread_id         : {sig['_thread_id']}")
        print(f"  subject           : {sig['_subject']!r}")
        print(f"  external_from     : {', '.join(ext)}")
        print(f"  city              : {sig['city'] or 'None  ← no NM city match; operator assigns'}")
        print(f"  dimension         : {sig['dimension'] or 'None  ← operator assignment required'}")
        print(f"  direction         : {sig['direction']}")
        print(f"  magnitude         : {sig['magnitude']}")
        print(f"  confidence        : {sig['confidence']}")
        print(f"  confidence_tier   : {sig['confidence_tier']}")
        print(f"  operator_verified : {sig['operator_verified']}")
        print(f"  status            : {sig['status']}")
        print(f"  source_date       : {sig['source_date']}")
        print(f"  ingested_at       : {sig['ingested_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"  source_metadata.signal_type : {meta.get('signal_type')}")
        print(f"  source_metadata.needs_review: {meta.get('needs_review')}")
        print(f"  evidence_text     : {sig['evidence_text']!r}")
        print(f"  raw_text (≤500)   : {meta.get('raw_text', '')!r}")

    print(f"\n{SEP}")
    print(f"\nEXCLUDED THREADS ({len(excluded)}):")
    print(SEP)
    for tid, subj, reason in excluded:
        label = subj[:60] + "…" if len(subj) > 60 else subj
        print(f"  [{tid}] {label!r}")
        print(f"    → {reason}")

    print(f"\n{SEP}")
    print(f"SUMMARY: {len(signals)} signal(s) to write | {len(excluded)} thread(s) excluded")
    print(f"NOTE: All signals have confidence=None and confidence_tier='review'.")
    print(f"      No signals touch the P-score pipeline.")
    print(SEP + "\n")


# ── main ───────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print signals that would be written without touching Notion.",
    )
    parser.add_argument(
        "--fixture-file", metavar="FILE",
        help="Load pre-fetched thread data from a JSON file (MCP search_threads format).",
    )
    parser.add_argument(
        "--query", default=_GMAIL_SEARCH_QUERY,
        help=f"Gmail search query (default: '{_GMAIL_SEARCH_QUERY}').",
    )
    args = parser.parse_args(argv)

    # ── load threads ─────────────────────────────────────────────────────────
    if args.fixture_file:
        with open(args.fixture_file) as f:
            data = json.load(f)
        raw_threads = data.get("threads") or (data if isinstance(data, list) else [])
        print(f"[gmail-ext] loaded {len(raw_threads)} threads from fixture: {args.fixture_file}")
    else:
        raw_threads = _fetch_threads_rest(args.query)
        print(f"[gmail-ext] fetched {len(raw_threads)} threads from Gmail REST API")

    # ── filter ────────────────────────────────────────────────────────────────
    included, excluded_list = _filter_threads(raw_threads)

    # ── build signals ─────────────────────────────────────────────────────────
    signals = [_build_signal(t, ext, dry_run=args.dry_run) for t, ext in included]

    # ── output ────────────────────────────────────────────────────────────────
    if args.dry_run:
        _print_dry_run(signals, excluded_list)
        return

    # ── live write to Notion ───────────────────────────────────────────────────
    # Add pipeline/layer2 to path when run as a script (not as a module).
    _project_root = os.path.abspath(os.path.join(_MODULE_DIR, "..", "..", "..", ".."))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    from pipeline.layer2.notion_client import NotionSignalStore  # noqa: PLC0415

    store = NotionSignalStore()
    cache = _load_cache()
    already_seen: set[str] = set(cache.get("gmail_ext", []))
    written = 0

    for sig in signals:
        thread_id = sig.pop("_thread_id")
        subject   = sig.pop("_subject", "")

        if thread_id in already_seen:
            logger.info("[gmail-ext] skipping already-ingested thread %s", thread_id)
            continue

        # city=None: coerce to "" so _city_page_id gets {"equals": ""} not null.
        # The signal is written with an empty city relation — operator assigns on review.
        if sig["city"] is None:
            sig["city"] = ""

        source_id = store.write_source({
            "source_type":  "gmail",
            "external_id":  thread_id,
            "title":        subject or f"Gmail thread {thread_id}",
            "source_date":  sig["source_date"],
        })
        sig["source_id"] = source_id

        try:
            store.write_signal(sig)
            written += 1
        except Exception as exc:
            logger.error("[gmail-ext] write_signal rejected for %s: %s", thread_id, exc)
        finally:
            already_seen.add(thread_id)

    cache["gmail_ext"] = list(already_seen)
    _save_cache(cache)
    print(f"[gmail-ext] {written} signal(s) written to Notion")


if __name__ == "__main__":
    main()
