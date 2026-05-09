"""token-logger — Hermes plugin for per-call token and cost logging.

Logs every API call to ~/.hermes/token_logs/YYYY-MM-DD.csv.gz with full
DeepSeek cache_hit/miss breakdown and cost estimates.

Hooks registered:
  post_api_request  — fires after every API call; logs one CSV row

Tools registered:
  token_summary     — print a usage summary table for the last N days
                      (also available as /token-summary slash command)

Enable:  hermes plugins enable token-logger
Disable: hermes plugins disable token-logger
View:    hermes tools -> Token Logger / Token Summary
"""

from __future__ import annotations

import logging
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


def handle_token_summary_command(raw_args: str = "") -> str | None:
    """
    Slash command handler for /token-summary. Parses optional ``days`` arg.
    Signature matches register_command expectations: ``fn(raw_args: str) -> str``.
    """
    days = 7
    raw = raw_args.strip()
    if raw:
        try:
            days = int(raw.split()[0])
        except (ValueError, IndexError):
            return f"Invalid argument: {raw!r}. Usage: /token-summary [1-30]"
    return handle_token_summary(days=days)


# ---------------------------------------------------------------------------
# Cost supplementation from pricing-tools plugin
# ---------------------------------------------------------------------------

_PKG_LOADED = False

def _load_pricing_module():
    """Lazy-load the pricing-tools plugin's pricing_module, returning
    get_pricing_entry callable or None."""
    global _PKG_LOADED
    try:
        import sys, types, importlib.util
        from pathlib import Path

        if not _PKG_LOADED:
            # Build the path: repo root -> plugins/pricing-tools/
            plugins_dir = Path(__file__).resolve().parent.parent
            pt_dir = str(plugins_dir / "pricing-tools")

            pkg = types.ModuleType("plugins.pricing_tools")
            pkg.__path__ = [pt_dir]
            sys.modules["plugins.pricing_tools"] = pkg

            spec = importlib.util.spec_from_file_location(
                "plugins.pricing_tools.pricing_module",
                pt_dir + "/pricing_module.py",
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["plugins.pricing_tools.pricing_module"] = mod
            spec.loader.exec_module(mod)
            _PKG_LOADED = True

        from plugins.pricing_tools.pricing_module import get_pricing_entry
        return get_pricing_entry
    except Exception:
        return None


def _supplement_cost_from_pricing_plugin(
    provider: str, model_raw: str, u: dict
) -> tuple:
    """Try to fill in missing cost data from the pricing-tools plugin cache.

    Returns (cost_usd, cost_source) or (None, None) on failure.
    Only supplements when usage_pricing.py returned unknown/missing cost.
    """
    try:
        import decimal
        from decimal import Decimal

        get_pricing_entry = _load_pricing_module()
        if get_pricing_entry is None:
            return None, None

        # Normalise model name: strip provider prefix if present
        model = model_raw
        if "/" in model and model.split("/")[0].lower() == provider.lower():
            model = model.split("/", 1)[1]

        entry = get_pricing_entry(provider, model)
        if not entry or entry["input_cost"] is None:
            return None, None

        inp = int(u.get("input_tokens", 0))
        out = int(u.get("output_tokens", 0))
        cache_hit = int(u.get("cache_read_tokens", 0))

        cost: Decimal = Decimal("0")
        if inp and entry["input_cost"] is not None:
            cost += Decimal(inp) * entry["input_cost"] / Decimal("1000000")
        if out and entry["output_cost"] is not None:
            cost += Decimal(out) * entry["output_cost"] / Decimal("1000000")
        if cache_hit and entry["cache_read_cost"] is not None:
            # Recalculate cache-hit row at cache rate instead of input rate
            cost += Decimal(cache_hit) * (
                entry["cache_read_cost"] - (entry["input_cost"] or Decimal("0"))
            ) / Decimal("1000000")

        if cost <= Decimal("0") and inp + out == 0:
            return None, None

        promo = entry.get("promo", "")
        label = "pricing-tools"
        if promo:
            label += f" ({promo})"
        return float(round(cost, 6)), label
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# post_api_request hook
# ---------------------------------------------------------------------------

def _on_api_request(
    task_id: str = "",
    session_id: str = "",
    platform: str = "",
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

        # Base cost from usage_pricing.py
        cost_usd = float(u.get("cost_usd", 0.0))
        cost_status = u.get("cost_status", "")
        cost_source = u.get("cost_source", "")

        # Supplement with pricing-tools cache if cost is unknown/missing
        if (cost_status == "unknown" or cost_usd == 0.0) and provider:
            model_raw = (
                u.get("model_raw", "")
                or response_model
                or ""
            )
            supp_cost, supp_source = _supplement_cost_from_pricing_plugin(
                provider.lower(), model_raw, u
            )
            if supp_cost is not None and supp_cost > 0:
                cost_usd = supp_cost
                cost_source = supp_source or cost_source

        logger_t.log(
            session_id=session_id or None,
            provider=provider or None,
            model=model_raw or response_model or "",
            api_call_n=api_call_count or 0,
            # Input token breakdown
            input_tokens=int(u.get("input_tokens", 0)),
            cache_hit_tokens=int(u.get("cache_read_tokens", 0)),
            cache_miss_tokens=0,  # DeepSeek miss = input_tokens - cache_read (already in input_tokens)
            cache_write_tokens=int(u.get("cache_write_tokens", 0)),
            # Output tokens
            output_tokens=int(u.get("output_tokens", 0)),
            reasoning_tokens=int(u.get("reasoning_tokens", 0)),
            total_tokens=int(u.get("total_tokens", 0)),
            # Cost
            cost_usd=cost_usd,
            cost_status=cost_status if cost_status else "supplemented",
            cost_source=cost_source,
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
      - post_api_request hook  -> logs each API call to CSV
      - token_summary tool     -> user-facing usage summary
    """
    ctx.register_hook("post_api_request", _on_api_request)
    ctx.register_tool(
        name="token_summary",
        toolset="token-logger",
        schema=TOOL_SCHEMA,
        handler=handle_token_summary,
        emoji="\U0001f4ca",
        description=TOOL_SCHEMA["description"],
    )
    ctx.register_command(
        name="token-summary",
        handler=handle_token_summary_command,
        description="Print token-usage summary from CSV logs (input, cache-hit rate, output, cost).",
        args_hint="[days: 1-30, default 7]",
    )
    logger.info("token-logger plugin loaded -- logs -> ~/.hermes/token_logs/")