# X Monitor Cron Prompt

This is used by the OpenClaw cron agent. It orchestrates the full X signal pipeline.

## Architecture

```
monitor.py (2h)
    → pending_alerts.json  → post-to-discord.cjs
    → tweets_window.json   → (x-engage) analyze.py (4h)
                              → pending_actions.json (human review)
                              → execute_actions.py (manual / conservative schedule)
```

**Two repos, three stages:**
1. `x-monitor` — signal ingestion (monitor.py), Discord alerts (post-to-discord.cjs)
2. `x-engage` — scoring + digest (analyze.py), action execution (execute_actions.py)
3. Human gate — pending_actions.json must be reviewed before live execution

## Stage 1 — Monitor (every 2h)

```bash
cd ~/projects/openclaw/x-monitor && \
  DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py
```

If DESEARCH_API_KEY is not set, check config.json for notes, then stop with a warning.

monitor.py outputs JSON. If `total_new == 0`: done silently.

On new tweets: post-to-discord.cjs is called automatically by the cron wrapper.

### Anti-concurrency
monitor.py acquires a PID lock at `.monitor.lock` before running.
If another instance is running, it exits immediately (exit 1).

## Stage 2 — Discord alert (after each monitor run)

```bash
cd ~/projects/openclaw/x-monitor && \
  node post-to-discord.cjs
```

post-to-discord.cjs reads `pending_alerts.json`, groups tweets by `_monitor_category`, builds one Discord message, posts to `#x-alerts` (1477727527618347340), and clears the queue on success.

### Anti-concurrency
post-to-discord.cjs acquires a PID lock at `.post-discord.lock` before running.

### Feishu digest (optional)

If `feishu.doc_token` and `feishu.app_id` are both set in config.json:

```bash
cd ~/projects/openclaw/x-monitor && \
  DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py | uv run python feishu_digest.py
```

Or run separately after monitor:

```bash
DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py && uv run python feishu_digest.py
```

## Stage 3 — Engagement analysis (every 4h)

```bash
cd ~/projects/openclaw/x-engage && ./run-engage.sh
```

`run-engage.sh` calls `analyze.py` which:
- Reads `tweets_window.json` from x-monitor
- Scores and ranks tweets by engagement
- Runs LLM analysis on top performers
- Posts a digest to `#x-alerts` (1477727527618347340)
- Writes top candidates to `pending_actions.json`

### Anti-concurrency
`run-engage.sh` acquires a PID lock at `.analyze.lock` before running.
Uses `uv run python` (project-managed venv).

## Stage 4 — Human review

Before running the executor, review `pending_actions.json`:

```bash
cat ~/projects/openclaw/x-engage/pending_actions.json | python3 -m json.tool | less
```

Each item must have:
- `status: "approved"` (not `"pending"`)
- `action: "retweet"` or `"quote"` (not `"skip"`)

## Stage 5 — Action execution (manual, human-gated)

Dry-run first (always):

```bash
cd ~/projects/openclaw/x-engage && ./run-executor.sh
# Output shows what WOULD be executed without touching X
```

Live execution (after human review):

```bash
cd ~/projects/openclaw/x-engage && ./run-executor.sh --live
```

The executor reads `pending_actions.json`, finds items with `status=approved`, and executes retweet/quote via Playwright browser automation.

### Anti-concurrency
`execute_actions.py` acquires a PID lock at `.executor.lock` before running.

### Schedule
Execute on a conservative manual schedule (e.g., weekly or after each significant digest).
NOT on auto-cron — always review first.

## Dry-run testing

All components support `--dry-run` / `--dry-run` flags:

```bash
cd ~/projects/openclaw/x-monitor && \
  DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py --dry-run

cd ~/projects/openclaw/x-engage && \
  uv run python execute_actions.py --dry-run
```

## Lock files

Lock files are PID-based and stored in the repo root:
- `.monitor.lock` — x-monitor monitor.py
- `.post-discord.lock` — x-monitor post-to-discord.cjs
- `.analyze.lock` — x-engage run-engage.sh
- `.executor.lock` — x-engage execute_actions.py

If a script exits abnormally, its lock may be stale. Remove manually:

```bash
rm ~/projects/openclaw/x-monitor/.monitor.lock
```

## Error handling

If a stage errors:
1. Check the output log from the cron agent
2. Inspect the relevant JSON file (pending_alerts.json, pending_actions.json)
3. Verify DESEARCH_API_KEY is set and valid
4. Remove any stale lock files before retrying

## OpenClaw Cron IDs

- x-monitor (monitor.py): cf7191f8-4097-4cc0-9c90-64a86c663366 (every 2h)
- x-engage (analyze.py): scheduled separately (every 4h)

## Rollback

If any change causes issues:
1. Remove lock files: `rm ~/.openclaw/x-monitor/.monitor.lock ~/.openclaw/x-monitor/.post-discord.lock ~/.openclaw/x-engage/.analyze.lock ~/.openclaw/x-engage/.executor.lock`
2. Restore previous versions: `git checkout HEAD~1 -- monitor.py post-to-discord.cjs run-engage.sh execute_actions.py`
3. Disable cron entries if the system behaves unexpectedly
