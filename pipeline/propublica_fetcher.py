"""
pipeline/propublica_fetcher.py
Local philanthropic-activity signal from the free, keyless ProPublica
Nonprofit Explorer API (IRS Form 990 data).

STEP-0 FINDING:
  The search.json endpoint does NOT return financials — only ein, name,
  ntee_code, city, state, etc. Asset and revenue figures live on the per-EIN
  detail endpoint /organizations/{ein}.json (fields asset_amount,
  revenue_amount). This fetcher therefore searches, filters by NTEE, then makes
  one detail call per qualifying EIN (capped) to obtain financials.

METHOD:
  1. search q=education&state[id]={ST}&city={CITY}  + q=charter+school&state[id]={ST}
  2. keep orgs whose ntee_code starts with B (Education) or R (Civil
     Rights/Community); drop post-secondary (B4*/B5*) and university/hospital
     names.
  3. per-EIN detail call -> asset_amount, revenue_amount; drop likely-national
     orgs (revenue_amount > $500M).
  4. tier: ACTIVE (>=3 orgs or total_assets > $10M) / MODERATE (1-2) / LIMITED (0).

CACHE:  data/cache/community/{state}/{cid}/propublica_foundations.json (TTL 90d)
ERROR CONTRACT:  never raises; returns None on failure.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_SEARCH_API = "https://projects.propublica.org/nonprofits/api/v2/search.json"
_ORG_API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
SOURCE_URL = "https://projects.propublica.org/nonprofits/"
SOURCE_TITLE = "ProPublica Nonprofit Explorer (IRS Form 990 data)"

CACHE_TTL_DAYS = 90
_NATIONAL_REVENUE_CEILING = 500_000_000     # orgs above this are likely national → skip
_ACTIVE_ASSET_FLOOR = 10_000_000            # total assets above this → ACTIVE
_MAX_DETAIL_LOOKUPS = 50                    # bound per-EIN detail calls
_EXCLUDE_NAME_TOKENS = ("UNIVERSITY", "COLLEGE", "HOSPITAL", "MEDICAL",
                        "HEALTH SYSTEM", "REGENTS")


def _cache_path(state: str, community_id: str) -> str:
    return os.path.join(
        "data", "cache", "community", state.lower(), community_id,
        "propublica_foundations.json",
    )


def _read_cache(state: str, community_id: str) -> Optional[dict]:
    path = _cache_path(state, community_id)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 86400 > CACHE_TTL_DAYS:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(state: str, community_id: str, data: dict) -> None:
    path = _cache_path(state, community_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("propublica_fetcher: cache write failed (%s) — %s", path, exc)


def _get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("propublica_fetcher: request failed for %s — %s", url, exc)
        return None


def _search(query: str, state: str, city: Optional[str]) -> list:
    params = {"q": query, "state[id]": state.upper()}
    if city:
        params["city"] = city
    url = _SEARCH_API + "?" + urllib.parse.urlencode(params)
    resp = _get(url)
    if not resp:
        return []
    return resp.get("organizations", []) or []


def _ntee_qualifies(ntee_code: Optional[str], name: str) -> bool:
    code = (ntee_code or "").strip().upper()
    if not code:
        return False
    if not (code.startswith("B") or code.startswith("R")):
        return False
    # Drop post-secondary education (universities, colleges, grad schools).
    if code.startswith("B4") or code.startswith("B5"):
        return False
    upper_name = name.upper()
    if any(tok in upper_name for tok in _EXCLUDE_NAME_TOKENS):
        return False
    return True


def _pos_int(val) -> int:
    try:
        n = int(val)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _fetch_org_financials(ein) -> Optional[dict]:
    resp = _get(_ORG_API.format(ein=ein))
    if not resp:
        return None
    org = resp.get("organization") or {}
    return {
        "ein": ein,
        "name": (org.get("name") or "").strip(),
        "ntee_code": org.get("ntee_code"),
        "asset_amount": _pos_int(org.get("asset_amount")),
        "revenue_amount": _pos_int(org.get("revenue_amount")),
    }


def _format_assets(total: int) -> str:
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M"
    if total >= 1_000:
        return f"{total / 1_000:.0f}K"
    return str(total)


def get_philanthropic_activity(
    city: str,
    state: str,
    community_id: str,
) -> Optional[dict]:
    """Return a philanthropic-activity signal for a city, or None on failure.

    Return shape:
        {
            "foundation_count": 4,
            "total_assets": 23450000,
            "total_assets_formatted": "23.5M",
            "largest_foundation": {"name": "...", "asset_amount": 12000000},
            "activity_tier": "ACTIVE",
            "city": "Albuquerque", "state": "NM",
            "source_url": "...", "source_title": "...",
            "confidence": "MODERATE",
        }
    """
    if not city or not state:
        return None

    cached = _read_cache(state, community_id)
    if cached is not None:
        log.info("propublica_fetcher: cache hit for %s", community_id)
        return cached

    # 1 — gather candidate orgs from both searches.
    candidates = _search("education", state, city) + _search("charter school", state, None)
    if not candidates:
        # Distinguish API failure (no result at all) from a genuine empty city.
        # A reachable API returns an (often empty) organizations list, so treat
        # total absence as a soft LIMITED rather than None only if at least one
        # search call succeeded. We cannot tell here, so return None on failure.
        log.warning("propublica_fetcher: no search results for %s, %s", city, state)
        return None

    # 2 — NTEE pre-filter + dedup by EIN (cap detail calls).
    seen: set = set()
    qualifying_eins: list = []
    for org in candidates:
        ein = org.get("ein")
        if ein in (None, "") or ein in seen:
            continue
        if not _ntee_qualifies(org.get("ntee_code"), org.get("name", "")):
            continue
        seen.add(ein)
        qualifying_eins.append(ein)
        if len(qualifying_eins) >= _MAX_DETAIL_LOOKUPS:
            break

    # 3 — per-EIN detail lookups for financials + national-org exclusion.
    orgs: list = []
    total_assets = 0
    largest = {"name": None, "asset_amount": 0}
    for ein in qualifying_eins:
        fin = _fetch_org_financials(ein)
        if fin is None:
            continue
        if fin["revenue_amount"] > _NATIONAL_REVENUE_CEILING:
            continue   # likely national org — skip
        orgs.append(fin)
        total_assets += fin["asset_amount"]
        if fin["asset_amount"] > largest["asset_amount"]:
            largest = {"name": fin["name"], "asset_amount": fin["asset_amount"]}

    foundation_count = len(orgs)

    # 4 — activity tier.
    if foundation_count >= 3 or total_assets > _ACTIVE_ASSET_FLOOR:
        tier = "ACTIVE"
    elif foundation_count >= 1:
        tier = "MODERATE"
    else:
        tier = "LIMITED"

    result = {
        "foundation_count": foundation_count,
        "total_assets": total_assets,
        "total_assets_formatted": _format_assets(total_assets),
        "largest_foundation": largest if largest["name"] else None,
        "activity_tier": tier,
        "city": city,
        "state": state.upper(),
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "MODERATE",
    }
    log.info(
        "propublica_fetcher: %s, %s — %d foundation(s), assets ~$%s, tier=%s",
        city, state, foundation_count, result["total_assets_formatted"], tier,
    )
    _write_cache(state, community_id, result)
    return result
