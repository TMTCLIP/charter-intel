"""
pipeline/utils/batch_runner.py
Anthropic Message Batches API layer for multi-community pipeline runs.

When ``--batch --all`` is active, every ``call_claude`` invocation from
every community thread is collected and submitted to the Batch API in one
request, giving ~50 % cost reduction on both input and output tokens.

Architecture
────────────
BatchGateway   — thread-safe drop-in for ``call_claude``.
                 Community worker threads submit requests and immediately
                 block on a ``threading.Event``.  A background coordinator
                 thread wakes every ``_FLUSH_INTERVAL_S`` seconds, snapshots
                 all pending requests, submits them as one batch, polls until
                 the batch finishes, then unblocks each waiting thread with
                 its APIResult.

Web-search calls (``use_web_search=True``) are not supported by the Batch
API and are passed through to the original synchronous ``call_claude``.

Fallback
────────
If batch submission or retrieval raises any exception, every affected call
falls back to the original synchronous implementation so the pipeline
continues without data loss.

Usage (from main.py)
────────────────────
    import pipeline.utils.api_client as _api
    from pipeline.utils.batch_runner import BatchGateway
    from tests.mock_anthropic import patch_call_claude

    gateway = BatchGateway(original_call_claude=_api.call_claude)
    patch_call_claude(gateway)
    gateway.start()

    with ThreadPoolExecutor(max_workers=N) as ex:
        futures = {ex.submit(run_community_pipeline, c, ...): c for c in communities}
        for f in as_completed(futures): all_results[futures[f]] = f.result()

    gateway.stop()   # drains remaining calls, then shuts down coordinator
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic

from pipeline.utils.api_client import APIResult, _parse_json_response
from pipeline.utils.token_logger import token_logger

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tuning constants
# ─────────────────────────────────────────────────────────────────────────────
_FLUSH_INTERVAL_S = 3.0    # seconds between coordinator flush attempts
_POLL_INTERVAL_S  = 15.0   # seconds between Batch API status polls
_CALL_TIMEOUT_S   = 1800   # 30 min max wait per individual call

# Maximum parallel community threads.  Anthropic batch limit is 10 000
# requests; 12 communities × ~5 API calls each = 60 requests, well within.
MAX_WORKERS = 12


# ─────────────────────────────────────────────────────────────────────────────
# Pending-call dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _PendingCall:
    """One call_claude invocation parked in the gateway queue."""
    model:        str
    system:       str
    user:         str
    max_tokens:   int
    temperature:  float
    expect_json:  bool
    stage:        str
    community_id: str
    # Synchronisation primitives — set by coordinator when result is ready
    event:  threading.Event  = field(default_factory=threading.Event)
    result: Optional[APIResult] = None


# ─────────────────────────────────────────────────────────────────────────────
# BatchGateway
# ─────────────────────────────────────────────────────────────────────────────

class BatchGateway:
    """Thread-safe ``call_claude`` replacement using Anthropic Message Batches API.

    Parameters
    ----------
    original_call_claude:
        The real synchronous ``call_claude`` function captured *before*
        ``patch_call_claude`` replaces it.  Used for web-search calls (which
        the Batch API does not support) and as a fallback if batch submission
        fails.
    """

    def __init__(self, original_call_claude: Callable) -> None:
        self._original    = original_call_claude
        self._pending:    list[_PendingCall] = []
        self._lock        = threading.Lock()
        self._stop        = threading.Event()
        self._coordinator: Optional[threading.Thread] = None
        self._client      = anthropic.Anthropic()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background coordinator thread."""
        self._coordinator = threading.Thread(
            target=self._coordinate, name="batch-coordinator", daemon=True
        )
        self._coordinator.start()
        logger.info("[BATCH] Gateway started — coordinator running")

    def stop(self) -> None:
        """Signal stop, drain any remaining pending calls, join the coordinator."""
        self._stop.set()
        if self._coordinator:
            self._coordinator.join(timeout=_CALL_TIMEOUT_S)
        logger.info("[BATCH] Gateway stopped")

    # ── call_claude drop-in ───────────────────────────────────────────────────

    def __call__(
        self,
        model:                str,
        system:               str,
        user:                 str,
        max_tokens:           int   = 2000,
        temperature:          float = 0.0,
        expect_json:          bool  = False,
        stage:                str   = "",
        community_id:         str   = "",
        retry_attempts:       int   = 3,
        retry_delay_seconds:  float = 5.0,
        use_web_search:       bool  = False,
    ) -> APIResult:
        # Batch API does not support tool use — pass through synchronously
        if use_web_search:
            return self._original(
                model=model, system=system, user=user,
                max_tokens=max_tokens, temperature=temperature,
                expect_json=expect_json, stage=stage, community_id=community_id,
                retry_attempts=retry_attempts,
                retry_delay_seconds=retry_delay_seconds,
                use_web_search=True,
            )

        pending = _PendingCall(
            model=model, system=system, user=user,
            max_tokens=max_tokens, temperature=temperature,
            expect_json=expect_json, stage=stage, community_id=community_id,
        )
        with self._lock:
            self._pending.append(pending)

        logger.debug("[BATCH] Queued  %s/%s", community_id, stage)
        signalled = pending.event.wait(timeout=_CALL_TIMEOUT_S)
        if not signalled or pending.result is None:
            raise RuntimeError(
                f"[BATCH] Timed out ({_CALL_TIMEOUT_S}s) waiting for "
                f"{community_id}/{stage}"
            )
        return pending.result

    # ── Coordinator ───────────────────────────────────────────────────────────

    def _coordinate(self) -> None:
        """Background: sleep → flush until stop signal, then final drain."""
        while not self._stop.is_set():
            time.sleep(_FLUSH_INTERVAL_S)
            self._flush()
        self._flush()  # drain after stop

    def _flush(self) -> None:
        """Snapshot pending calls, submit as batch, deliver results."""
        with self._lock:
            calls, self._pending = self._pending, []

        if not calls:
            return

        logger.info("[BATCH] Flushing %d pending call(s)…", len(calls))
        try:
            self._submit_and_deliver(calls)
        except Exception as exc:
            logger.error(
                "[BATCH] Batch submission failed (%s) — "
                "falling back to synchronous for %d call(s)",
                exc, len(calls),
            )
            self._sync_fallback(calls)

    # ── Batch submit + poll + deliver ─────────────────────────────────────────

    def _submit_and_deliver(self, calls: list[_PendingCall]) -> None:
        requests = [
            {
                "custom_id": str(i),
                "params": {
                    "model":       c.model,
                    "max_tokens":  c.max_tokens,
                    "temperature": c.temperature,
                    "system":      c.system,
                    "messages":    [{"role": "user", "content": c.user}],
                },
            }
            for i, c in enumerate(calls)
        ]

        batch = self._client.beta.messages.batches.create(requests=requests)
        logger.info("[BATCH] Submitted batch %s (%d requests)", batch.id, len(calls))

        # Poll until processing_status == "ended"
        while True:
            batch = self._client.beta.messages.batches.retrieve(batch.id)
            if batch.processing_status == "ended":
                break
            rc = batch.request_counts
            logger.info(
                "[BATCH] %s — processing=%d succeeded=%d errored=%d",
                batch.id, rc.processing, rc.succeeded, rc.errored,
            )
            time.sleep(_POLL_INTERVAL_S)

        # Build custom_id → result item map
        result_map: dict[str, Any] = {
            item.custom_id: item
            for item in self._client.beta.messages.batches.results(batch.id)
        }

        # Deliver to waiting threads
        for i, call in enumerate(calls):
            item = result_map.get(str(i))
            if item and item.result.type == "succeeded":
                msg        = item.result.message
                text       = msg.content[0].text if msg.content else ""
                tokens_in  = msg.usage.input_tokens
                tokens_out = msg.usage.output_tokens
                parsed, parse_error = (
                    _parse_json_response(text) if call.expect_json
                    else (None, None)
                )
                call.result = APIResult(
                    text=text, parsed_json=parsed,
                    tokens_input=tokens_in, tokens_output=tokens_out,
                    model=call.model, stage=call.stage,
                    community_id=call.community_id, parse_error=parse_error,
                )
                token_logger.log_call(
                    stage=call.stage, model=call.model,
                    tokens_input=tokens_in, tokens_output=tokens_out,
                    community_id=call.community_id,
                    via_batch=True,   # 50% Batch API discount applied
                )
                logger.info(
                    "[BATCH] Delivered %s/%s  in=%d out=%d",
                    call.community_id, call.stage, tokens_in, tokens_out,
                )
            else:
                err = None
                if item and hasattr(getattr(item, "result", None), "error"):
                    err = item.result.error
                logger.error(
                    "[BATCH] Request %d (%s/%s) failed: %s",
                    i, call.community_id, call.stage, err,
                )
                call.result = APIResult(
                    text="", parsed_json=None, model=call.model,
                    stage=call.stage, community_id=call.community_id,
                    parse_error=f"Batch request failed: {err}",
                )
            call.event.set()

    # ── Synchronous fallback ──────────────────────────────────────────────────

    def _sync_fallback(self, calls: list[_PendingCall]) -> None:
        """Deliver results via the original synchronous call_claude."""
        for call in calls:
            try:
                call.result = self._original(
                    model=call.model, system=call.system, user=call.user,
                    max_tokens=call.max_tokens, temperature=call.temperature,
                    expect_json=call.expect_json, stage=call.stage,
                    community_id=call.community_id,
                )
            except Exception as exc:
                call.result = APIResult(
                    text="", parsed_json=None, model=call.model,
                    stage=call.stage, community_id=call.community_id,
                    parse_error=f"Sync fallback failed: {exc}",
                )
            finally:
                call.event.set()
