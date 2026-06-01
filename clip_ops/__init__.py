"""
clip_ops — deterministic, filesystem-based operations layer for CLIP.

Standalone. Consumes CLIP outputs/logs/docs read-only and emits a daily
system-state digest. Contains NO LLM or AI calls and never imports or
mutates CLIP pipeline code.
"""

__all__ = [
    "config",
    "collectors",
    "state",
    "backlog",
    "digest",
    "send_email",
    "run_daily",
]
