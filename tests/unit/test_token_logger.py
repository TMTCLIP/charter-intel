"""
tests/unit/test_token_logger.py

Regression guard: token_logger.finalize() must write to data/logs/,
the directory that is symlinked to the persistent volume in production.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.utils.token_logger import TokenLogger

pytestmark = pytest.mark.unit


class TestTokenLoggerPath:
    def test_finalize_writes_to_data_logs(self, tmp_path, monkeypatch):
        """finalize() must place the CSV under data/logs/ (the persisted dir)."""
        # Run from a temp dir so we don't pollute the repo's data/logs/
        monkeypatch.chdir(tmp_path)
        logs_dir = tmp_path / "data" / "logs"
        logs_dir.mkdir(parents=True)

        logger = TokenLogger()
        logger.set_run_id("test-run-001")
        logger.log_call(
            stage="s1",
            model="claude-sonnet-4-5",
            tokens_input=100,
            tokens_output=50,
            community_id="test-city",
        )
        logger.finalize()

        written = list(logs_dir.glob("token_usage_*.csv"))
        assert len(written) == 1, f"Expected 1 CSV in data/logs/, found: {written}"
        assert written[0].name == "token_usage_test-run-001.csv"

    def test_finalize_csv_contains_logged_call(self, tmp_path, monkeypatch):
        """The written CSV must include the stage and community that were logged."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data" / "logs").mkdir(parents=True)

        logger = TokenLogger()
        logger.set_run_id("test-run-002")
        logger.log_call(
            stage="s3",
            model="claude-haiku-4-5-20251001",
            tokens_input=200,
            tokens_output=80,
            community_id="albuquerque-nm",
        )
        logger.finalize()

        csv_path = tmp_path / "data" / "logs" / "token_usage_test-run-002.csv"
        content = csv_path.read_text()
        assert "s3" in content
        assert "albuquerque-nm" in content
