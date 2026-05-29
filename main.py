"""
main.py
Charter Community Intelligence Platform — Pipeline Entry Point

USAGE:
  python main.py "Santa Fe"
  python main.py "Española" --preset turnaround
  python main.py --community nm-albuquerque
  python main.py --all --preset growth --mode 2
  python main.py --all --dry-run
  python main.py --interactive

FLAGS:
  community       Positional city name or community_id (e.g. "Santa Fe" or nm-santa-fe)
  --state         Two-letter state code (default: NM)
  --community     Specific community_id or city name (alternative to positional)
  --all           Run all communities in the state
  --preset        growth | replication | turnaround (default: growth)
  --mode          1 | 2 | 3 (default: 2)
  --force-refresh Ignore cache; regenerate all stages
  --no-cache      Disable cache reads AND writes
  --dry-run       Validate config and inputs without making API calls
  --stages        Comma-separated list of stages to run (default: all)
                  e.g., --stages s5,s6,s7 to re-run only scoring and later
  --interactive   Prompt for state and community interactively

ENVIRONMENT:
  ANTHROPIC_API_KEY  — required for stages that call Claude
"""

import argparse
import datetime
import json
import logging
import os
import sys
import threading
import time
import yaml

from dotenv import load_dotenv
load_dotenv(override=True)  # loads .env → always overrides any empty shell var

from pipeline import (
    OperatorPreset, OutputMode, PipelineConfig,
    StageResult, StageStatus, STAGE_ORDER,
    build_community_id,
)
from pipeline import s1_discovery, s2_state_context, s3_fact_extraction
from pipeline import s4_verification, s5_scoring, s6_synthesis, s7_render
from pipeline.utils.token_logger import token_logger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

STAGE_MODULES = {
    "s1_discovery":     s1_discovery,
    "s2_state_context": s2_state_context,
    "s3_fact_extraction": s3_fact_extraction,
    "s4_verification":  s4_verification,
    "s5_scoring":       s5_scoring,
    "s6_synthesis":     s6_synthesis,
    "s7_render":        s7_render,
}


class _CommunityLogCapture(logging.Handler):
    """Accumulates WARNING+ log lines for one community run (fed to the HTML debug section).

    In parallel (batch) runs multiple capture handlers are attached to the root
    logger simultaneously.  Pass community_id to filter each handler to only
    records that contain ``[{community_id}]`` in the formatted message, preventing
    cross-community contamination of S7 warn_lines.  When community_id is empty
    (single-city, serial mode) no filtering is applied — existing behaviour.
    """

    def __init__(self, community_id: str = "") -> None:
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.lines: list[str] = []
        self._community_id = community_id

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if self._community_id and f"[{self._community_id}]" not in msg:
            return
        self.lines.append(msg)


