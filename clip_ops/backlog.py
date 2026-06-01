"""
clip_ops/backlog.py — strictly deterministic backlog detection and ranking.

priority_score = blocking_score*3 + frequency_score*2 + recency_score*1

  blocking_score: HIGH severity keyword -> 3; MED severity keyword -> 2; else 0
  frequency_score: occurrences of the item's signature term across the corpus,
                   capped at FREQUENCY_CAP
  recency_score:  source modified today -> 2; within 1..3 days -> 1; older -> 0

No randomness, no network, no LLM. Tie-breaks are alphabetical for stability.
"""
from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional, Tuple

from clip_ops import config

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "not", "yet",
    "are", "was", "but", "has", "have", "all", "any", "via", "per", "use",
    "add", "fix", "new", "old", "out", "off", "set", "get", "run", "now",
    "when", "then", "than", "next", "step", "item", "todo", "done", "will",
    "still", "must", "should", "needs", "need", "implement", "update",
}


def _normalize(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def blocking_score(title: str) -> Tuple[int, Optional[str]]:
    t = title.lower()
    for kw in config.BLOCKING_KEYWORDS_HIGH:
        if kw in t:
            return 3, kw
    for kw in config.BLOCKING_KEYWORDS_MED:
        if kw in t:
            return 2, kw
    return 0, None


def signature_term(title: str) -> Optional[str]:
    """Longest non-stopword alphabetic token (>=4 chars) — deterministic."""
    tokens = re.findall(r"[a-z]{4,}", title.lower())
    candidates = [t for t in tokens if t not in _STOPWORDS]
    if not candidates:
        candidates = tokens
    if not candidates:
        return None
    # Longest first, then alphabetical — fully deterministic.
    candidates.sort(key=lambda w: (-len(w), w))
    return candidates[0]


def frequency_score(title: str, corpus: str) -> Tuple[int, str]:
    term = signature_term(title)
    if not term:
        return 0, "no signature term"
    count = corpus.count(term)
    capped = min(count, config.FREQUENCY_CAP)
    return capped, "term '%s' x%d (cap %d)" % (term, count, config.FREQUENCY_CAP)


def recency_score(source_date: Optional[str], ref: datetime.date) -> Tuple[int, str]:
    if not source_date:
        return 0, "no source date"
    try:
        d = datetime.date.fromisoformat(source_date)
    except (TypeError, ValueError):
        return 0, "unparsable source date"
    delta = (ref - d).days
    if delta <= 0:
        return config.RECENCY_TODAY_SCORE, "modified today"
    if 1 <= delta <= config.RECENCY_RECENT_MAX_DAYS:
        return config.RECENCY_RECENT_SCORE, "modified %d day(s) ago" % delta
    return 0, "modified %d day(s) ago" % delta


def categorize(title: str) -> str:
    t = title.lower()
    for category, keywords in config.CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return category
    return "feature"


def rank_backlog(
    candidates: List[Dict[str, Optional[str]]],
    corpus: str,
    ref: Optional[datetime.date] = None,
) -> List[dict]:
    """
    candidates: list of {"title": str, "source_date": isoformat|None}.
    Deduplicates by normalized title (first occurrence wins), scores each,
    and returns the list sorted by priority_score desc, then title asc.
    """
    ref = ref or config.today()
    seen: set = set()
    ranked: List[dict] = []

    for cand in candidates:
        title = _clean_title(cand.get("title", ""))
        if not title:
            continue
        key = _normalize(title)
        if key in seen:
            continue
        seen.add(key)

        b_score, b_kw = blocking_score(title)
        f_score, f_reason = frequency_score(title, corpus)
        r_score, r_reason = recency_score(cand.get("source_date"), ref)

        priority = (b_score * config.WEIGHT_BLOCKING
                    + f_score * config.WEIGHT_FREQUENCY
                    + r_score * config.WEIGHT_RECENCY)

        blocking_reason = ("severity '%s' -> %d" % (b_kw, b_score)) if b_kw \
            else "no severity keyword -> 0"
        rationale = (
            "priority=%d = blocking(%d)x%d + frequency(%d)x%d + recency(%d)x%d; "
            "%s; %s; %s"
            % (priority,
               b_score, config.WEIGHT_BLOCKING,
               f_score, config.WEIGHT_FREQUENCY,
               r_score, config.WEIGHT_RECENCY,
               blocking_reason, f_reason, r_reason)
        )

        ranked.append({
            "title": title,
            "category": categorize(title),
            "priority_score": priority,
            "rationale": rationale,
            "components": {
                "blocking_score": b_score,
                "frequency_score": f_score,
                "recency_score": r_score,
            },
        })

    ranked.sort(key=lambda x: (-x["priority_score"], x["title"]))
    return ranked


def _clean_title(title: str, max_len: int = 160) -> str:
    title = re.sub(r"\s+", " ", (title or "").strip())
    if len(title) > max_len:
        title = title[:max_len - 1].rstrip() + "…"
    return title
