"""
tests/unit/test_ui_cities.py

Tests for the Flask UI:
  - /api/cities returns no duplicate display names within any state
  - The scan-panel template has the combobox (role=combobox) and no plain <select>
    in the city-field-group, and the hidden name="city" input is present.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make app/ui and app/ importable
_UI_DIR  = Path(__file__).resolve().parent.parent.parent / "app" / "ui"
_APP_DIR = Path(__file__).resolve().parent.parent.parent / "app"
for _p in (_UI_DIR, _APP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytestmark = pytest.mark.unit


@pytest.fixture()
def client():
    import server as srv
    srv.app.config["TESTING"] = True
    srv.app.config["SECRET_KEY"] = "test-secret"
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_email"] = "test@example.com"
            sess["user_name"] = "Test"
        with (
            patch.object(srv.rate_limit, "check_daily_limit", return_value=(True, "", 0)),
            patch.object(srv.rate_limit, "check_cost_cap",    return_value=(True, "", 0.0)),
            patch("threading.Thread"),
        ):
            yield c


# ── /api/cities deduplication ───────────────────────────────────────────────

class TestCitiesDedup:

    def test_no_duplicate_names_ms(self, client):
        resp = client.get("/api/cities?state=MS")
        assert resp.status_code == 200
        cities = resp.get_json()
        names = [c["name"] for c in cities]
        assert len(names) == len(set(names)), (
            f"Duplicate names in MS cities: "
            f"{[n for n in names if names.count(n) > 1]}"
        )

    def test_no_duplicate_names_tn(self, client):
        resp = client.get("/api/cities?state=TN")
        assert resp.status_code == 200
        cities = resp.get_json()
        names = [c["name"] for c in cities]
        assert len(names) == len(set(names))

    def test_no_duplicate_names_wi(self, client):
        resp = client.get("/api/cities?state=WI")
        assert resp.status_code == 200
        cities = resp.get_json()
        names = [c["name"] for c in cities]
        assert len(names) == len(set(names))

    def test_no_duplicate_names_nm(self, client):
        resp = client.get("/api/cities?state=NM")
        assert resp.status_code == 200
        cities = resp.get_json()
        names = [c["name"] for c in cities]
        assert len(names) == len(set(names))

    def test_oxford_appears_once_in_ms(self, client):
        resp = client.get("/api/cities?state=MS")
        cities = resp.get_json()
        oxford_entries = [c for c in cities if c["name"] == "Oxford, MS"]
        assert len(oxford_entries) == 1, (
            f"Expected exactly 1 Oxford entry, got {len(oxford_entries)}: {oxford_entries}"
        )

    def test_all_active_states_no_duplicates(self, client):
        """Combined all-states response has no duplicate names within any state."""
        import server as srv
        states_data = srv._load_states()
        for code, info in states_data.items():
            if code.startswith("_") or code == "version":
                continue
            if not isinstance(info, dict) or info.get("status") != "ACTIVE":
                continue
            resp = client.get(f"/api/cities?state={code}")
            assert resp.status_code == 200
            cities = resp.get_json()
            names = [c["name"] for c in cities]
            dupes = [n for n in set(names) if names.count(n) > 1]
            assert not dupes, f"Duplicates in {code}: {dupes}"


# ── Template source: combobox present, no bare <select> in city-field-group ─
# Read the template source directly — avoids Flask request-context machinery
# needed by url_for() and still verifies the markup we care about.

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "app" / "ui" / "templates" / "index.html"
)


class TestComboboxTemplate:

    @classmethod
    def _src(cls) -> str:
        return _TEMPLATE_PATH.read_text()

    def test_no_plain_select_in_city_field_group(self):
        import re
        src = self._src()
        block = re.search(r'id="city-field-group"(.*?)id="mode-label"', src, re.S)
        assert block, "city-field-group block not found in template"
        assert '<select id="scan-city"' not in block.group(0), (
            "Plain <select id='scan-city'> still present — expected combobox"
        )

    def test_combobox_role_present(self):
        assert 'role="combobox"' in self._src(), (
            "role='combobox' not found in template"
        )

    def test_hidden_city_input_present(self):
        src = self._src()
        assert 'type="hidden"' in src, "type='hidden' not found"
        assert 'id="scan-city"' in src, "id='scan-city' not found"

    def test_city_listbox_present(self):
        src = self._src()
        assert 'id="city-listbox"' in src, "city-listbox element not found"
        assert 'role="listbox"' in src, "role='listbox' not found"