_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _StageSpinner:
    def __init__(self, community_id: str, stage_num: int, total: int, stage_id: str) -> None:
        bar_width = 20
        filled = int(bar_width * stage_num / total)
        bar = "█" * filled + "░" * (bar_width - filled)
        self._prefix = f"[{community_id}] [{bar}] {stage_num}/{total} — {stage_id}"
        self._start = time.time()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tty = sys.stderr.isatty()

    def start(self) -> None:
        if not self._tty:
            sys.stderr.write(f"\r{self._prefix}")
            sys.stderr.flush()
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            ch = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
            elapsed = time.time() - self._start
            sys.stderr.write(f"\r{self._prefix} {ch} {elapsed:.1f}s")
            sys.stderr.flush()
            idx += 1
            self._stop_event.wait(0.1)

    def stop(self, elapsed: float, error: bool = False) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        mark = "✗" if error else "✓"
        sys.stderr.write(f"\r{self._prefix} {mark} {elapsed:.1f}s")
        sys.stderr.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Charter Community Intelligence Platform"
    )
    # Positional: accept a bare city name or community_id without a flag
    parser.add_argument(
        "community_pos", nargs="?", default=None,
        metavar="COMMUNITY",
        help="City name or community_id (e.g. 'Santa Fe' or nm-santa-fe)"
    )
    parser.add_argument("--state", default="NM", help="Two-letter state code (default: NM)")
    parser.add_argument("--community", help="Specific community_id or city name")
    parser.add_argument("--all", action="store_true", dest="run_all",
                        help="Run all communities in state")
    parser.add_argument("--preset", default="growth",
                        choices=["growth", "replication", "turnaround", "maturity_adjusted"])
    parser.add_argument(
        "--mode", default="2",
        help="1 | 2 | 3 (output depth) or 'zip' for ZIP Drill v1 or 'zip_v2' for ZIP Drill v2"
    )
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stages", help="Comma-separated stages to run")
    parser.add_argument(
        "--depth", default="standard", choices=["fast", "standard", "deep"],
        help=(
            "Web search depth for S3 fact extraction. "
            "fast=none (quick triage), standard=political_climate only (default), "
            "deep=all 5 dimensions (high-priority markets)"
        ),
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="Prompt for state and community interactively"
    )
    # ── Testing / fixture flags ─────────────────────────────────────────────
    parser.add_argument(
        "--mock", action="store_true",
        help=(
            "Replay recorded fixtures instead of making API calls. "
            "Requires fixtures recorded with --record. "
            "Fails loudly if any fixture is missing."
        ),
    )
    parser.add_argument(
        "--record", action="store_true",
        help=(
            "Run the pipeline normally and persist every API request+response "
            "to tests/fixtures/{community_id}/{stage}/call_N.json."
        ),
    )
    parser.add_argument(
        "--force-record", action="store_true",
        help="Overwrite existing fixture files when recording (use with --record).",
    )
    parser.add_argument(
        "--batch", action="store_true",
        help=(
            "Use Anthropic Message Batches API (~50%% cost reduction). "
            "call_claude invocations are collected and submitted as batches; "
            "single-city runs add ~3-5 min polling latency. "
            "Incompatible with --mock and --record."
        ),
    )
    return parser.parse_args()


def _is_community_id(value: str) -> bool:
    """Return True if value looks like a canonical community_id (e.g. nm-santa-fe)."""
    # Community IDs are lowercase, start with a two-letter state code + hyphen,
    # and contain no spaces.
    return bool(value) and " " not in value and "-" in value and value == value.lower()


def resolve_community(state: str, city_name: str) -> str:
    """Convert a plain city name to a canonical community_id.

    Accepts either a community_id (returned unchanged) or a plain city name
    which is normalised through build_community_id.

    Examples:
        resolve_community("NM", "Santa Fe")    → "nm-santa-fe"
        resolve_community("NM", "Española")    → "nm-espanola"
        resolve_community("NM", "nm-santa-fe") → "nm-santa-fe"
    """
    if _is_community_id(city_name):
        return city_name
    return build_community_id(city_name, state)


def build_config(args: argparse.Namespace) -> PipelineConfig:
    state = args.state.upper()

    # Resolve community: positional arg takes precedence over --community flag
    raw_community = args.community_pos or args.community
    communities = None
    if raw_community:
        resolved = resolve_community(state, raw_community)
        logger.info("Resolved community: %r → %s", raw_community, resolved)
        communities = [resolved]

    return PipelineConfig(
        state=state,
        preset=OperatorPreset(args.preset),
        mode=OutputMode(args.mode),
        output_format="markdown",
        cache_enabled=not args.no_cache,
        dry_run=args.dry_run,
        force_refresh=args.force_refresh,
        communities=communities,
        depth=args.depth,
    )


