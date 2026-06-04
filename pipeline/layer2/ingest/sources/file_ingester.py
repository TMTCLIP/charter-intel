"""
pipeline/layer2/ingest/sources/file_ingester.py

FileIngester — reads .txt and .md files from a path (file or directory),
matches each to a known city via word-boundary regex, and passes cleaned
content to SignalExtractor for soft-signal extraction.

Serves as a manual ingestion bridge for Gmail/Granola exports while
programmatic MCP auth is being resolved.

Only .txt and .md extensions are supported; all others emit a warning
and are skipped. Files with no city match are warned about and skipped
(non-fatal). Already-processed absolute paths are deduped via
config/ingest_cache.json under the "file" key.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
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

_SUPPORTED_EXTENSIONS = frozenset({".txt", ".md"})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _detect_source_type(filename: str) -> str:
    """Infer source_type from the filename stem.

    Rules (case-insensitive, checked against the stem only):
      "gmail" or "email"     → "gmail"
      "granola" or "meeting" → "granola"
      otherwise              → "manual"
    """
    stem = os.path.splitext(filename)[0].lower()
    if "gmail" in stem or "email" in stem:
        return "gmail"
    if "granola" in stem or "meeting" in stem:
        return "granola"
    return "manual"


def _load_city_name_map(states_path: str = _STATES_PATH) -> dict[str, str]:
    """Return {display_name_lower: city_slug} built from states.yaml district maps.

    Mirrors the identical function in granola.py and gmail.py exactly.
    """
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
    """Return the first city_slug whose display name appears in *text*.

    Sorts by name length descending so longer/more-specific names win.
    Mirrors the identical function in granola.py and gmail.py exactly.
    """
    text_lower = text.lower()
    for name, slug in sorted(city_name_map.items(), key=lambda kv: -len(kv[0])):
        if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
            return slug
    return None


def _load_cache(path: str = _CACHE_PATH) -> dict:
    if not os.path.exists(path):
        return {"granola": [], "gmail": [], "file": []}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"granola": [], "gmail": [], "file": []}
        data.setdefault("granola", [])
        data.setdefault("gmail", [])
        data.setdefault("file", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"granola": [], "gmail": [], "file": []}


def _save_cache(cache: dict, path: str = _CACHE_PATH) -> None:
    try:
        with open(path, "w") as f:
            json.dump(cache, f, indent=2)
    except OSError as exc:
        print(
            f"[file] STOP — ingest_cache.json could not be written: {exc}",
            file=sys.stderr,
        )
        raise


# ── FileIngester ───────────────────────────────────────────────────────────────

class FileIngester:
    """Reads .txt/.md files and ingests them through SignalExtractor into Notion.

    Parameters
    ----------
    store:
        SignalStore instance used to persist source and signal records.
    extractor:
        SignalExtractor instance.
    cache_path:
        Path to the JSON dedup cache file (default: config/ingest_cache.json).
        Cache keys are absolute file paths stored under the "file" key.
    states_path:
        Path to states.yaml for city name resolution.
    """

    def __init__(
        self,
        store: SignalStore,
        extractor: SignalExtractor,
        cache_path: str = _CACHE_PATH,
        states_path: str = _STATES_PATH,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._cache_path = cache_path
        self._city_name_map = _load_city_name_map(states_path)

    def ingest(self, path: str) -> None:
        """Ingest all supported files found at *path* (file or directory).

        For directories: processes only files at the top level (non-recursive).
        Supported extensions: .txt, .md
        Unsupported extensions → warning printed, file skipped (non-fatal).
        No city match → warning printed, file skipped and cached (non-fatal).
        Already-cached absolute paths → silently skipped.
        """
        abs_path = os.path.abspath(path)

        # ── Collect candidates ─────────────────────────────────────────────
        if os.path.isdir(abs_path):
            files_to_process: list[str] = []
            for entry in sorted(os.listdir(abs_path)):
                full = os.path.join(abs_path, entry)
                if not os.path.isfile(full):
                    continue
                ext = os.path.splitext(entry)[1].lower()
                if ext not in _SUPPORTED_EXTENSIONS:
                    print(
                        f"[file] WARNING — unsupported file extension "
                        f"{ext!r} for {entry}, skipping"
                    )
                    continue
                files_to_process.append(full)

        elif os.path.isfile(abs_path):
            ext = os.path.splitext(abs_path)[1].lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                print(
                    f"[file] WARNING — unsupported file extension "
                    f"{ext!r} for {os.path.basename(abs_path)}, skipping"
                )
                files_to_process = []
            else:
                files_to_process = [abs_path]

        else:
            print(f"[file] STOP — path not found: {abs_path}", file=sys.stderr)
            sys.exit(1)

        # ── Process ────────────────────────────────────────────────────────
        cache = _load_cache(self._cache_path)
        already_seen: set[str] = set(cache.get("file", []))

        total_found = len(files_to_process)
        city_matched = 0
        signals_written = 0

        for file_path in files_to_process:
            filename = os.path.basename(file_path)

            if file_path in already_seen:
                print(f"[file] SKIP (cached) — {filename}")
                continue

            try:
                with open(file_path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as exc:
                print(f"[file] WARNING — could not read {filename}: {exc}")
                continue

            city_slug = _match_city(content, self._city_name_map)
            if city_slug is None:
                print(f"[file] WARNING — no city match in {filename}, skipping")
                already_seen.add(file_path)
                continue

            city_matched += 1
            source_type = _detect_source_type(filename)

            # Use file mtime as source_date; fall back to today if unavailable.
            try:
                mtime = os.path.getmtime(file_path)
                source_date: date = datetime.fromtimestamp(mtime, tz=timezone.utc).date()
            except OSError:
                source_date = date.today()

            source_id = self._store.write_source({
                "source_type": source_type,
                "external_id": file_path,
                "title": filename,
                "source_date": source_date,
            })

            metadata = {
                "source_type": source_type,
                "city_slug": city_slug,
                "source_date": source_date,
                "source_id": source_id,
            }

            written = self._extractor.extract(content, metadata)
            signals_written += len(written)
            already_seen.add(file_path)

            print(
                f"[file] {filename} — city: {city_slug}, "
                f"source_type: {source_type}, signals: {len(written)}"
            )

        cache["file"] = list(already_seen)
        _save_cache(cache, self._cache_path)

        print(
            f"[file] {total_found} files found, "
            f"{city_matched} matched to cities, "
            f"{signals_written} signals written"
        )
