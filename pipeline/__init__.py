"""
pipeline/__init__.py
Shared types, interfaces, and base classes for the charter intelligence pipeline.
Every stage imports from here. Never import stage modules from other stage modules.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional
import datetime
import unicodedata


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class Confidence(str, Enum):
    HIGH     = "HIGH"
    MODERATE = "MODERATE"
    LOW      = "LOW"
    NONE     = "NONE"

    @classmethod
    def minimum(cls, levels: list["Confidence"]) -> "Confidence":
        """Return the lowest confidence level in a list."""
        order = [cls.HIGH, cls.MODERATE, cls.LOW, cls.NONE]
        for level in reversed(order):
            if level in levels:
                return level
        return cls.NONE


class VerificationStatus(str, Enum):
    VERIFIED    = "VERIFIED"
    PROVISIONAL = "PROVISIONAL"
    DISPUTED    = "DISPUTED"
    UNVERIFIED  = "UNVERIFIED"
    NOT_FOUND   = "NOT_FOUND"


class SourceClass(str, Enum):
    STATUTE        = "STATUTE"
    PED_DATA       = "PED_DATA"
    FEDERAL_DATA   = "FEDERAL_DATA"
    AUTHORIZER_DOC = "AUTHORIZER_DOC"
    PRIMARY_GOVT   = "PRIMARY_GOVT"
    THINK_TANK     = "THINK_TANK"
    ADVOCACY       = "ADVOCACY"
    MEDIA          = "MEDIA"
    SELF_REPORTED  = "SELF_REPORTED"
    UNVERIFIED     = "UNVERIFIED"


class OutputMode(int, Enum):
    SCAN           = 1
    STRATEGIC_BRIEF = 2
    DEEP_DIVE      = 3


class OperatorPreset(str, Enum):
    GROWTH             = "growth"
    REPLICATION        = "replication"
    TURNAROUND         = "turnaround"
    MATURITY_ADJUSTED  = "maturity_adjusted"


class StageStatus(str, Enum):
    SUCCESS = "success"
    ERROR   = "error"
    PARTIAL = "partial"
    SKIPPED = "skipped"


# ─────────────────────────────────────────────
# STAGE RESULT — every stage returns this
# ─────────────────────────────────────────────

@dataclass
class StageResult:
    stage_id: str
    community_id: str
    state: str
    status: StageStatus
    output_path: Optional[str] = None   # path to JSON output written to disk
    output_data: Optional[dict] = None  # in-memory output (for pipeline chaining)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tokens_used: int = 0
    duration_seconds: float = 0.0
    cache_hit: bool = False
    data_through: Optional[str] = None  # ISO date string of most recent data used

    @property
    def ok(self) -> bool:
        return self.status in (StageStatus.SUCCESS, StageStatus.PARTIAL)


# ─────────────────────────────────────────────
# PIPELINE CONFIG — loaded from pipeline.yaml
# ─────────────────────────────────────────────

@dataclass
class PipelineConfig:
    state: str
    preset: OperatorPreset
    mode: OutputMode
    output_format: str           # "markdown" | "pdf"
    cache_enabled: bool
    dry_run: bool                # If True, run validation only; don't call APIs
    force_refresh: bool          # Ignore cache; regenerate everything
    communities: Optional[list[str]] = None  # None = all communities in state
    depth: str = "standard"            # fast | standard | deep

    # Model strings (loaded from pipeline.yaml)
    model_opus: str   = "claude-opus-4-5"
    model_sonnet: str = "claude-sonnet-4-5"
    model_haiku: str  = "claude-haiku-4-5-20251001"


# ─────────────────────────────────────────────
# VALIDATION RESULT
# ─────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.valid


# ─────────────────────────────────────────────
# STAGE PROTOCOL
# ─────────────────────────────────────────────
# Not a formal ABC — implement these methods in each stage module.
# Each stage module exposes a `run()` function matching this signature.

def stage_run_signature(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs: Any
) -> StageResult:
    """
    Expected signature for every stage's `run()` function.
    Not called directly — used as documentation contract.

    Args:
        community_id: Stable community identifier (e.g., "nm-albuquerque")
        state:        Two-letter state code
        config:       Pipeline configuration
        previous_result: Output from the immediately preceding stage (for chaining)
        **kwargs:     Stage-specific overrides

    Returns:
        StageResult with status, output path/data, and metadata
    """
    raise NotImplementedError


# ─────────────────────────────────────────────
# PIPELINE RUNNER
# ─────────────────────────────────────────────

STAGE_ORDER = [
    "s1_discovery",
    "s2_state_context",
    "s3_fact_extraction",
    "s4_verification",
    "s5_scoring",
    "s6_synthesis",
    "s7_render"
]


def build_community_id(city_slug: str, state: str) -> str:
    """Construct a canonical community_id from city name and state code.

    Handles accented characters (e.g. Española → nm-espanola) via NFKD
    Unicode normalization before ASCII conversion.
    """
    normalized = unicodedata.normalize("NFKD", city_slug)
    ascii_slug = normalized.encode("ascii", "ignore").decode("ascii")
    slug = ascii_slug.lower().replace(" ", "-").replace(",", "")
    return f"{state.lower()}-{slug}"


def timestamp_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.utcnow().isoformat() + "Z"


def today_str() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.date.today().isoformat()
