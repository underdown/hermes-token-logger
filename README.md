# hermes-token-logger

A Hermes Agent plugin that logs every API call to gzip-compressed CSV files — with full DeepSeek cache-hit/miss breakdowns, token counts, and cost estimates.

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
~/.hermes/token_logs/YYYY-MM-DD.csv.gz
```

Files rotate daily (UTC). One `.csv.gz` per day, forever — gzip compressed to keep size manageable.

View raw:
```bash
zcat ~/.hermes/token_logs/2026-05-08.csv.gz | head -5
```

Parse with Python:
```python
import gzip, csv
with gzip.open("~/.hermes/token_logs/2026-05-08.csv.gz") as f:
    for row in csv.DictReader(f):
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
| `cache_write_tokens` | int | DeepSeek prompt cache write tokens |
| `output_tokens` | int | Completion tokens |
| `reasoning_tokens` | int | Reasoning / chain-of-thought tokens (if separated) |
| `total_tokens` | int | `input + output` tokens |
| `cost_usd` | float | Estimated cost based on Hermes pricing table |
| `cost_status` | string | `within_budget`, `over_budget`, `unmetered` |
| `cost_source` | string | Pricing source: `nvidia`, `deepseek`, `openrouter`, etc. |
| `cache_hit_rate_pct` | float | `cache_hit_tokens / (input + cache_hit + cache_write) × 100` |
| `latency_s` | float | Round-trip API call duration in seconds |

---

## Shell analysis one-liners

```bash
# Total cost this month
zcat ~/.hermes/token_logs/*.csv.gz | grep deepseek | cut -d, -f13 | awk '{s+=$1} END {printf "DeepSeek cost: $%.4f\n", s}'

# Cache hit rate by day
for f in ~/.hermes/token_logs/*.csv.gz; do
  echo -n "$(basename $f): "
  zcat "$f" | awk -F, 'NR>1 && $7+0>0 {h+=$7; t+=$6+$7+$9} END {print (t?100*h/t:0)"% hit rate"}'
done

# Top models by token volume
zcat ~/.hermes/token_logs/*.csv.gz | cut -d, -f4,11 | awk -F, '{t[$4]+=$11} END {for(m in t) print t[m], m}' | sort -rn | head

# Sessions with highest cost
zcat ~/.hermes/token_logs/*.csv.gz | awk -F, 'NR>1 && $13+0>0.001 {s[$2]+=$13} END {for(k in s) print s[k], k}' | sort -rn | head

# Average latency per provider
zcat ~/.hermes/token_logs/*.csv.gz | awk -F, 'NR>1 {l[$3]++; s[$3]+=$16} END {for(p in l) print p, s[p]/l[p]"s avg"}'
```

---

## Configuration

No configuration required. The plugin uses Hermes's built-in pricing table.

If you want to change the log directory, edit `token_logger.py`:

```python
_TOKEN_LOG_DIR = Path.home() / ".your/custom/path"
```

Then restart Hermes.

---

## How the plugin works

Hermes has a plugin system with lifecycle hooks. This plugin registers one hook and one tool:

| Registration | Name | When it fires |
|---|---|---|
| Hook | `post_api_request` | After every API call completes |
| Tool | `token_summary` | When called from conversation or CLI |

The `post_api_request` hook receives Hermes's internal usage dict (already normalised across providers), extracts the DeepSeek cache fields, and appends one row to the CSV.

```
API call → Hermes normalises usage → post_api_request hook fires → CSV row written
```

If CSV writing fails, the error is logged and swallowed — the agent never notices.

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