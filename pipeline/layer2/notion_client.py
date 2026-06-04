"""
pipeline/layer2/notion_client.py

Live Notion backend for Layer 2 soft signals (`NotionSignalStore`).

Uses the `notion-client` Python SDK with NOTION_API_KEY from the environment and
the four database IDs from config/notion_ids.yaml. Implements the SignalStore
interface, mapping the canonical dict shapes to/from Notion properties.

The SDK is imported lazily inside __init__ so this module stays importable (and
the rest of layer2 keeps working) even when `notion-client` is not installed —
nothing here touches the network until you actually construct the store.

Resilience: Notion enforces ~3 req/s per integration. Every API call goes
through `_call`, which throttles to that rate and retries on HTTP 429 with
exponential backoff, honoring the Retry-After header. Writes are serialized
(one signal == one row, no nested-block bloat).

The `__main__` block is a manual smoke test that hits the network — it is NOT a
pytest test and must never be collected by the suite.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import yaml

from pipeline.layer2 import decay as decay_mod
from pipeline.layer2.blend import resolve_dimension_config
from pipeline.layer2.storage_interface import SignalStore

_NOTION_IDS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "notion_ids.yaml"
)

# ~3 requests/sec per integration → minimum spacing between calls.
_DEFAULT_MIN_INTERVAL = 0.34
_DEFAULT_MAX_RETRIES = 5


def _load_notion_ids(path: Optional[str] = None) -> dict:
    with open(path or _NOTION_IDS_PATH) as f:
        return yaml.safe_load(f) or {}


# ── property builders (dict → Notion) ─────────────────────────────────────────

def _p_title(value: str) -> dict:
    return {"title": [{"text": {"content": value or ""}}]}


def _p_text(value: Optional[str]) -> dict:
    return {"rich_text": [{"text": {"content": value}}] if value else []}


def _p_number(value) -> dict:
    return {"number": None if value is None else float(value)}


def _p_select(value: Optional[str]) -> dict:
    return {"select": {"name": value} if value else None}


def _p_checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def _p_url(value: Optional[str]) -> dict:
    return {"url": value or None}


def _p_relation(page_ids) -> dict:
    if not page_ids:
        return {"relation": []}
    if isinstance(page_ids, str):
        page_ids = [page_ids]
    return {"relation": [{"id": pid} for pid in page_ids]}


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _p_date(value) -> dict:
    iso = _iso(value)
    return {"date": {"start": iso} if iso else None}


# ── property readers (Notion → dict) ──────────────────────────────────────────

def _r_title(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("title", []))


def _r_text(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def _r_number(prop: dict):
    return prop.get("number")


def _r_select(prop: dict) -> Optional[str]:
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _r_checkbox(prop: dict) -> bool:
    return bool(prop.get("checkbox"))


def _r_url(prop: dict) -> Optional[str]:
    return prop.get("url")


def _r_relation_ids(prop: dict) -> List[str]:
    return [r.get("id") for r in prop.get("relation", [])]


def _r_date(prop: dict):
    d = prop.get("date")
    if not d or not d.get("start"):
        return None
    raw = d["start"]
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return date.fromisoformat(raw)
    except ValueError:
        return raw


class NotionSignalStore(SignalStore):
    """SignalStore backed by four Notion databases."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        notion_ids: Optional[dict] = None,
        ids_path: Optional[str] = None,
        min_interval: float = _DEFAULT_MIN_INTERVAL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        try:
            from notion_client import Client  # lazy: only needed to go live
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "notion-client is not installed; `pip install notion-client` to use "
                "the live Notion backend"
            ) from exc

        self._api_key = api_key or os.environ.get("NOTION_API_KEY")
        if not self._api_key:
            raise RuntimeError("NOTION_API_KEY is not set in the environment")

        ids = notion_ids if notion_ids is not None else _load_notion_ids(ids_path)
        missing = [
            k for k in ("signal_db_id", "source_db_id", "city_db_id", "dimension_db_id")
            if not ids.get(k)
        ]
        if missing:
            raise RuntimeError(
                "config/notion_ids.yaml is missing database IDs: %s — create the "
                "Notion databases first" % ", ".join(missing)
            )
        self.signal_db_id = ids["signal_db_id"]
        self.source_db_id = ids["source_db_id"]
        self.city_db_id = ids["city_db_id"]
        self.dimension_db_id = ids["dimension_db_id"]

        self._client = Client(auth=self._api_key)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last_call = 0.0
        # caches for relation resolution
        self._dim_id_by_key: Dict[str, str] = {}
        self._dim_key_by_id: Dict[str, str] = {}
        self._city_id_by_slug: Dict[str, str] = {}

    # ── request plumbing ──────────────────────────────────────────────────────

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _call(self, fn, **kwargs):
        """Throttle to ~3 req/s and retry on 429 with exponential backoff."""
        for attempt in range(self._max_retries):
            self._throttle()
            try:
                return fn(**kwargs)
            except Exception as exc:  # noqa: BLE001 - inspect status, then re-raise
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                is_rate_limited = status == 429 or status == "rate_limited"
                if not is_rate_limited or attempt == self._max_retries - 1:
                    raise
                headers = getattr(exc, "headers", None) or {}
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
                delay = float(retry_after) if retry_after else 2.0 ** attempt
                time.sleep(delay)
        raise RuntimeError("unreachable: retry loop exhausted")  # pragma: no cover

    # ── relation resolution ───────────────────────────────────────────────────

    def _load_dimension_maps(self) -> None:
        if self._dim_id_by_key:
            return
        cursor = None
        while True:
            resp = self._call(
                self._client.databases.query,
                database_id=self.dimension_db_id,
                start_cursor=cursor,
            )
            for page in resp.get("results", []):
                props = page["properties"]
                key = _r_title(props.get("display_name", {}))
                if key:
                    self._dim_id_by_key[key] = page["id"]
                    self._dim_key_by_id[page["id"]] = key
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

    def _dimension_page_id(self, dimension: str) -> Optional[str]:
        self._load_dimension_maps()
        return self._dim_id_by_key.get(dimension)

    def _city_page_id(self, city_slug: str) -> Optional[str]:
        if city_slug in self._city_id_by_slug:
            return self._city_id_by_slug[city_slug]
        resp = self._call(
            self._client.databases.query,
            database_id=self.city_db_id,
            filter={"property": "city_slug", "title": {"equals": city_slug}},
        )
        results = resp.get("results", [])
        if not results:
            return None
        pid = results[0]["id"]
        self._city_id_by_slug[city_slug] = pid
        return pid

    # ── SignalStore implementation ────────────────────────────────────────────

    def write_signal(self, signal: dict) -> str:
        self._validate_evidence_text(signal)

        city_id = self._city_page_id(signal["city"])
        dim_id = self._dimension_page_id(signal["dimension"])
        title = "%s · %s · %s" % (
            signal.get("city", ""),
            signal.get("dimension", ""),
            signal.get("direction", ""),
        )
        meta = signal.get("source_metadata")
        props = {
            "name": _p_title(title),
            "city": _p_relation(city_id),
            "dimension": _p_relation(dim_id),
            "direction": _p_select(signal.get("direction")),
            "magnitude": _p_number(signal.get("magnitude")),
            "confidence": _p_number(signal.get("confidence")),
            "evidence_text": _p_text(signal.get("evidence_text")),
            "source": _p_relation(signal.get("source_id")),
            "source_date": _p_date(signal.get("source_date")),
            "ingested_at": _p_date(signal.get("ingested_at") or datetime.now(timezone.utc)),
            "status": _p_select(signal.get("status", "active")),
            "confidence_tier": _p_select(signal.get("confidence_tier")),
            "operator_verified": _p_checkbox(signal.get("operator_verified", False)),
            "operator_note": _p_text(signal.get("operator_note")),
            "superseded_by": _p_relation(signal.get("superseded_by")),
            "source_metadata": _p_text(_dump_metadata(meta)),
        }
        page = self._call(
            self._client.pages.create,
            parent={"database_id": self.signal_db_id},
            properties=props,
        )
        return page["id"]

    def get_signals(
        self,
        city_slug: str,
        dimension: str,
        status: str = "active",
    ) -> List[dict]:
        city_id = self._city_page_id(city_slug)
        dim_id = self._dimension_page_id(dimension)
        and_filters: List[dict] = [{"property": "status", "select": {"equals": status}}]
        if city_id:
            and_filters.append({"property": "city", "relation": {"contains": city_id}})
        if dim_id:
            and_filters.append(
                {"property": "dimension", "relation": {"contains": dim_id}}
            )

        out: List[dict] = []
        cursor = None
        while True:
            resp = self._call(
                self._client.databases.query,
                database_id=self.signal_db_id,
                filter={"and": and_filters},
                start_cursor=cursor,
            )
            for page in resp.get("results", []):
                out.append(self._page_to_signal(page, city_slug, dimension))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return out

    def update_signal(self, signal_id: str, updates: dict) -> None:
        if "evidence_text" in updates:
            self._validate_evidence_text(updates)
        props: Dict[str, Any] = {}
        for field, value in updates.items():
            if field in ("status", "confidence_tier", "direction"):
                props[field] = _p_select(value)
            elif field in ("magnitude", "confidence"):
                props[field] = _p_number(value)
            elif field in ("evidence_text", "operator_note"):
                props[field] = _p_text(value)
            elif field == "operator_verified":
                props[field] = _p_checkbox(value)
            elif field in ("source_date", "ingested_at"):
                props[field] = _p_date(value)
            elif field == "superseded_by":
                props[field] = _p_relation(value)
            elif field == "source_metadata":
                props[field] = _p_text(_dump_metadata(value))
        if props:
            self._call(self._client.pages.update, page_id=signal_id, properties=props)

    def expire_stale_signals(self, decay_floor: float = 0.1) -> int:
        self._load_dimension_maps()
        as_of = datetime.now(timezone.utc)
        expired = 0
        cursor = None
        while True:
            resp = self._call(
                self._client.databases.query,
                database_id=self.signal_db_id,
                filter={"property": "status", "select": {"equals": "active"}},
                start_cursor=cursor,
            )
            for page in resp.get("results", []):
                props = page["properties"]
                dim_ids = _r_relation_ids(props.get("dimension", {}))
                dim_key = self._dim_key_by_id.get(dim_ids[0]) if dim_ids else None
                src_date = _r_date(props.get("source_date", {}))
                if not dim_key or src_date is None:
                    continue
                cfg = resolve_dimension_config(dim_key)
                d = decay_mod.compute_decay(
                    src_date,
                    cfg.half_life_days,
                    as_of=as_of,
                    operator_verified=_r_checkbox(props.get("operator_verified", {})),
                    verified_multiplier=cfg.verified_half_life_multiplier,
                )
                if decay_mod.is_stale(d, decay_floor):
                    self._call(
                        self._client.pages.update,
                        page_id=page["id"],
                        properties={"status": _p_select("expired")},
                    )
                    expired += 1
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return expired

    def get_source_by_external_id(self, external_id: str) -> Optional[dict]:
        resp = self._call(
            self._client.databases.query,
            database_id=self.source_db_id,
            filter={"property": "external_id", "rich_text": {"equals": external_id}},
        )
        results = resp.get("results", [])
        return self._page_to_source(results[0]) if results else None

    def write_source(self, source: dict) -> str:
        title = source.get("title") or "%s · %s" % (
            source.get("source_type", ""),
            source.get("external_id", ""),
        )
        props = {
            "name": _p_title(title),
            "source_type": _p_select(source.get("source_type")),
            "external_id": _p_text(source.get("external_id")),
            "title": _p_text(source.get("title")),
            "source_date": _p_date(source.get("source_date")),
            "ingested_at": _p_date(source.get("ingested_at") or datetime.now(timezone.utc)),
            "raw_url": _p_url(source.get("raw_url")),
        }
        page = self._call(
            self._client.pages.create,
            parent={"database_id": self.source_db_id},
            properties=props,
        )
        return page["id"]

    # ── page → dict mappers ────────────────────────────────────────────────────

    def _page_to_signal(self, page: dict, city_slug: str, dimension: str) -> dict:
        p = page["properties"]
        src_ids = _r_relation_ids(p.get("source", {}))
        sup_ids = _r_relation_ids(p.get("superseded_by", {}))
        return {
            "signal_id": page["id"],
            "city": city_slug,
            "dimension": dimension,
            "direction": _r_select(p.get("direction", {})),
            "magnitude": _r_number(p.get("magnitude", {})),
            "confidence": _r_number(p.get("confidence", {})),
            "evidence_text": _r_text(p.get("evidence_text", {})),
            "source_id": src_ids[0] if src_ids else None,
            "source_date": _r_date(p.get("source_date", {})),
            "ingested_at": _r_date(p.get("ingested_at", {})),
            "status": _r_select(p.get("status", {})),
            "confidence_tier": _r_select(p.get("confidence_tier", {})),
            "operator_verified": _r_checkbox(p.get("operator_verified", {})),
            "operator_note": _r_text(p.get("operator_note", {})),
            "superseded_by": sup_ids[0] if sup_ids else None,
            "source_metadata": _load_metadata(_r_text(p.get("source_metadata", {}))),
        }

    def _page_to_source(self, page: dict) -> dict:
        p = page["properties"]
        return {
            "source_id": page["id"],
            "source_type": _r_select(p.get("source_type", {})),
            "external_id": _r_text(p.get("external_id", {})),
            "title": _r_text(p.get("title", {})),
            "source_date": _r_date(p.get("source_date", {})),
            "ingested_at": _r_date(p.get("ingested_at", {})),
            "raw_url": _r_url(p.get("raw_url", {})),
        }


