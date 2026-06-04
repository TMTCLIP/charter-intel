"""
pipeline/layer2/ingest/sources/gmail.py

GmailIngester — searches "charter school" email threads via the Gmail MCP
server, matches each thread to a known city, and hands it off to SignalExtractor.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import yaml

from pipeline.layer2.ingest._mcp_client import MCPHTTPClient, MCPError, MCPUnexpectedSchema
from pipeline.layer2.ingest.extractor import SignalExtractor
from pipeline.layer2.storage_interface import SignalStore

logger = logging.getLogger(__name__)

_SOURCES_DIR = os.path.dirname(__file__)
_CONFIG_DIR = os.path.abspath(os.path.join(_SOURCES_DIR, "..", "..", "..", "..", "config"))

_CACHE_PATH = os.path.join(_CONFIG_DIR, "ingest_cache.json")
_STATES_PATH = os.path.join(_CONFIG_DIR, "states.yaml")

_GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
_GMAIL_SEARCH_QUERY = "charter school newer_than:30d"

_SEARCH_TOOL_PATTERNS = ("search_threads", "search_messages", "threads/search", "search")
_GET_THREAD_TOOL_PATTERNS = ("get_thread", "get_thread_content", "threads/get")

# Regex to strip quoted reply chains (lines starting with > or On ... wrote:)
_QUOTE_RE = re.compile(
    r"(^>.*$)|"
    r"(^On .+wrote:$)",
    re.MULTILINE | re.IGNORECASE,
)
_TRAILING_BLANK_RE = re.compile(r"\n{3,}")


def _load_city_name_map(states_path: str = _STATES_PATH) -> dict[str, str]:
    """Return {display_name_lower: city_slug} built from states.yaml district maps."""
    with open(states_path) as f:
        cfg = yaml.safe_load(f) or {}
    result: dict[str, str] = {}
    for state_data in cfg.values():
        if not isinstance(state_data, dict):
            continue
        for slug in (state_data.get("nm_district_map") or {}):
            display = slug.split("-", 1)[1].replace("-", " ") if "-" in slug else slug
            result[display.lower()] = slug
            result[slug.lower()] = slug
    return result


def _match_city(text: str, city_name_map: dict[str, str]) -> Optional[str]:
    text_lower = text.lower()
    for name, slug in sorted(city_name_map.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
            return slug
    return None


def _strip_quoted_replies(text: str) -> str:
    """Remove standard email reply chains from a thread body."""
    cleaned = _QUOTE_RE.sub("", text)
    cleaned = _TRAILING_BLANK_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def _load_cache(path: str = _CACHE_PATH) -> dict:
    if not os.path.exists(path):
        return {"granola": [], "gmail": []}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"granola": [], "gmail": []}
        data.setdefault("granola", [])
        data.setdefault("gmail", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"granola": [], "gmail": []}


def _save_cache(cache: dict, path: str = _CACHE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


def _find_tool(tools: list[dict], patterns: tuple) -> Optional[str]:
    names = {t.get("name", "") for t in tools}
    for pattern in patterns:
        if pattern in names:
            return pattern
    return None


def _parse_message_date(msg: dict) -> Optional[date]:
    for field in ("date", "timestamp", "internalDate", "received_at", "sent_at"):
        val = msg.get(field)
        if not val:
            continue
        if isinstance(val, (date, datetime)):
            return val.date() if isinstance(val, datetime) else val
        # Gmail internalDate is epoch-ms as a string
        try:
            epoch_ms = int(val)
            return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).date()
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return None


def _concat_thread_messages(thread: dict) -> tuple[str, Optional[date]]:
    """Concatenate message bodies in a thread; return (body, earliest_date)."""
    messages = thread.get("messages") or []
    if not messages and "body" in thread:
        return _strip_quoted_replies(thread["body"]), _parse_message_date(thread)

    parts: list[str] = []
    earliest: Optional[date] = None
    for msg in messages:
        body = msg.get("body") or msg.get("snippet") or msg.get("text") or ""
        if body:
            parts.append(_strip_quoted_replies(body))
        msg_date = _parse_message_date(msg)
        if msg_date and (earliest is None or msg_date < earliest):
            earliest = msg_date
    return "\n\n".join(parts), earliest


class GmailIngester:
    """Searches Gmail for charter-school threads and writes soft signals.

    Parameters
    ----------
    store:
        SignalStore instance used to persist source and signal records.
    extractor:
        SignalExtractor instance.
    days:
        Look-back window in days (default 30; also embedded in the query).
    mcp_url:
        Gmail MCP URL (default: https://gmailmcp.googleapis.com/mcp/v1).
    cache_path:
        Path to the local JSON dedup cache file.
    search_query:
        Gmail search query (default: "charter school newer_than:30d").
    """

    def __init__(
        self,
        store: SignalStore,
        extractor: SignalExtractor,
        days: int = 30,
        mcp_url: str = _GMAIL_MCP_URL,
        cache_path: str = _CACHE_PATH,
        states_path: str = _STATES_PATH,
        search_query: str = _GMAIL_SEARCH_QUERY,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._days = days
        self._mcp_url = mcp_url
        self._cache_path = cache_path
        self._search_query = search_query
        self._city_name_map = _load_city_name_map(states_path)

    def ingest(self) -> None:
        cache = _load_cache(self._cache_path)
        already_seen: set[str] = set(cache.get("gmail", []))

        # Gmail MCP connector is pre-authenticated via the Claude workspace
        # integration — no auth header required.
        mcp = MCPHTTPClient(url=self._mcp_url, headers={})
        mcp.initialize()

        tools = mcp.list_tools()
        search_tool = _find_tool(tools, _SEARCH_TOOL_PATTERNS)
        if search_tool is None:
            print(
                f"[gmail] STOP — could not find a thread-search tool. "
                f"Available tools: {[t.get('name') for t in tools]}\n"
                f"Raw tools response: {json.dumps(tools, indent=2)}",
                file=sys.stderr,
            )
            raise MCPUnexpectedSchema(
                f"No thread-search tool found in Gmail MCP. Tools: "
                f"{[t.get('name') for t in tools]}"
            )

        raw_result = mcp.call_tool(search_tool, {
            "query": self._search_query,
            "maxResults": 200,
        })

        # Validate schema
        if isinstance(raw_result, list):
            threads = raw_result
        elif isinstance(raw_result, dict):
            for key in ("threads", "messages", "items", "data", "results"):
                if isinstance(raw_result.get(key), list):
                    threads = raw_result[key]
                    break
            else:
                print(
                    f"[gmail] STOP — unexpected search response schema.\n"
                    f"Raw result: {json.dumps(raw_result, indent=2)[:2000]}",
                    file=sys.stderr,
                )
                raise MCPUnexpectedSchema(
                    f"search returned unexpected schema: {list(raw_result.keys())}"
                )
        else:
            print(
                f"[gmail] STOP — search returned non-list/dict: {type(raw_result)}",
                file=sys.stderr,
            )
            raise MCPUnexpectedSchema(f"search returned {type(raw_result)}")

        # Optionally fetch full thread content if the search returned stubs
        get_tool = _find_tool(tools, _GET_THREAD_TOOL_PATTERNS)

        total_fetched = len(threads)
        city_matched = 0
        signals_written = 0

        for thread_stub in threads:
            if not isinstance(thread_stub, dict):
                continue

            thread_id = str(
                thread_stub.get("id") or
                thread_stub.get("thread_id") or
                thread_stub.get("threadId") or
                ""
            )
            if not thread_id:
                continue
            if thread_id in already_seen:
                continue

            # Fetch full thread if we have the tool and only received a stub
            if get_tool and not thread_stub.get("messages"):
                try:
                    thread = mcp.call_tool(get_tool, {"id": thread_id})
                    if not isinstance(thread, dict):
                        thread = thread_stub
                except MCPError as exc:
                    logger.warning("[gmail] get_thread failed for %s: %s", thread_id, exc)
                    thread = thread_stub
            else:
                thread = thread_stub

            subject = (
                thread.get("subject") or
                thread.get("snippet") or
                ""
            )
            body, thread_date = _concat_thread_messages(thread)
            search_text = f"{subject}\n{body}"

            city_slug = _match_city(search_text, self._city_name_map)
            if city_slug is None:
                already_seen.add(thread_id)
                continue

            city_matched += 1
            effective_date = thread_date or date.today()

            source_id = self._store.write_source({
                "source_type": "gmail",
                "external_id": thread_id,
                "title": subject or f"Gmail thread {thread_id}",
                "source_date": effective_date,
            })

            metadata = {
                "source_type": "gmail",
                "city_slug": city_slug,
                "source_date": effective_date,
                "source_id": source_id,
            }

            content = body.strip() or subject
            written = self._extractor.extract(content, metadata)
            signals_written += len(written)
            already_seen.add(thread_id)

        cache["gmail"] = list(already_seen)
        _save_cache(cache, self._cache_path)

        print(
            f"[gmail] {total_fetched} threads fetched, "
            f"{city_matched} matched to cities, "
            f"{signals_written} signals written"
        )
