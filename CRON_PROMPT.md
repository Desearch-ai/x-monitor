# X Monitor Runtime Prompt

This job owns passive signal ingestion only. It must never perform live X account actions.

## Safe cadence

- **Monitor ingestion:** every 2 hours
- **Engagement analysis / queue refresh:** every 4 hours in `x-engage`
- **Approval review:** manual, after the analysis digest lands in Discord
- **Live execution:** manual or separately scheduled only after approval, never bundled into this monitor job

## Production flow

### Step 1: collect signals

```bash
cd /Users/giga/projects/openclaw/x-monitor
DESEARCH_API_KEY="$DESEARCH_API_KEY" uv run python monitor.py
```

Behavior:
- exits `0` with a JSON `skipped` payload if another monitor run already holds `.monitor.lock`
- writes `state.json`, `tweets_window.json`, and `pending_alerts.json` with atomic replace semantics
- preserves `pending_alerts.json` across partial downstream failures

### Step 2: post pending alerts to Discord

Only run this step after `monitor.py` exits successfully.

```bash
cd /Users/giga/projects/openclaw/x-monitor
node post-to-discord.cjs
```

Behavior:
- exits safely if `pending_alerts.json` is missing or empty
- serializes `pending_alerts.json` access with `.pending-alerts.lock`
- removes only successfully posted chunks from `pending_alerts.json`
- leaves unsent chunks queued for retry after any Discord/API failure

### Step 3: optional Feishu digest

Run only when `config.json` has Feishu credentials configured.

```bash
cd /Users/giga/projects/openclaw/x-monitor
DESEARCH_API_KEY="$DESEARCH_API_KEY" uv run python monitor.py --dry-run | uv run python feishu_digest.py
```

## Dry-run / non-prod verification

```bash
cd /Users/giga/projects/openclaw/x-monitor
DESEARCH_API_KEY="$DESEARCH_API_KEY" uv run python monitor.py --dry-run
```

Dry-run fetches and prints output without mutating `state.json`, `tweets_window.json`, or `pending_alerts.json`.

## Failure handling

- If `monitor.py` returns source errors, record the compact error list and retry on the next interval.
- If `post-to-discord.cjs` fails mid-run, fix the Discord/token/channel issue, then rerun the same command. The remaining queue is preserved.
- Never combine this cron with `x-engage` live execution. Approval stays human-first.