def run_community_pipeline(
    community_id: str,
    state: str,
    config: PipelineConfig,
    stages_to_run: list[str]
) -> dict[str, StageResult]:
    """Run the full pipeline for one community. Returns stage_id → StageResult."""
    results = {}
    previous = None
    total_stages = len(stages_to_run)

    if total_stages == 0:
        return results

    capture = _CommunityLogCapture(community_id=community_id)
    logging.getLogger().addHandler(capture)
    try:
        for i, stage_id in enumerate(stages_to_run, 1):
            module = STAGE_MODULES.get(stage_id)
            if not module:
                logger.warning(f"Unknown stage '{stage_id}' — skipping")
                continue

            spinner = _StageSpinner(community_id, i, total_stages, stage_id)
            spinner.start()
            logger.info(f"[{community_id}] Running {stage_id}...")
            start = time.time()

            extra: dict = {}
            if stage_id == "s7_render":
                extra["warn_lines"] = list(capture.lines)

            try:
                result = module.run(
                    community_id=community_id,
                    state=state,
                    config=config,
                    previous_result=previous,
                    **extra
                )
            except Exception as e:
                result = StageResult(
                    stage_id=stage_id,
                    community_id=community_id,
                    state=state,
                    status=StageStatus.ERROR,
                    errors=[f"Unhandled exception: {str(e)}"]
                )
                logger.exception(f"[{community_id}] {stage_id} raised exception")

            elapsed = round(time.time() - start, 2)
            spinner.stop(elapsed, error=result.status == StageStatus.ERROR)
            sys.stderr.write("\n")
            sys.stderr.flush()
            results[stage_id] = result

            if result.warnings:
                for w in result.warnings:
                    logger.warning(f"[{community_id}] [{stage_id}] {w}")

            if result.status == StageStatus.ERROR:
                logger.error(
                    f"[{community_id}] {stage_id} FAILED: {result.errors}. "
                    f"Halting pipeline for this community."
                )
                break
            elif result.cache_hit:
                logger.info(f"[{community_id}] {stage_id} — cache hit ({elapsed}s)")
            else:
                logger.info(
                    f"[{community_id}] {stage_id} — OK "
                    f"({elapsed}s, {result.tokens_used} tokens)"
                )

            previous = result

    finally:
        logging.getLogger().removeHandler(capture)

    return results


def _print_dry_run_cost_estimate(
    stages_to_run: list,
    communities: list,
    config: "PipelineConfig",
    use_batch_pricing: bool = False,
) -> None:
    """Print a heuristic token/cost estimate for a planned pipeline run.

    Derived from observed nm-questa standard-depth token counts.
    Actual usage varies ±30–50% depending on fact density and response length.

    When use_batch_pricing=True (--batch flag present), applies the Anthropic
    Batch API 50% discount to all estimated costs.
    """
    n = len(communities)
    depth = getattr(config, "depth", "standard")

    # (stage_id, model_tier, est_input_tokens, est_output_tokens, skip_when_fast)
    # S3 fires once for main extraction, then again for political_climate web search
    # at standard/deep depth.  S6 fires twice: synthesis (Haiku) + audit (Haiku).
    STAGE_COSTS = [
        ("s3_fact_extraction", "sonnet", 3_700, 3_600, False),
        ("s3_fact_extraction", "sonnet", 4_500, 2_000, True),   # political_climate web search
        ("s4_verification",    "haiku",  3_000,   800, False),
        ("s6_synthesis",       "haiku",  8_000, 5_000, False),  # synthesis  (Haiku, Session 15)
        ("s6_synthesis",       "haiku",  6_000, 3_000, False),  # audit pass (Haiku, Session 15)
    ]
    # Pricing ($/1M tokens): input, output
    PRICE = {
        "sonnet": (3.00, 15.00),
        "haiku":  (0.80,  4.00),
    }
    # Batch API: 50% discount on both input and output tokens
    batch_factor = 0.5 if use_batch_pricing else 1.0

    total_in = total_out = 0
    total_usd = 0.0

    for stage_id, model, est_in, est_out, skip_if_fast in STAGE_COSTS:
        if stage_id not in stages_to_run:
            continue
        if skip_if_fast and depth == "fast":
            continue
        p_in, p_out = PRICE[model]
        total_in  += est_in  * n
        total_out += est_out * n
        total_usd += ((est_in * p_in + est_out * p_out) / 1_000_000) * n * batch_factor

    plural = "community" if n == 1 else "communities"
    batch_note = " — Batch API (50% discount)" if use_batch_pricing else ""
    print(f"\n{'─' * 60}")
    print(f"DRY-RUN COST ESTIMATE  ({n} {plural}, {depth} depth{batch_note})")
    print(f"{'─' * 60}")
    print(f"  Est. input tokens : {total_in:>10,}")
    print(f"  Est. output tokens: {total_out:>10,}")
    print(f"  Est. total cost   :  ${total_usd:>8.2f}")
    print(f"  Note: heuristic from nm-questa baseline; actual ±30–50%")
    if use_batch_pricing:
        print(f"  Batch API: 50% discount applied to all stage estimates")
    print(f"{'─' * 60}\n")


