"""
pipeline/layer2/ingest/sources/granola.py

GranolaIngester — fetches meeting notes from the Granola MCP server,
matches each note to a known city, and hands it off to SignalExtractor.
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

_GRANOLA_MCP_URL = "https://mcp.granola.ai/mcp"

# Expected tool name patterns (checked in order; first match wins).
_NOTE_LIST_TOOL_PATTERNS = ("list_notes", "get_notes", "notes/list")
_NOTE_GET_TOOL_PATTERNS = ("get_note", "get_note_content", "notes/get")


def _load_city_name_map(states_path: str = _STATES_PATH) -> dict[str, str]:
    """Return {display_name_lower: city_slug} built from states.yaml district maps."""
    with open(states_path) as f:
        cfg = yaml.safe_load(f) or {}
    result: dict[str, str] = {}
    for state_data in cfg.values():
        if not isinstance(state_data, dict):
            continue
        for slug in (state_data.get("nm_district_map") or {}):
            # nm-santa-fe → "santa fe"
            display = slug.split("-", 1)[1].replace("-", " ") if "-" in slug else slug
            result[display.lower()] = slug
            # Also map the slug itself for exact-match fallback
            result[slug.lower()] = slug
    return result


def _match_city(text: str, city_name_map: dict[str, str]) -> Optional[str]:
    """Return the first city_slug whose display name appears in *text*."""
    text_lower = text.lower()
    # Sort by length descending so longer/more specific names match first.
    for name, slug in sorted(city_name_map.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
            return slug
    return None


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


def _parse_note_date(note: dict) -> Optional[date]:
    for field in ("created_at", "date", "meeting_date", "start_time", "timestamp"):
        val = note.get(field)
        if not val:
            continue
        if isinstance(val, (date, datetime)):
            return val.date() if isinstance(val, datetime) else val
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return dt.date()
        except ValueError:
            pass
    return None


def _extract_note_text(note: dict) -> str:
    for field in ("content", "body", "text", "markdown", "notes"):
        val = note.get(field)
        if val and isinstance(val, str):
            return val
    # Fallback: title only
    return note.get("title", "")


class GranolaIngester:
    """Fetches recent Granola meeting notes and writes soft signals via SignalExtractor.

    Parameters
    ----------
    store:
        SignalStore instance used to persist source and signal records.
    extractor:
        SignalExtractor instance.
    days:
        Look-back window in days (default 30).
    granola_api_key:
        Granola API key. Falls back to GRANOLA_API_KEY env var if not provided.
    mcp_url:
        Granola MCP URL (default: https://mcp.granola.ai/mcp).
    cache_path:
        Path to the local JSON dedup cache file.
    """

    def __init__(
        self,
        store: SignalStore,
        extractor: SignalExtractor,
        days: int = 30,
        granola_api_key: Optional[str] = None,
        mcp_url: str = _GRANOLA_MCP_URL,
        cache_path: str = _CACHE_PATH,
        states_path: str = _STATES_PATH,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._days = days
        self._api_key = granola_api_key or os.environ.get("GRANOLA_API_KEY", "")
        self._mcp_url = mcp_url
        self._cache_path = cache_path
        self._city_name_map = _load_city_name_map(states_path)

    def ingest(self) -> None:
        cache = _load_cache(self._cache_path)
        already_seen: set[str] = set(cache.get("granola", []))

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        mcp = MCPHTTPClient(url=self._mcp_url, headers=headers)
        mcp.initialize()

        tools = mcp.list_tools()
        list_tool = _find_tool(tools, _NOTE_LIST_TOOL_PATTERNS)
        if list_tool is None:
            print(
                f"[granola] STOP — could not find a note-list tool. "
                f"Available tools: {[t.get('name') for t in tools]}\n"
                f"Raw tools response: {json.dumps(tools, indent=2)}",
                file=sys.stderr,
            )
            raise MCPUnexpectedSchema(
                f"No note-list tool found in Granola MCP. Tools: "
                f"{[t.get('name') for t in tools]}"
            )

        since = (datetime.now(timezone.utc) - timedelta(days=self._days)).isoformat()
        raw_result = mcp.call_tool(list_tool, {"since": since, "limit": 200})

        # Validate schema — must be list or contain a list key
        if isinstance(raw_result, list):
            notes = raw_result
        elif isinstance(raw_result, dict):
            # Common wrappers: {"notes": [...]} or {"items": [...]} or {"data": [...]}
            for key in ("notes", "items", "data", "results", "content"):
                if isinstance(raw_result.get(key), list):
                    notes = raw_result[key]
                    break
            else:
                print(
                    f"[granola] STOP — unexpected list_notes response schema.\n"
                    f"Raw result: {json.dumps(raw_result, indent=2)[:2000]}",
                    file=sys.stderr,
                )
                raise MCPUnexpectedSchema(
                    f"list_notes returned unexpected schema: {list(raw_result.keys())}"
                )
        else:
            print(
                f"[granola] STOP — list_notes returned non-list/dict: {type(raw_result)}",
                file=sys.stderr,
            )
            raise MCPUnexpectedSchema(f"list_notes returned {type(raw_result)}")

        total_fetched = len(notes)
        city_matched = 0
        signals_written = 0

        for note in notes:
            if not isinstance(note, dict):
                continue

            note_id = str(note.get("id") or note.get("note_id") or "")
            if not note_id:
                continue
            if note_id in already_seen:
                continue

            title = note.get("title", "")
            body = _extract_note_text(note)
            search_text = f"{title}\n{body}"

            city_slug = _match_city(search_text, self._city_name_map)
            if city_slug is None:
                already_seen.add(note_id)
                continue

            city_matched += 1
            note_date = _parse_note_date(note) or date.today()

            # Write source record for dedup
            source_id = self._store.write_source({
                "source_type": "granola",
                "external_id": note_id,
                "title": title or f"Granola note {note_id}",
                "source_date": note_date,
            })

            metadata = {
                "source_type": "granola",
                "city_slug": city_slug,
                "source_date": note_date,
                "source_id": source_id,
            }

            written = self._extractor.extract(body or title, metadata)
            signals_written += len(written)
            already_seen.add(note_id)

        cache["granola"] = list(already_seen)
        _save_cache(cache, self._cache_path)

        print(
            f"[granola] {total_fetched} notes fetched, "
            f"{city_matched} matched to cities, "
            f"{signals_written} signals written"
        )
