"""
Per-call token logger for Hermes Agent.

Logs every API call to a rotating CSV file with full token breakdowns including
DeepSeek's cache_hit / cache_miss breakdown.

Output file: ~/.hermes/token_logs/YYYY-MM-DD.csv.gz

CSV columns:
  timestamp, session_id, provider, model, api_call_n,
  input_tokens, cache_hit_tokens, cache_miss_tokens, cache_write_tokens,
  output_tokens, reasoning_tokens, total_tokens,
  cost_usd, cost_status, cost_source, cache_hit_rate_pct, latency_s

Usage:
  from plugins.token_logger.token_logger import TokenLogger
  logger = TokenLogger()
  logger.log(session_id="...", model="deepseek/deepseek-v4-pro", ...)
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERMES_DIR = Path.home() / ".hermes"
_TOKEN_LOG_DIR = _HERMES_DIR / "token_logs"
_TOKEN_LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# TokenLogger
# ---------------------------------------------------------------------------


class TokenLogger:
    """
    Appends one CSV row per API call.  File rotates daily (UTC midnight).

    The logger is intentionally simple — no background threads, no external
    dependencies.  Write happens synchronously after each API call.
    """

    CSV_HEADERS = [
        "timestamp",
        "session_id",
        "provider",
        "model",
        "api_call_n",
        # Input breakdown
        "input_tokens",
        "cache_hit_tokens",  # DeepSeek: prompt_cache_hit_tokens
        "cache_miss_tokens",  # DeepSeek: prompt_cache_miss_tokens
        "cache_write_tokens",
        # Output
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        # Cost
        "cost_usd",
        "cost_status",
        "cost_source",
        # Derived
        "cache_hit_rate_pct",
        "latency_s",
    ]

    def __init__(self, log_dir: Path = _TOKEN_LOG_DIR) -> None:
        self._log_dir = log_dir
        self._current_date: Optional[str] = None
        self._fh: Optional[gzip.GzipFile] = None
        self._writer: Optional[csv.DictWriter] = None

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _date_tag(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _filepath(self, date_tag: str) -> Path:
        return self._log_dir / f"{date_tag}.csv.gz"

    def _ensure_open(self) -> None:
        date = self._date_tag()
        if date != self._current_date:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
                self._writer = None
            self._current_date = date

            path = self._filepath(date)
            is_new = not path.exists()

            # Gzip doesn't support text-mode "a".  We open in binary append
            # and wrap with TextIOWrapper to get a text-mode file handle.
            self._fh = gzip.open(path, mode="ab", compresslevel=6)
            self._writer = csv.DictWriter(
                io.TextIOWrapper(self._fh, encoding="utf-8", write_through=True),
                fieldnames=self.CSV_HEADERS,
            )
            if is_new:
                self._writer.writeheader()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def log(
        self,
        *,
        session_id: Optional[str],
        provider: Optional[str],
        model: str,
        api_call_n: int,
        input_tokens: int,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        total_tokens: int,
        cost_usd: float,
        cost_status: str,
        cost_source: str,
        latency_s: float,
    ) -> None:
        """
        Record one API call.

        Args:
            session_id:     Current session ID (may be None for stateless calls)
            provider:       Billing provider e.g. "deepseek", "openrouter"
            model:          Full model string e.g. "deepseek/deepseek-v4-pro"
            api_call_n:     Call sequence number within this session (1-based)
            ...: token breakdowns from the post_api_request hook usage dict
            latency_s:      Round-trip latency in seconds
        """
        self._ensure_open()
        if self._writer is None:
            return

        # Cache hit rate
        total_input = input_tokens + cache_hit_tokens + cache_write_tokens
        if total_input > 0:
            cache_hit_rate = round(100 * cache_hit_tokens / total_input, 1)
        else:
            cache_hit_rate = 0.0

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id or "",
            "provider": provider or "",
            "model": model,
            "api_call_n": api_call_n,
            "input_tokens": input_tokens,
            "cache_hit_tokens": cache_hit_tokens,
            "cache_miss_tokens": cache_miss_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(cost_usd, 6),
            "cost_status": cost_status,
            "cost_source": cost_source,
            "cache_hit_rate_pct": cache_hit_rate,
            "latency_s": round(latency_s, 3),
        }

        self._writer.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_token_logger: Optional[TokenLogger] = None


def get_token_logger() -> TokenLogger:
    global _token_logger
    if _token_logger is None:
        _token_logger = TokenLogger()
    return _token_logger


# ---------------------------------------------------------------------------
# CLI summary
# ---------------------------------------------------------------------------

def summarize_logs(days: int = 7) -> str:
    """
    Build a terminal summary string for recent token usage.

    Returns a multi-line string.  Callers decide how to display it.
    """
    cutoff = time.time() - days * 86400
    rows: list[dict] = []

    for gz_path in sorted(_TOKEN_LOG_DIR.glob("*.csv.gz")):
        try:
            with gzip.open(gz_path, mode="rb") as raw:
                fh = io.TextIOWrapper(raw, encoding="utf-8")
                for row in csv.DictReader(fh):
                    try:
                        ts = datetime.fromisoformat(row["timestamp"]).timestamp()
                        if ts < cutoff:
                            continue
                    except Exception:
                        continue
                    rows.append(row)
        except Exception:
            continue

    if not rows:
        return f"No token log data in the last {days} days."

    total_cost = sum(float(r["cost_usd"]) for r in rows if r["cost_usd"])
    total_input = sum(int(r["input_tokens"]) for r in rows)
    total_cache_hit = sum(int(r["cache_hit_tokens"]) for r in rows)
    total_output = sum(int(r["output_tokens"]) for r in rows)
    total_calls = len(rows)
    sessions = set(r["session_id"] for r in rows if r["session_id"])
    providers = set(r["provider"] for r in rows if r["provider"])

    # Per-model breakdown
    by_model: dict[str, dict] = {}
    for r in rows:
        m = r["model"] or "unknown"
        if m not in by_model:
            by_model[m] = {"input": 0, "cache_hit": 0, "output": 0, "cost": 0.0, "calls": 0}
        by_model[m]["input"] += int(r["input_tokens"])
        by_model[m]["cache_hit"] += int(r["cache_hit_tokens"])
        by_model[m]["output"] += int(r["output_tokens"])
        by_model[m]["cost"] += float(r["cost_usd"]) if r["cost_usd"] else 0.0
        by_model[m]["calls"] += 1

    # Per-day breakdown
    by_day: dict[str, dict] = {}
    for r in rows:
        try:
            day = r["timestamp"][:10]
        except Exception:
            continue
        if day not in by_day:
            by_day[day] = {"input": 0, "cache_hit": 0, "output": 0, "cost": 0.0, "calls": 0}
        by_day[day]["input"] += int(r["input_tokens"])
        by_day[day]["cache_hit"] += int(r["cache_hit_tokens"])
        by_day[day]["output"] += int(r["output_tokens"])
        by_day[day]["cost"] += float(r["cost_usd"]) if r["cost_usd"] else 0.0
        by_day[day]["calls"] += 1

    lines = [
        "",
        "token-logger summary",
        f"  period: last {days} days | {len(rows)} calls | {len(sessions)} sessions",
        "",
        f"  {'PROVIDER BREAKDOWN':}",
        *[f"  {p}" for p in sorted(providers)],
        "",
        f"  {'TOKEN TOTALS':30s}  {'CALLS':>6}  {'INPUT':>10}  {'CACHE_HIT':>10}  {'OUTPUT':>10}  {'COST':>10}",
        f"  {'─'*68}",
    ]
    for model, d in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        display = model.split("/")[-1] if "/" in model else model
        hit_pct = 100 * d["cache_hit"] / max(d["input"], 1)
        lines.append(
            f"  {display:30s} "
            f"{d['calls']:>6} "
            f"{d['input']:>10,} "
            f"{hit_pct:>9.1f}% "
            f"{d['output']:>10,} "
            f"${d['cost']:>9.4f}"
        )

    lines += [
        f"  {'─'*68}",
        f"  {'TOTAL':30s} {total_calls:>6} {total_input:>10,} "
        f"{100*total_cache_hit/max(total_input,1):>9.1f}% {total_output:>10,} ${total_cost:>9.4f}",
        "",
        f"  {'DAILY BREAKDOWN':30s}  {'DATE':>12}  {'CALLS':>6}  {'INPUT':>10}  {'COST':>10}",
        f"  {'─'*68}",
    ]
    for day, d in sorted(by_day.items(), reverse=True)[:14]:
        lines.append(
            f"  {'':30s} {day:>12} {d['calls']:>6} "
            f"{d['input']:>10,} ${d['cost']:>9.4f}"
        )

    lines += ["", f"  Logs: ~/.hermes/token_logs/", ""]
    return "\n".join(lines)