def main():
    args = parse_args()

    # ── Interactive mode ────────────────────────────────────────────────────
    if args.interactive:
        try:
            state_input = input("State code [NM]: ").strip() or "NM"
            args.state = state_input.upper()
            city_input = input(f"Community (city name or community_id): ").strip()
            if city_input:
                args.community_pos = city_input
        except EOFError:
            # Support piped input (e.g. echo "NM\nSanta Fe" | python main.py --interactive)
            pass

    # ── ZIP Drill mode ────────────────────────────────────────────────────────
    if args.mode == "zip":
        city = args.community_pos or args.community
        if not city:
            logger.error(
                "--mode zip requires a city name. "
                "Example: python main.py 'Albuquerque' --mode zip"
            )
            sys.exit(1)
        from pipeline import zip_drill
        out_path = zip_drill.run(city, state=args.state)
        print(f"\n  ZIP Drill: {out_path}\n")
        return

    # ── ZIP Drill v2 mode ─────────────────────────────────────────────────────
    if args.mode == "zip_v2":
        city = args.community_pos or args.community
        if not city:
            logger.error(
                "--mode zip_v2 requires a city name. "
                "Example: python main.py 'Albuquerque' --mode zip_v2"
            )
            sys.exit(1)
        from pipeline import zip_drill_v2
        out_path = zip_drill_v2.run_v2(city, state=args.state)
        print(f"\n  ZIP Drill v2: {out_path}\n")
        return

    # Cast mode to int for existing pipeline
    try:
        args.mode = int(args.mode)
    except ValueError:
        logger.error(f"Invalid --mode: {args.mode!r}. Expected 1, 2, 3, or 'zip'.")
        sys.exit(1)
    if args.mode not in (1, 2, 3):
        logger.error(f"--mode must be 1, 2, 3, or 'zip'.")
        sys.exit(1)

    config = build_config(args)

    # ── Mock / record injection ────────────────────────────────────────────────
    if args.mock and args.record:
        logger.error("--mock and --record are mutually exclusive")
        sys.exit(1)

    _mock_client = None
    _recorder = None

    if args.mock:
        import pipeline.utils.api_client as _api_module
        from tests.mock_anthropic import MockCallClaude, patch_call_claude
        _mock_client = MockCallClaude()
        patch_call_claude(_mock_client)
        logger.info("[MOCK] Fixture mock active — zero API calls")
    elif args.record:
        import pipeline.utils.api_client as _api_module
        from tests.mock_anthropic import RecordingCallClaude, patch_call_claude
        _recorder = RecordingCallClaude(
            original_fn=_api_module.call_claude,
            force=args.force_record,
        )
        patch_call_claude(_recorder)
        logger.info("[RECORD] Recording mode active — fixtures → tests/fixtures/")

    if not os.getenv("ANTHROPIC_API_KEY") and not config.dry_run and not args.mock:
        logger.error(
            "ANTHROPIC_API_KEY not set. "
            "Export it or run with --dry-run."
        )
        sys.exit(1)

    # Determine which stages to run
    if args.stages:
        stages_to_run = [s.strip() for s in args.stages.split(",")]
        invalid = [s for s in stages_to_run if s not in STAGE_MODULES]
        if invalid:
            logger.error(f"Unknown stages: {invalid}")
            sys.exit(1)
    else:
        stages_to_run = STAGE_ORDER

    # Run S1 to get community list if --all
    if args.run_all:
        s1_result = s1_discovery.run(
            community_id="ALL",
            state=config.state,
            config=config
        )
        if not s1_result.ok:
            logger.error(f"S1 discovery failed: {s1_result.errors}")
            sys.exit(1)
        communities = [
            c["community_id"]
            for c in s1_result.output_data.get("communities", [])
        ]

        # Filter communities excluded in states.yaml
        try:
            with open("config/states.yaml") as _f:
                states_cfg = yaml.safe_load(_f)
            excluded = set(
                states_cfg.get(config.state, {}).get("excluded_communities", []) or []
            )
            filtered = []
            for c in communities:
                if c in excluded:
                    logger.info("Skipping %s: excluded in states.yaml", c)
                else:
                    filtered.append(c)
            communities = filtered
        except Exception as exc_err:
            logger.warning("Could not load states.yaml exclusions: %s", exc_err)

    elif config.communities:
        communities = config.communities
    else:
        logger.error("Specify --community or --all")
        sys.exit(1)

    # ── Token logger initialisation ─────────────────────────────────────────
    run_id = (
        f"{config.state.lower()}_{config.preset.value}_"
        f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    token_logger.set_run_id(run_id)

    # ── Batch mode setup ─────────────────────────────────────────────────────
    # Active only when: --batch flag, not --mock, not --dry-run.
    # --mock takes priority (mock already patched in; batch gateway not needed).
    # --dry-run never makes API calls, so no gateway needed (pricing reflected
    # in the cost estimate below via use_batch_pricing).
    use_batch = (
        args.batch
        and not args.mock
        and not config.dry_run
    )
    _gateway = None
    if use_batch:
        import concurrent.futures
        import pipeline.utils.api_client as _api_module
        from pipeline.utils.batch_runner import BatchGateway, MAX_WORKERS
        from tests.mock_anthropic import patch_call_claude as _patch
        _gateway = BatchGateway(original_call_claude=_api_module.call_claude)
        _patch(_gateway)
        _gateway.start()
        logger.info(
            "[BATCH] Batch mode active — %d communities will run in parallel threads",
            len(communities),
        )

    logger.info(
        f"Running pipeline for {len(communities)} communities in {config.state} "
        f"| preset={config.preset.value} | mode={config.mode.value} "
        f"| stages={stages_to_run} | run_id={run_id}"
    )

    # ── Run pipeline per community ───────────────────────────────────────────
    all_results = {}
    if use_batch:
        # Parallel execution — BatchGateway collects all call_claude calls
        # across threads and submits them as Batch API requests.
        max_workers = min(len(communities), MAX_WORKERS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    run_community_pipeline,
                    community_id=comm_id,
                    state=config.state,
                    config=config,
                    stages_to_run=stages_to_run,
                ): comm_id
                for comm_id in communities
            }
            for future in concurrent.futures.as_completed(future_map):
                comm_id = future_map[future]
                try:
                    all_results[comm_id] = future.result()
                except Exception as exc:
                    logger.error("[BATCH] Community %s raised exception: %s", comm_id, exc)
                    all_results[comm_id] = {}
        _gateway.stop()
    else:
        # Serial execution — original behaviour, unmodified
        for community_id in communities:
            if _mock_client is not None:
                _mock_client.reset()
            if _recorder is not None:
                _recorder.reset()
            all_results[community_id] = run_community_pipeline(
                community_id=community_id,
                state=config.state,
                config=config,
                stages_to_run=stages_to_run,
            )
            if _recorder is not None:
                logger.info(_recorder.summary(community_id))

    # Summary
    successes = sum(
        1 for cr in all_results.values()
        if all(r.ok for r in cr.values())
    )
    failures = len(all_results) - successes

    logger.info(
        f"\n{'='*60}\n"
        f"PIPELINE COMPLETE\n"
        f"  Communities: {len(all_results)} total | {successes} succeeded | {failures} failed\n"
        f"{'='*60}"
    )

    # ── Dry-run cost estimate ─────────────────────────────────────────────────
    if config.dry_run:
        _print_dry_run_cost_estimate(
            stages_to_run, communities, config,
            use_batch_pricing=args.batch,
        )

    # ── Scan mode: render comparison table from all city results ────────────
    if config.mode == OutputMode.SCAN:
        scan_results = []
        for comm_id, community_results in all_results.items():
            s7 = community_results.get("s7_render")
            if s7 and s7.ok and s7.output_data:
                scan_results.append(s7.output_data)
        if scan_results:
            s7_render.render_scan_table(scan_results, config)
        else:
            logger.warning("Scan mode: no scan results collected — comparison table skipped")

    # ── Token usage summary + CSV export ────────────────────────────────────
    if not config.dry_run:
        token_logger.finalize()
        if config.mode == OutputMode.SCAN:
            token_logger.print_scan_summary(len(communities))

    if failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
