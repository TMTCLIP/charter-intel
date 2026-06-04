"""
tests/unit/test_file_ingester.py

Unit tests for FileIngester.
All network calls (Notion, Anthropic) are mocked — no I/O beyond tmp_path.
Uses tmp_path pytest fixture for ephemeral file and cache creation.

Coverage matrix
───────────────
  Single file  : city match found → signals written
  Single file  : no city match → warning, no Notion write
  Single file  : unsupported extension → warning, no Notion write
  Single file  : already cached → silently skipped
  Directory    : multiple files, mixed city matches
  Directory    : unsupported extension files warned, others processed
  source_type  : gmail / granola / manual detection
  CLI guard    : --ingest file without --ingest-path → sys.exit(1)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.ingest.sources.file_ingester import (
    FileIngester,
    _detect_source_type,
)

pytestmark = pytest.mark.unit

# ── Shared test data ───────────────────────────────────────────────────────────

# Minimal city map used for all FileIngester tests.
# Mirrors the format produced by the real _load_city_name_map.
_TEST_CITY_MAP = {
    "albuquerque": "nm-albuquerque",
    "nm-albuquerque": "nm-albuquerque",
    "santa fe": "nm-santa-fe",
    "nm-santa-fe": "nm-santa-fe",
}

# File content that contains a known city name.
_CITY_CONTENT = (
    "Charter school board meeting in Albuquerque, NM.\n"
    "Discussed expansion plans for two new schools."
)

# File content with no city mention.
_NO_CITY_CONTENT = (
    "National charter school trends for the upcoming fiscal year.\n"
    "No specific city or district is discussed here."
)

_FAKE_SOURCE_ID = "notion-source-id-001"
_FAKE_SIGNAL = {"signal_id": "notion-signal-id-001", "city": "nm-albuquerque"}


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _make_ingester(tmp_path, *, city_map=None, store=None, extractor=None):
    """Return a FileIngester wired to mocks, with cache in tmp_path.

    If *extractor* is provided, its return_value is NOT overridden so callers
    can preset custom signal lists. If omitted a default mock returning [] is used.
    """
    store = store or MagicMock()
    store.write_source.return_value = _FAKE_SOURCE_ID

    if extractor is None:
        extractor = MagicMock()
        extractor.extract.return_value = []

    cache_file = tmp_path / "cache.json"

    with patch(
        "pipeline.layer2.ingest.sources.file_ingester._load_city_name_map",
        return_value=city_map if city_map is not None else _TEST_CITY_MAP,
    ):
        ingester = FileIngester(
            store=store,
            extractor=extractor,
            cache_path=str(cache_file),
        )
    return ingester, store, extractor, cache_file


# ══════════════════════════════════════════════════════════════════════════════
# source_type detection
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectSourceType:
    def test_gmail_keyword_in_stem(self):
        assert _detect_source_type("gmail_export_2026.txt") == "gmail"

    def test_email_keyword_in_stem(self):
        assert _detect_source_type("email_thread_may.md") == "gmail"

    def test_granola_keyword_in_stem(self):
        assert _detect_source_type("granola_notes.txt") == "granola"

    def test_meeting_keyword_in_stem(self):
        assert _detect_source_type("meeting_2026_06_04.md") == "granola"

    def test_no_keyword_returns_manual(self):
        assert _detect_source_type("quarterly_report.txt") == "manual"
        assert _detect_source_type("notes.md") == "manual"

    def test_case_insensitive(self):
        assert _detect_source_type("GMAIL_Export.txt") == "gmail"
        assert _detect_source_type("Granola_Session.md") == "granola"

    def test_extension_excluded_from_check(self):
        # _detect_source_type checks only the stem, not the extension.
        # "report.gmail" stem is "report" — no keyword → manual.
        assert _detect_source_type("report.gmail") == "manual"
        # A file like "data.txt" has no keywords in stem → manual
        assert _detect_source_type("data.txt") == "manual"

    def test_email_beats_no_match(self):
        assert _detect_source_type("email_notes_albuquerque.txt") == "gmail"

    def test_meeting_beats_no_match(self):
        assert _detect_source_type("meeting_albuquerque_charter.md") == "granola"


# ══════════════════════════════════════════════════════════════════════════════
# Single file ingestion
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleFileIngestion:
    def test_city_match_writes_source_and_calls_extract(self, tmp_path):
        """A file whose content contains a known city → source written + extract called."""
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        extractor_mock = MagicMock()
        extractor_mock.extract.return_value = [_FAKE_SIGNAL]
        ingester, store, extractor, cache_file = _make_ingester(
            tmp_path, extractor=extractor_mock
        )

        ingester.ingest(str(f))

        store.write_source.assert_called_once()
        source_call = store.write_source.call_args[0][0]
        assert source_call["source_type"] == "manual"
        assert source_call["external_id"] == str(f.resolve())
        assert source_call["title"] == "notes.txt"

        extractor.extract.assert_called_once()
        metadata = extractor.extract.call_args[0][1]
        assert metadata["source_type"] == "manual"
        assert metadata["city_slug"] == "nm-albuquerque"
        assert metadata["source_id"] == _FAKE_SOURCE_ID

    def test_city_match_signals_counted_in_summary(self, tmp_path, capsys):
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        extractor_mock = MagicMock()
        extractor_mock.extract.return_value = [_FAKE_SIGNAL, _FAKE_SIGNAL]
        ingester, store, _, _ = _make_ingester(tmp_path, extractor=extractor_mock)

        ingester.ingest(str(f))

        out = capsys.readouterr().out
        assert "1 files found" in out
        assert "1 matched to cities" in out
        assert "2 signals written" in out

    def test_no_city_match_skips_without_notion_write(self, tmp_path, capsys):
        """Content with no city name → no write_source, no extract, warning printed."""
        f = tmp_path / "random.md"
        f.write_text(_NO_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        store.write_source.assert_not_called()
        extractor.extract.assert_not_called()

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "no city match" in out
        assert "0 matched to cities" in out

    def test_unsupported_extension_skipped_with_warning(self, tmp_path, capsys):
        """A .pdf file → warning printed, no processing."""
        f = tmp_path / "notes.pdf"
        f.write_text(_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        store.write_source.assert_not_called()
        extractor.extract.assert_not_called()

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "unsupported file extension" in out
        assert ".pdf" in out

    def test_unsupported_docx_extension_skipped(self, tmp_path, capsys):
        f = tmp_path / "notes.docx"
        f.write_text(_CITY_CONTENT)
        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        store.write_source.assert_not_called()
        extractor.extract.assert_not_called()
        out = capsys.readouterr().out
        assert ".docx" in out

    def test_already_cached_file_silently_skipped(self, tmp_path, capsys):
        """A file path already in cache → silently skipped; no write_source."""
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        cache_file = tmp_path / "cache.json"
        cache_file.write_text(
            json.dumps({"granola": [], "gmail": [], "file": [str(f.resolve())]})
        )

        with patch(
            "pipeline.layer2.ingest.sources.file_ingester._load_city_name_map",
            return_value=_TEST_CITY_MAP,
        ):
            store = MagicMock()
            extractor = MagicMock()
            ingester = FileIngester(
                store=store,
                extractor=extractor,
                cache_path=str(cache_file),
            )

        ingester.ingest(str(f))

        store.write_source.assert_not_called()
        extractor.extract.assert_not_called()

        out = capsys.readouterr().out
        assert "SKIP (cached)" in out

    def test_processed_file_written_to_cache(self, tmp_path):
        """After successful ingestion the absolute path appears in cache["file"]."""
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        ingester, store, _, cache_file = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        cache = json.loads(cache_file.read_text())
        assert str(f.resolve()) in cache["file"]

    def test_no_city_file_also_cached_to_avoid_reprocessing(self, tmp_path):
        """Files with no city match are added to the cache to skip next run."""
        f = tmp_path / "random.txt"
        f.write_text(_NO_CITY_CONTENT)

        ingester, store, _, cache_file = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        cache = json.loads(cache_file.read_text())
        assert str(f.resolve()) in cache["file"]

    def test_source_type_gmail_detected(self, tmp_path):
        f = tmp_path / "gmail_thread_june.txt"
        f.write_text(_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        metadata = extractor.extract.call_args[0][1]
        assert metadata["source_type"] == "gmail"

    def test_source_type_granola_detected(self, tmp_path):
        f = tmp_path / "granola_notes.md"
        f.write_text(_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        metadata = extractor.extract.call_args[0][1]
        assert metadata["source_type"] == "granola"

    def test_source_type_manual_fallback(self, tmp_path):
        f = tmp_path / "board_update.txt"
        f.write_text(_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(f))

        metadata = extractor.extract.call_args[0][1]
        assert metadata["source_type"] == "manual"


# ══════════════════════════════════════════════════════════════════════════════
# Directory ingestion
# ══════════════════════════════════════════════════════════════════════════════

class TestDirectoryIngestion:
    def test_directory_multiple_files_mixed_matches(self, tmp_path, capsys):
        """Directory with 2 files: one matches a city, one doesn't."""
        matched = tmp_path / "albuquerque_notes.txt"
        matched.write_text(_CITY_CONTENT)
        unmatched = tmp_path / "generic_notes.md"
        unmatched.write_text(_NO_CITY_CONTENT)

        extractor_mock = MagicMock()
        extractor_mock.extract.return_value = [_FAKE_SIGNAL]
        ingester, store, extractor, cache_file = _make_ingester(
            tmp_path, extractor=extractor_mock
        )

        ingester.ingest(str(tmp_path))

        assert store.write_source.call_count == 1
        assert extractor.extract.call_count == 1

        out = capsys.readouterr().out
        assert "2 files found" in out
        assert "1 matched to cities" in out
        assert "1 signals written" in out

    def test_directory_both_files_match(self, tmp_path, capsys):
        """Directory where both files match different cities."""
        f1 = tmp_path / "notes1.txt"
        f1.write_text(_CITY_CONTENT)  # albuquerque
        f2 = tmp_path / "notes2.md"
        f2.write_text("Charter school expansion in Santa Fe is proceeding well.")

        extractor_mock = MagicMock()
        extractor_mock.extract.return_value = [_FAKE_SIGNAL]
        ingester, store, extractor, _ = _make_ingester(
            tmp_path, extractor=extractor_mock
        )

        ingester.ingest(str(tmp_path))

        assert store.write_source.call_count == 2
        assert extractor.extract.call_count == 2

        out = capsys.readouterr().out
        assert "2 files found" in out
        assert "2 matched to cities" in out

    def test_directory_unsupported_extension_warned_supported_processed(
        self, tmp_path, capsys
    ):
        """Directory with .txt (valid) and .pdf (invalid): .pdf warned, .txt processed."""
        valid = tmp_path / "notes.txt"
        valid.write_text(_CITY_CONTENT)
        invalid = tmp_path / "report.pdf"
        invalid.write_text(_CITY_CONTENT)

        extractor_mock = MagicMock()
        extractor_mock.extract.return_value = [_FAKE_SIGNAL]
        ingester, store, extractor, _ = _make_ingester(
            tmp_path, extractor=extractor_mock
        )

        ingester.ingest(str(tmp_path))

        # Only .txt is counted and processed
        assert store.write_source.call_count == 1
        assert extractor.extract.call_count == 1

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert ".pdf" in out
        assert "1 files found" in out  # only the .txt counts

    def test_directory_all_files_cached_are_skipped(self, tmp_path, capsys):
        """All files already in cache → nothing processed."""
        f1 = tmp_path / "notes.txt"
        f1.write_text(_CITY_CONTENT)
        f2 = tmp_path / "more.md"
        f2.write_text(_CITY_CONTENT)

        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "granola": [],
            "gmail": [],
            "file": [str(f1.resolve()), str(f2.resolve())],
        }))

        with patch(
            "pipeline.layer2.ingest.sources.file_ingester._load_city_name_map",
            return_value=_TEST_CITY_MAP,
        ):
            store = MagicMock()
            extractor = MagicMock()
            ingester = FileIngester(
                store=store,
                extractor=extractor,
                cache_path=str(cache_file),
            )

        ingester.ingest(str(tmp_path))

        store.write_source.assert_not_called()
        extractor.extract.assert_not_called()

        out = capsys.readouterr().out
        assert "SKIP (cached)" in out

    def test_directory_empty_no_error(self, tmp_path, capsys):
        """An empty directory produces a 0-file summary without errors."""
        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(tmp_path))

        store.write_source.assert_not_called()
        out = capsys.readouterr().out
        assert "0 files found" in out

    def test_directory_nonrecursive(self, tmp_path):
        """Files in sub-directories are NOT processed (non-recursive)."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        nested = sub / "notes.txt"
        nested.write_text(_CITY_CONTENT)
        # Top-level file that does NOT match a city
        top = tmp_path / "top.txt"
        top.write_text(_NO_CITY_CONTENT)

        ingester, store, extractor, _ = _make_ingester(tmp_path)

        ingester.ingest(str(tmp_path))

        # Only the top-level .txt is found; nested is ignored
        store.write_source.assert_not_called()  # top-level file has no city match


# ══════════════════════════════════════════════════════════════════════════════
# Cache persistence
# ══════════════════════════════════════════════════════════════════════════════

class TestCachePersistence:
    def test_cache_preserves_existing_granola_and_gmail_keys(self, tmp_path):
        """Existing "granola" and "gmail" cache entries survive a file ingest run."""
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "granola": ["granola-note-abc"],
            "gmail": ["gmail-thread-xyz"],
            "file": [],
        }))

        with patch(
            "pipeline.layer2.ingest.sources.file_ingester._load_city_name_map",
            return_value=_TEST_CITY_MAP,
        ):
            store = MagicMock()
            store.write_source.return_value = _FAKE_SOURCE_ID
            extractor = MagicMock()
            extractor.extract.return_value = []
            ingester = FileIngester(
                store=store,
                extractor=extractor,
                cache_path=str(cache_file),
            )

        ingester.ingest(str(f))

        cache = json.loads(cache_file.read_text())
        assert "granola-note-abc" in cache["granola"]
        assert "gmail-thread-xyz" in cache["gmail"]
        assert str(f.resolve()) in cache["file"]

    def test_cache_created_when_missing(self, tmp_path):
        """ingest_cache.json is created from scratch if it doesn't exist."""
        f = tmp_path / "notes.txt"
        f.write_text(_CITY_CONTENT)

        cache_file = tmp_path / "new_cache.json"
        assert not cache_file.exists()

        with patch(
            "pipeline.layer2.ingest.sources.file_ingester._load_city_name_map",
            return_value=_TEST_CITY_MAP,
        ):
            store = MagicMock()
            store.write_source.return_value = _FAKE_SOURCE_ID
            extractor = MagicMock()
            extractor.extract.return_value = []
            ingester = FileIngester(
                store=store,
                extractor=extractor,
                cache_path=str(cache_file),
            )

        ingester.ingest(str(f))

        assert cache_file.exists()
        cache = json.loads(cache_file.read_text())
        assert str(f.resolve()) in cache["file"]


# ══════════════════════════════════════════════════════════════════════════════
# CLI guard: --ingest file without --ingest-path
# ══════════════════════════════════════════════════════════════════════════════

class TestMissingIngestPath:
    def test_exits_with_error_when_ingest_path_missing(self, capsys):
        """_run_ingest('file', None) must exit(1) before touching Notion."""
        from main import _run_ingest

        with pytest.raises(SystemExit) as exc_info:
            _run_ingest("file", None)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "--ingest-path" in err

    def test_exits_before_notion_import(self, capsys):
        """The guard fires before NotionSignalStore is ever imported/constructed."""
        from main import _run_ingest

        # If Notion were imported it would fail (no NOTION_API_KEY in test env
        # necessarily). The guard must fire first — if this test passes without
        # a Notion error, the guard is pre-import.
        with patch(
            "pipeline.layer2.notion_client.NotionSignalStore.__init__",
            side_effect=AssertionError("Notion constructor must NOT be called"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_ingest("file", None)

        assert exc_info.value.code == 1
