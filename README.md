# hermes-token-logger

A Hermes Agent plugin that logs every API call to plain-text CSV files AND a SQLite database — with full DeepSeek cache-hit/miss breakdowns, token counts, and cost estimates.

**v2.0.0 — SQLite dual-write.** Crash-safe: no more gzip corruption from process restarts. The database enables sub-millisecond queries across arbitrary date ranges.

---

## Quick start

```bash
# 1. Install the plugin
git clone git@github.com:underdown/hermes-token-logger.git ~/.hermes/plugins/token-logger

# 2. Enable it
hermes plugins enable token-logger

# 3. Restart Hermes (or start a new session)
hermes

# 4. View your usage summary
/token-summary days=7
```

That's it. The plugin activates automatically. No config files, no API keys.

---

## Log file location

```
~/.hermes/token_logs/YYYY-MM-DD.csv         ← today's log (uncompressed, append-safe)
~/.hermes/token_logs/YYYY-MM-DD.csv.gz      ← archived (nightly gzip cron)
~/.hermes/token_logs/token_logs.db           ← SQLite database (fast queries)
```

Each day gets one CSV file. At 1am UTC, yesterday's CSV is gzip-archived. The SQLite database is the primary query interface — `token_summary` reads from it in sub-millisecond time.

Query the database directly:
```bash
sqlite3 ~/.hermes/token_logs/token_logs.db "SELECT model, COUNT(*), SUM(cost_usd) FROM api_calls WHERE timestamp >= date('now','-7 days') GROUP BY model ORDER BY SUM(cost_usd) DESC"
```

View raw CSV:
```bash
cat ~/.hermes/token_logs/2026-05-24.csv | head -5
# or legacy gzip files:
zcat ~/.hermes/token_logs/2026-05-16.csv.gz | head -5
```

Parse with Python:
```python
import sqlite3
db = sqlite3.connect("~/.hermes/token_logs/token_logs.db")
for row in db.execute("SELECT * FROM api_calls WHERE timestamp >= date('now','-7 days')"):
    print(row["model"], row["cache_hit_rate_pct"], row["cost_usd"])
```

---

## Commands & tools

### `/token-summary [days=7]`
Prints a formatted usage table for the last N days.

```
/token-summary days=14
```

Sample output:
```
token-logger summary
  period: last 14 days | 47 calls | 3 sessions

  CALLS  INPUT_TOKENS  CACHE_HIT%  OUTPUT_TOKENS  COST
  ──────────────────────────────────────────────────────
  deepseek-v4-pro     23      1,234,567      78.3%      456,789  $0.0042
  claude-sonnet-4      14        890,123       N/A       234,567  $0.0000
  gpt-4o               10        567,890       N/A       123,456  $0.0000
  ──────────────────────────────────────────────────────
  TOTAL               47      2,692,580      78.3%      814,812  $0.0042

  DATE           CALLS  INPUT_TOKENS  COST
  ──────────────────────────────────────────
  2026-05-08       12      890,123  $0.0018
  2026-05-07       18      923,456  $0.0024
  2026-05-06       17      879,001  $0.0000
```

Also callable from a conversation — the model runs it as a tool and returns the table inline.

---

## CSV schema

Every row = one API call.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | ISO-8601 UTC | When the call completed |
| `session_id` | string | Hermes session ID (empty for subagent calls) |
| `provider` | string | Billing provider: `deepseek`, `openrouter`, `nvidia`, etc. |
| `model` | string | Full model string, e.g. `deepseek-ai/deepseek-v4-pro` |
| `api_call_n` | int | Call number within this session (1-based) |
| `input_tokens` | int | Non-cached prompt tokens |
| `cache_hit_tokens` | int | DeepSeek prompt cache hit tokens |
| `cache_miss_tokens` | int | DeepSeek prompt cache miss tokens |
## Configuration

No configuration required. The plugin uses Hermes's built-in pricing table for cost estimation.

The SQLite database path defaults to `~/.hermes/token_logs/token_logs.db`. To override:

```python
logger = TokenLogger(db_path=Path("/custom/path.db"))
```

The nightly archive script (`token-logger-archive.py`) compresses yesterday's CSV at 1am UTC and vacuums the database. It's installed as a Hermes cron job automatically.

## Shell analysis one-liners

```bash
# Total cost this month (SQLite — instant)
sqlite3 ~/.hermes/token_logs/token_logs.db \
  "SELECT provider, SUM(cost_usd) FROM api_calls WHERE timestamp >= date('now','start of month') GROUP BY provider"

# Top 5 costliest sessions
sqlite3 ~/.hermes/token_logs/token_logs.db \
  "SELECT session_id, COUNT(*) as calls, SUM(cost_usd) as cost FROM api_calls GROUP BY session_id ORDER BY cost DESC LIMIT 5"

# Cache hit rate by day
sqlite3 -column -header ~/.hermes/token_logs/token_logs.db \
  "SELECT date(timestamp) as day, ROUND(100.0*SUM(cache_hit_tokens)/NULLIF(SUM(input_tokens)+SUM(cache_hit_tokens),0),1) as hit_pct FROM api_calls GROUP BY day ORDER BY day DESC LIMIT 14"

# Top models by token volume
sqlite3 ~/.hermes/token_logs/token_logs.db \
  "SELECT model, SUM(total_tokens) FROM api_calls GROUP BY model ORDER BY SUM(total_tokens) DESC"

# Average latency per provider
sqlite3 ~/.hermes/token_logs/token_logs.db \
  "SELECT provider, ROUND(AVG(latency_s),1) as avg_latency, COUNT(*) as calls FROM api_calls GROUP BY provider ORDER BY avg_latency"
```

## How the plugin works

Hermes has a plugin system with lifecycle hooks. This plugin registers one hook and one tool:

| Registration | Name | When it fires |
|---|---|---|
| Hook | `post_api_request` | After every API call completes |
| Tool | `token_summary` | When called from conversation or CLI |

The `post_api_request` hook receives Hermes's internal usage dict, extracts token breakdowns and DeepSeek cache fields, then writes to TWO places simultaneously:

1. **Plain-text CSV** (`YYYY-MM-DD.csv`) — append-only, crash-safe. A process restart mid-write leaves a readable partial file.
2. **SQLite database** (`token_logs.db`) — ACID-compliant, indexed, sub-millisecond queries. Powers the `token_summary` tool.

A nightly cron job at 1am UTC gzips yesterday's finalized CSV and vacuums the SQLite database.

```
API call → Hermes normalises usage → post_api_request hook fires → CSV + SQLite written
```

If CSV or DB writing fails, the error is logged and swallowed — the agent never notices.

---

## Upgrading from v1.x to v2.0

v2.0 replaces gzip CSV append with plain-text CSV + SQLite dual-write. To upgrade:

```bash
# Pull latest
cd ~/.hermes/plugins/token-logger && git pull

# Restart Hermes (or reload plugins)
hermes plugins disable token-logger && hermes plugins enable token-logger

# Migrate existing data into SQLite
python3 -c "from token_logger import import_existing_logs; print(import_existing_logs())"
```

Your old `.csv.gz` files are left intact. The migration imports whatever is readable from them into the new SQLite database. Corrupt gzip files (common in v1.x) will be reported as `files_corrupt` — their data is unrecoverable, but new writes from v2.0 are crash-safe.

---

## Uninstall

```bash
hermes plugins disable token-logger
rm -rf ~/.hermes/plugins/token-logger
```

Your CSV logs in `~/.hermes/token_logs/` are left intact.

---

## Repo

https://github.com/underdown/hermes-token-logger

---

## License

MIT