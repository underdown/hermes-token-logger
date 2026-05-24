"""
Per-call token logger for Hermes Agent.

Logs every API call to a rotating plain-text CSV file AND a SQLite database
for crash-safe durability and fast querying.

Output files: ~/.hermes/token_logs/YYYY-MM-DD.csv  (uncompressed, append-safe)
Database:      ~/.hermes/token_logs/token_logs.db   (SQLite, indexed)

CSV / DB columns:
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
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERMES_DIR = Path.home() / ".hermes"
_TOKEN_LOG_DIR = _HERMES_DIR / "token_logs"
_TOKEN_LOG_DIR.mkdir(parents=True, exist_ok=True)
_TOKEN_DB_PATH = _TOKEN_LOG_DIR / "token_logs.db"

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    model TEXT NOT NULL,
    api_call_n INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    cost_status TEXT DEFAULT '',
    cost_source TEXT DEFAULT '',
    cache_hit_rate_pct REAL DEFAULT 0.0,
    latency_s REAL DEFAULT 0.0
)
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_api_calls_timestamp ON api_calls(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_calls(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_api_calls_provider ON api_calls(provider)",
    "CREATE INDEX IF NOT EXISTS idx_api_calls_model ON api_calls(model)",
    "CREATE INDEX IF NOT EXISTS idx_api_calls_date ON api_calls(date(timestamp))",
]

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

    def __init__(self, log_dir: Path = _TOKEN_LOG_DIR, db_path: Path = _TOKEN_DB_PATH) -> None:
        self._log_dir = log_dir
        self._db_path = db_path
        self._current_date: Optional[str] = None
        self._fh: Optional[io.TextIOWrapper] = None
        self._writer: Optional[csv.DictWriter] = None
        self._db: Optional[sqlite3.Connection] = None
        self._ensure_db()

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _date_tag(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _filepath(self, date_tag: str) -> Path:
        return self._log_dir / f"{date_tag}.csv"

    def _ensure_db(self) -> None:
        """Initialize SQLite database, creating table and indexes if needed."""
        self._db = sqlite3.connect(str(self._db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(_CREATE_TABLE_SQL)
        for idx_sql in _CREATE_INDEXES_SQL:
            self._db.execute(idx_sql)
        self._db.commit()

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

            # Plain-text CSV append — crash-safe, no gzip corruption risk.
            self._fh = io.open(path, mode="a", encoding="utf-8")
            self._writer = csv.DictWriter(
                self._fh,
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

        # Dual-write to SQLite for fast queries
        if self._db is not None:
            try:
                self._db.execute(
                    """INSERT INTO api_calls (
                        timestamp, session_id, provider, model, api_call_n,
                        input_tokens, cache_hit_tokens, cache_miss_tokens, cache_write_tokens,
                        output_tokens, reasoning_tokens, total_tokens,
                        cost_usd, cost_status, cost_source, cache_hit_rate_pct, latency_s
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["timestamp"], row["session_id"], row["provider"], row["model"], row["api_call_n"],
                        row["input_tokens"], row["cache_hit_tokens"], row["cache_miss_tokens"], row["cache_write_tokens"],
                        row["output_tokens"], row["reasoning_tokens"], row["total_tokens"],
                        row["cost_usd"], row["cost_status"], row["cost_source"],
                        row["cache_hit_rate_pct"], row["latency_s"],
                    ),
                )
                self._db.commit()
            except Exception:
                pass  # fail-open: don't let DB errors affect the agent

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        if self._db is not None:
            self._db.close()
            self._db = None

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
    """Build a terminal summary string for recent token usage.

    Uses SQLite when available (fast, arbitrary date ranges); falls back to
    scanning uncompressed CSV files for backward compatibility.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff_str = cutoff.isoformat(timespec="seconds")

    # Try SQLite first
    if _TOKEN_DB_PATH.exists():
        try:
            return _summarize_from_sqlite(days, cutoff_str)
        except Exception:
            pass  # fall through to CSV scan

    # Fallback: scan uncompressed CSV and gzip CSV files
    return _summarize_from_csv_files(days)


def _summarize_from_sqlite(days: int, cutoff: str) -> str:
    """Query SQLite for summary — fast, no file scanning."""
    db = sqlite3.connect(str(_TOKEN_DB_PATH))
    db.row_factory = sqlite3.Row
    try:
        # Row count / session count / total aggregates
        summary = db.execute(
            """SELECT COUNT(*) as calls, COUNT(DISTINCT session_id) as sessions,
                      COALESCE(SUM(input_tokens),0) as total_input,
                      COALESCE(SUM(cache_hit_tokens),0) as total_cache_hit,
                      COALESCE(SUM(output_tokens),0) as total_output,
                      COALESCE(SUM(cost_usd),0) as total_cost
               FROM api_calls WHERE timestamp >= ?""",
            (cutoff,),
        ).fetchone()

        if not summary or summary["calls"] == 0:
            return f"No token log data in the last {days} days."

        # Per-model breakdown
        model_rows = db.execute(
            """SELECT model,
                      COUNT(*) as calls,
                      COALESCE(SUM(input_tokens),0) as input_t,
                      COALESCE(SUM(cache_hit_tokens),0) as cache_hit,
                      COALESCE(SUM(output_tokens),0) as output_t,
                      COALESCE(SUM(cost_usd),0) as cost
               FROM api_calls WHERE timestamp >= ?
               GROUP BY model ORDER BY cost DESC""",
            (cutoff,),
        ).fetchall()

        # Per-day breakdown (last 14 days)
        day_rows = db.execute(
            """SELECT date(timestamp) as day,
                      COUNT(*) as calls,
                      COALESCE(SUM(input_tokens),0) as input_t,
                      COALESCE(SUM(cost_usd),0) as cost
               FROM api_calls WHERE timestamp >= ?
               GROUP BY day ORDER BY day DESC LIMIT 14""",
            (cutoff,),
        ).fetchall()

        # Provider list
        providers = db.execute(
            "SELECT DISTINCT provider FROM api_calls WHERE timestamp >= ? AND provider != '' ORDER BY provider",
            (cutoff,),
        ).fetchall()

        lines = [
            "",
            "token-logger summary",
            f"  period: last {days} days | {summary['calls']} calls | {summary['sessions']} sessions",
            "",
            f"  {'PROVIDER BREAKDOWN':}",
        ]
        lines.extend(f"  {p['provider']}" for p in providers)
        lines += [
            "",
            f"  {'TOKEN TOTALS':30s}  {'CALLS':>6}  {'INPUT':>10}  {'CACHE_HIT':>10}  {'OUTPUT':>10}  {'COST':>10}",
            f"  {'─'*68}",
        ]
        for r in model_rows:
            display = r["model"].split("/")[-1] if "/" in r["model"] else r["model"]
            total_in = r["input_t"] + r["cache_hit"]
            hit_pct = 100 * r["cache_hit"] / max(total_in, 1)
            lines.append(
                f"  {display:30s} "
                f"{r['calls']:>6} "
                f"{r['input_t']:>10,} "
                f"{hit_pct:>9.1f}% "
                f"{r['output_t']:>10,} "
                f"${r['cost']:>9.4f}"
            )
        total_input_for_rate = summary["total_input"] + summary["total_cache_hit"]
        lines += [
            f"  {'─'*68}",
            f"  {'TOTAL':30s} {summary['calls']:>6} {summary['total_input']:>10,} "
            f"{100*summary['total_cache_hit']/max(total_input_for_rate,1):>9.1f}% "
            f"{summary['total_output']:>10,} ${summary['total_cost']:>9.4f}",
            "",
            f"  {'DAILY BREAKDOWN':30s}  {'DATE':>12}  {'CALLS':>6}  {'INPUT':>10}  {'COST':>10}",
            f"  {'─'*68}",
        ]
        for d in day_rows:
            lines.append(
                f"  {'':30s} {d['day']:>12} {d['calls']:>6} "
                f"{d['input_t']:>10,} ${d['cost']:>9.4f}"
            )
        lines += ["", f"  Logs: ~/.hermes/token_logs/", f"  Database: ~/.hermes/token_logs/token_logs.db", ""]
        return "\n".join(lines)
    finally:
        db.close()


def _summarize_from_csv_files(days: int) -> str:
    """Fallback: scan CSV and legacy gzip CSV files for the summary."""
    cutoff = time.time() - days * 86400
    rows: list[dict] = []

    # Scan uncompressed CSVs first, then legacy .csv.gz files
    for pattern in ["*.csv", "*.csv.gz"]:
        for path in sorted(_TOKEN_LOG_DIR.glob(pattern)):
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, mode="rt" if path.suffix == ".gz" else "r", encoding="utf-8", errors="replace") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
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

    total_cost = sum(float(r.get("cost_usd", 0) or 0) for r in rows)
    total_input = sum(int(r.get("input_tokens", 0) or 0) for r in rows)
    total_cache_hit = sum(int(r.get("cache_hit_tokens", 0) or 0) for r in rows)
    total_output = sum(int(r.get("output_tokens", 0) or 0) for r in rows)
    total_calls = len(rows)
    sessions = set(r.get("session_id", "") for r in rows if r.get("session_id"))
    providers = set(r.get("provider", "") for r in rows if r.get("provider"))

    # Per-model breakdown
    by_model: dict[str, dict] = {}
    for r in rows:
        m = r.get("model") or "unknown"
        if m not in by_model:
            by_model[m] = {"input": 0, "cache_hit": 0, "output": 0, "cost": 0.0, "calls": 0}
        by_model[m]["input"] += int(r.get("input_tokens", 0) or 0)
        by_model[m]["cache_hit"] += int(r.get("cache_hit_tokens", 0) or 0)
        by_model[m]["output"] += int(r.get("output_tokens", 0) or 0)
        by_model[m]["cost"] += float(r.get("cost_usd", 0) or 0)
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
        by_day[day]["input"] += int(r.get("input_tokens", 0) or 0)
        by_day[day]["cache_hit"] += int(r.get("cache_hit_tokens", 0) or 0)
        by_day[day]["output"] += int(r.get("output_tokens", 0) or 0)
        by_day[day]["cost"] += float(r.get("cost_usd", 0) or 0)
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
        total_in = d["input"] + d["cache_hit"]
        hit_pct = 100 * d["cache_hit"] / max(total_in, 1)
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
        f"{100*total_cache_hit/max(total_input+total_cache_hit,1):>9.1f}% {total_output:>10,} ${total_cost:>9.4f}",
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


# ---------------------------------------------------------------------------
# Migration: import existing CSV/gzip data into SQLite
# ---------------------------------------------------------------------------

def import_existing_logs() -> dict:
    """Import all readable data from existing CSV and gzip-CSV files into SQLite.

    Called once after upgrade to populate the database. Safe to run multiple
    times — existing rows are skipped by timestamp+session_id+api_call_n.

    Returns a dict with counts: {imported: N, skipped: N, files_ok: N, files_corrupt: N}
    """
    db = sqlite3.connect(str(_TOKEN_DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(_CREATE_TABLE_SQL)
    for idx_sql in _CREATE_INDEXES_SQL:
        db.execute(idx_sql)

    imported = 0
    skipped = 0
    files_ok = 0
    files_corrupt = 0

    for pattern in ["*.csv", "*.csv.gz"]:
        for path in sorted(_TOKEN_LOG_DIR.glob(pattern)):
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, mode="rt" if path.suffix == ".gz" else "r",
                           encoding="utf-8", errors="replace") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        # Check for duplicates
                        exists = db.execute(
                            """SELECT 1 FROM api_calls
                               WHERE timestamp = ? AND session_id = ?
                               AND (api_call_n = ? OR (api_call_n = 0 AND ? = 0))
                               LIMIT 1""",
                            (row.get("timestamp", ""), row.get("session_id", ""),
                             int(row.get("api_call_n", 0) or 0), int(row.get("api_call_n", 0) or 0)),
                        ).fetchone()
                        if exists:
                            skipped += 1
                            continue

                        try:
                            db.execute(
                                """INSERT INTO api_calls (
                                    timestamp, session_id, provider, model, api_call_n,
                                    input_tokens, cache_hit_tokens, cache_miss_tokens, cache_write_tokens,
                                    output_tokens, reasoning_tokens, total_tokens,
                                    cost_usd, cost_status, cost_source, cache_hit_rate_pct, latency_s
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    row.get("timestamp", ""),
                                    row.get("session_id", ""),
                                    row.get("provider", ""),
                                    row.get("model", ""),
                                    int(row.get("api_call_n", 0) or 0),
                                    int(row.get("input_tokens", 0) or 0),
                                    int(row.get("cache_hit_tokens", 0) or 0),
                                    int(row.get("cache_miss_tokens", 0) or 0),
                                    int(row.get("cache_write_tokens", 0) or 0),
                                    int(row.get("output_tokens", 0) or 0),
                                    int(row.get("reasoning_tokens", 0) or 0),
                                    int(row.get("total_tokens", 0) or 0),
                                    float(row.get("cost_usd", 0) or 0),
                                    row.get("cost_status", ""),
                                    row.get("cost_source", ""),
                                    float(row.get("cache_hit_rate_pct", 0) or 0),
                                    float(row.get("latency_s", 0) or 0),
                                ),
                            )
                            imported += 1
                        except Exception:
                            skipped += 1
                db.commit()
                files_ok += 1
            except Exception:
                files_corrupt += 1

    db.close()
    return {"imported": imported, "skipped": skipped,
            "files_ok": files_ok, "files_corrupt": files_corrupt}