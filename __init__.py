"""
token-logger — Hermes plugin for per-call token and cost logging.

Logs every API call to ~/.hermes/token_logs/YYYY-MM-DD.csv.gz with full
DeepSeek cache_hit/miss breakdown and cost estimates.

Hooks registered:
  post_api_request  — fires after every API call; logs one CSV row

Tools registered:
  token_summary     — print a usage summary table for the last N days
                      (also available as `/token-summary` slash command)

Enable:  hermes plugins enable token-logger
Disable: hermes plugins disable token-logger
View:    hermes tools → Token Logger / Token Summary
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

# token_logger.py is a sibling module.  When Hermes loads this plugin it
# creates a synthetic package hermes_plugins.token_logger so relative imports
# work.  When imported standalone for testing we fall back to a bare import.
try:
    from .token_logger import TokenLogger, get_token_logger, summarize_logs
except ImportError:  # pragma: no cover — standalone test path
    from token_logger import TokenLogger, get_token_logger, summarize_logs  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TOOL_SCHEMA = {
    "name": "token_summary",
    "description": (
        "Print a token-usage summary from the CSV logs written by the "
        "token-logger plugin.  Shows per-model and per-day breakdowns of "
        "input tokens, cache-hit rate, output tokens, and estimated cost.  "
        "Logs are stored in ~/.hermes/token_logs/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of past days to include (default: 7, max: 30).",
                "minimum": 1,
                "maximum": 30,
                "default": 7,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle_token_summary(
    days: int = 7,
    **_kwargs,
) -> str:
    """
    Tool handler for token_summary.  Returns a plain-text table.
    """
    days = min(max(int(days), 1), 30)
    return summarize_logs(days=days)


# ---------------------------------------------------------------------------
# post_api_request hook
# ---------------------------------------------------------------------------

def _on_api_request(
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    api_duration: float = 0.0,
    finish_reason: str = "",
    message_count: int = 0,
    response_model: str = "",
    usage: dict | None = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    **kwargs,
) -> None:
    """
    post_api_request hook.  Writes one CSV row per API call.

    ``usage`` is the dict produced by AIAgent._usage_summary_for_api_request_hook()
    and contains CanonicalUsage fields (prompt_tokens, total_tokens, etc.)
    plus DeepSeek-specific cache_hit/miss tokens when available.
    """
    try:
        u = usage or {}
        logger_t = get_token_logger()
        logger_t.log(
            session_id=session_id or None,
            provider=provider or None,
            model=model or response_model or "",
            api_call_n=api_call_count or 0,
            # Input token breakdown
            input_tokens=int(u.get("prompt_tokens", 0)),
            cache_hit_tokens=int(u.get("prompt_cache_hit_tokens", 0)),
            cache_miss_tokens=int(u.get("prompt_cache_miss_tokens", 0)),
            cache_write_tokens=int(u.get("prompt_cache_write_tokens", 0)),
            # Output tokens
            output_tokens=int(u.get("completion_tokens", 0)),
            reasoning_tokens=int(u.get("reasoning_tokens", 0)),
            total_tokens=int(u.get("total_tokens", 0)),
            # Cost (may come back as 0 if provider not recognised yet)
            cost_usd=float(u.get("cost_usd", 0.0)),
            cost_status=u.get("cost_status", ""),
            cost_source=u.get("cost_source", ""),
            latency_s=api_duration,
        )
    except Exception as exc:
        # Fail-open: never let a logging error affect the agent
        logger.debug("token-logger hook error (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """
    Called once by the plugin loader when the plugin is enabled.

    Registers:
      - post_api_request hook  → logs each API call to CSV
      - token_summary tool     → user-facing usage summary
    """
    ctx.register_hook("post_api_request", _on_api_request)
    ctx.register_tool(
        name="token_summary",
        toolset="token-logger",
        schema=TOOL_SCHEMA,
        handler=handle_token_summary,
        emoji="📊",
        description=TOOL_SCHEMA["description"],
    )
    logger.info("token-logger plugin loaded — logs → ~/.hermes/token_logs/")