"""
pipeline/layer2/ingest/sources/gmail.py

GmailIngester — searches "charter school" email threads via the Gmail REST API,
matches each thread to a known city, and hands it off to SignalExtractor.
"""
from __future__ import annotations

import base64
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

import yaml

from pipeline.layer2.ingest.extractor import SignalExtractor
from pipeline.layer2.storage_interface import SignalStore

logger = logging.getLogger(__name__)

_SOURCES_DIR = os.path.dirname(__file__)
_CONFIG_DIR = os.path.abspath(os.path.join(_SOURCES_DIR, "..", "..", "..", "..", "config"))

_CACHE_PATH = os.path.join(_CONFIG_DIR, "ingest_cache.json")
_STATES_PATH = os.path.join(_CONFIG_DIR, "states.yaml")

_GMAIL_SEARCH_QUERY = "charter school newer_than:30d"

_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

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


def _decode_body_data(data: str) -> str:
    """Decode a base64url-encoded Gmail message body part."""
    padded = data.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_text_from_payload(payload: dict) -> str:
    """Recursively extract plain-text content from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    parts = payload.get("parts")

    if parts:
        texts: list[str] = []
        for part in parts:
            text = _extract_text_from_payload(part)
            if text:
                texts.append(text)
        return "\n".join(texts)

    if mime in ("text/plain", "text/html") or not mime:
        body_data = (payload.get("body") or {}).get("data", "")
        if body_data:
            return _decode_body_data(body_data)

    return ""


def _concat_thread_messages(thread: dict) -> tuple[str, Optional[date]]:
    """Concatenate message bodies in a thread; return (body, earliest_date)."""
    messages = thread.get("messages") or []
    if not messages and "body" in thread:
        return _strip_quoted_replies(thread["body"]), _parse_message_date(thread)

    parts: list[str] = []
    earliest: Optional[date] = None
    for msg in messages:
        # Prefer decoded payload; fall back to snippet
        payload = msg.get("payload")
        if payload:
            body = _extract_text_from_payload(payload)
        else:
            body = msg.get("body") or msg.get("snippet") or msg.get("text") or ""
        if body:
            parts.append(_strip_quoted_replies(body))
        msg_date = _parse_message_date(msg)
        if msg_date and (earliest is None or msg_date < earliest):
            earliest = msg_date
    return "\n\n".join(parts), earliest


def _get_subject_from_thread(thread: dict) -> str:
    """Extract the subject from the first message's headers."""
    messages = thread.get("messages") or []
    if not messages:
        return thread.get("snippet", "")
    headers = (messages[0].get("payload") or {}).get("headers") or []
    for h in headers:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "")
    return thread.get("snippet", "")


def _http_get(url: str, params: dict, access_token: str) -> dict:
    """Perform an authenticated GET, return parsed JSON."""
    full_url = url + "?" + urllib.parse.urlencode(params) if params else url
    req = urllib.request.Request(
        full_url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _exchange_refresh_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a refresh token for a fresh access token; halt on failure."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
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
        error_body = exc.read().decode(errors="replace")
        print(
            f"[gmail] STOP — token exchange failed: HTTP {exc.code}\n{error_body}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"[gmail] STOP — token exchange network error: {exc.reason}", file=sys.stderr)
        raise SystemExit(1)

    access_token = data.get("access_token")
    if not access_token:
        print(
            f"[gmail] STOP — token exchange returned no access_token.\n"
            f"Response: {json.dumps(data, indent=2)}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return access_token


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
        cache_path: str = _CACHE_PATH,
        states_path: str = _STATES_PATH,
        search_query: str = _GMAIL_SEARCH_QUERY,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._days = days
        self._cache_path = cache_path
        self._search_query = search_query
        self._city_name_map = _load_city_name_map(states_path)

        client_id = os.environ.get("GMAIL_CLIENT_ID", "")
        client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
        refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "")

        if not (client_id and client_secret and refresh_token):
            missing = [
                name for name, val in [
                    ("GMAIL_CLIENT_ID", client_id),
                    ("GMAIL_CLIENT_SECRET", client_secret),
                    ("GMAIL_REFRESH_TOKEN", refresh_token),
                ] if not val
            ]
            print(
                f"[gmail] STOP — missing environment variables: {missing}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        self._access_token = _exchange_refresh_token(client_id, client_secret, refresh_token)

    def ingest(self) -> None:
        cache = _load_cache(self._cache_path)
        already_seen: set[str] = set(cache.get("gmail", []))

        # Search for matching threads
        search_resp = _http_get(
            f"{_GMAIL_API_BASE}/threads",
            {"q": self._search_query, "maxResults": 200},
            self._access_token,
        )
        thread_stubs = search_resp.get("threads") or []

        total_fetched = len(thread_stubs)
        city_matched = 0
        signals_written = 0

        for stub in thread_stubs:
            if not isinstance(stub, dict):
                continue

            thread_id = str(stub.get("id") or "")
            if not thread_id:
                continue
            if thread_id in already_seen:
                continue

            # Fetch full thread content
            try:
                thread = _http_get(
                    f"{_GMAIL_API_BASE}/threads/{thread_id}",
                    {"format": "full"},
                    self._access_token,
                )
                if not isinstance(thread, dict):
                    thread = stub
            except urllib.error.HTTPError as exc:
                logger.warning("[gmail] get_thread failed for %s: HTTP %s", thread_id, exc.code)
                thread = stub
            except urllib.error.URLError as exc:
                logger.warning("[gmail] get_thread failed for %s: %s", thread_id, exc.reason)
                thread = stub

            subject = _get_subject_from_thread(thread)
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