def _dump_metadata(meta) -> str:
    if not meta:
        return ""
    import json

    return json.dumps(meta) if not isinstance(meta, str) else meta


def _load_metadata(text: str):
    if not text:
        return {}
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": text}


if __name__ == "__main__":  # pragma: no cover - manual, network-touching smoke test
    # Dev round-trip: write a source + signal, read it back, update, expire.
    # NOT a pytest test. Requires NOTION_API_KEY + populated config/notion_ids.yaml
    # + `pip install notion-client`. Run: python -m pipeline.layer2.notion_client
    store = NotionSignalStore()
    src_id = store.write_source(
        {
            "source_type": "manual",
            "external_id": "smoke-test-001",
            "title": "Manual smoke test source",
            "source_date": date.today(),
            "raw_url": "https://example.com/smoke",
        }
    )
    print("wrote source:", src_id)

    sig_id = store.write_signal(
        {
            "city": "nm-albuquerque",
            "dimension": "facilities_feasibility",
            "direction": "positive",
            "magnitude": 2,
            "confidence": 0.8,
            "evidence_text": "Smoke-test signal — safe to delete.",
            "source_id": src_id,
            "source_date": date.today(),
            "status": "active",
            "confidence_tier": "review",
            "operator_verified": False,
        }
    )
    print("wrote signal:", sig_id)

    got = store.get_signals("nm-albuquerque", "facilities_feasibility")
    print("read back %d active signal(s)" % len(got))

    store.update_signal(sig_id, {"confidence_tier": "verified", "operator_verified": True})
    print("updated signal")

    n = store.expire_stale_signals()
    print("expired %d stale signal(s)" % n)
