"""
tests/unit/test_cache_bust.py — bust_community_cache()

Verifies:
  - correct community files are deleted
  - files for OTHER communities are not deleted
  - config/charter_law/ is never touched (even if path trick attempted)
  - returns the correct summary dict
  - missing dirs are noted in skipped, not raised
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make app/ importable so cache_utils can be imported directly
APP_DIR = Path(__file__).resolve().parent.parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import cache_utils  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────

def _write(path: Path, content: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── community + synthesis cache ────────────────────────────────────────────────

def test_community_json_deleted(tmp_path):
    comm_dir = tmp_path / "data" / "cache" / "community" / "nm" / "nm-questa"
    _write(comm_dir / "s3_facts_raw.json")
    _write(comm_dir / "s4_verified.json")
    _write(comm_dir / "s5_scorecard.json")

    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert len(result["deleted"]) == 3
    assert result["error"] is None
    for path in [comm_dir / "s3_facts_raw.json", comm_dir / "s4_verified.json", comm_dir / "s5_scorecard.json"]:
        assert not path.exists()


def test_synthesis_json_deleted(tmp_path):
    syn_dir = tmp_path / "data" / "cache" / "synthesis" / "nm" / "nm-questa"
    _write(syn_dir / "s6_brief_maturity_adjusted_mode2.json")

    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert any("s6_brief" in d for d in result["deleted"])
    assert result["error"] is None


def test_nonexistent_dirs_go_to_skipped(tmp_path):
    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert result["deleted"] == []
    assert result["error"] is None
    assert len(result["skipped"]) >= 2  # community dir + synthesis dir both missing


# ── scoping guard: other communities must NOT be deleted ───────────────────────

def test_other_community_files_untouched(tmp_path):
    """Files for nm-santa-fe must NOT be deleted when busting nm-questa."""
    questa_dir = tmp_path / "data" / "cache" / "community" / "nm" / "nm-questa"
    santa_dir  = tmp_path / "data" / "cache" / "community" / "nm" / "nm-santa-fe"
    _write(questa_dir / "s3_facts_raw.json")
    _write(santa_dir  / "s3_facts_raw.json")

    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert not (questa_dir / "s3_facts_raw.json").exists(), "questa file should be deleted"
    assert (santa_dir / "s3_facts_raw.json").exists(),      "santa-fe file must NOT be deleted"


def test_fetcher_scoped_by_community_id(tmp_path):
    """Fetcher files only deleted when community_id OR leaid is in the path."""
    fetcher_dir = tmp_path / "data" / "cache" / "fetcher"
    target_file = fetcher_dir / "nm-questa" / "acs.json"
    other_file  = fetcher_dir / "nm-albuquerque" / "acs.json"
    _write(target_file)
    _write(other_file)

    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert not target_file.exists(), "questa fetcher file should be deleted"
    assert other_file.exists(),      "albuquerque fetcher file must NOT be deleted"


def test_fetcher_matched_by_leaid(tmp_path):
    """Fetcher files matched by leaid (district_nces_id) are also deleted."""
    leaid = "3507710"
    fetcher_dir = tmp_path / "data" / "cache" / "fetcher" / "nces"
    leaid_file  = fetcher_dir / f"{leaid}_enrollment.json"
    other_file  = fetcher_dir / "9999999_enrollment.json"
    _write(leaid_file)
    _write(other_file)

    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", leaid)

    assert not leaid_file.exists(), "leaid-named fetcher file should be deleted"
    assert other_file.exists(),     "other leaid file must NOT be deleted"


# ── charter_law guard ──────────────────────────────────────────────────────────

def test_charter_law_never_deleted(tmp_path):
    """config/charter_law/ must never be touched even if a file path matches."""
    charter_law_dir = tmp_path / "config" / "charter_law"
    protected_file  = charter_law_dir / "nm-questa.yaml"
    _write(protected_file, "protected: true")

    # Trick: the community dir is set to point at charter_law to simulate attack
    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", charter_law_dir):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert protected_file.exists(), "charter_law file must NEVER be deleted"


# ── summary dict shape ─────────────────────────────────────────────────────────

def test_summary_keys_present(tmp_path):
    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert set(result.keys()) == {"deleted", "skipped", "protected", "error"}
    assert isinstance(result["deleted"], list)
    assert isinstance(result["skipped"], list)
    assert isinstance(result["protected"], list)
    assert result["error"] is None


def test_protected_always_includes_charter_law(tmp_path):
    with patch.object(cache_utils, "_REPO_ROOT", tmp_path), \
         patch.object(cache_utils, "_CACHE_ROOT", tmp_path / "data" / "cache"), \
         patch.object(cache_utils, "_CONFIG_CHARTER_LAW", tmp_path / "config" / "charter_law"):
        result = cache_utils.bust_community_cache("nm-questa", "NM", "3507710")

    assert any("charter_law" in p for p in result["protected"])
