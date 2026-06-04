"""
pipeline/layer2/ingest/_mcp_client.py

Minimal MCP JSON-RPC client over HTTP. Supports both plain JSON and
Server-Sent Events (SSE) response bodies.

Not public API — used only by the ingest source modules.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Raised when the MCP server returns a JSON-RPC error object."""


class MCPUnexpectedSchema(RuntimeError):
    """Raised when a tool's response doesn't match the expected structure.

    Per spec stopping rules: callers should print the raw response and halt.
    """


class MCPHTTPClient:
    """Synchronous MCP client that communicates over HTTP/HTTPS.

    Uses ``httpx`` (available as a transitive dep of ``anthropic``).
    Handles both ``application/json`` and ``text/event-stream`` responses.
    """

    def __init__(self, url: str, headers: Optional[dict] = None) -> None:
        self._url = url
        self._extra_headers = headers or {}
        self._seq = 0
        self._http: Any = None  # lazy httpx.Client

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=60.0)
        return self._http

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _post(self, method: str, params: Optional[dict] = None) -> Any:
        payload: dict = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }

        resp = self._client().post(self._url, json=payload, headers=headers)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            return self._parse_sse(resp.text)

        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP error from {self._url}: {data['error']}")
        return data.get("result")

    @staticmethod
    def _parse_sse(text: str) -> Any:
        """Extract the first data payload from an SSE response body."""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data: "):
                raw = line[6:].strip()
                if raw in ("", "[DONE]"):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "error" in obj:
                    raise MCPError(f"MCP SSE error: {obj['error']}")
                return obj.get("result", obj)
        raise MCPError("SSE response contained no parseable data events")

    # ── public API ─────────────────────────────────────────────────────────────

    def initialize(self) -> dict:
        result = self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "clip-ingest", "version": "1.0"},
        })
        # Send initialized notification (fire-and-forget — some servers require it)
        try:
            self._client().post(
                self._url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={
                    "Content-Type": "application/json",
                    **self._extra_headers,
                },
                timeout=10.0,
            )
        except Exception:
            pass
        return result or {}

    def list_tools(self) -> list[dict]:
        result = self._post("tools/list") or {}
        if isinstance(result, list):
            return result
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> Any:
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        # Some servers wrap tool output in {"content": [...]} per MCP spec
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            # Unwrap a single text/json item
            if isinstance(content, list) and len(content) == 1:
                item = content[0]
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return text
                    return item
        return result
