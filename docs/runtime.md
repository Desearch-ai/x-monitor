# Runtime Orchestration

## Overview

X Monitor runs on a file-based pipeline with four distinct stages across two repos.

```
x-monitor                      x-engage
─────────────────────────     ───────────────────────────────────────────
[monitor.py] 2h               [analyze.py] 4h
    ↓ tweets_window.json  ──────────────────→
    ↓ pending_alerts.json                            [pending_actions.json]
    ↓                                                   ↓
[post-to-discord.cjs]                              [human review]
    ↓                                                   ↓
[#x-alerts Discord]                               [execute_actions.py] (manual)
                                                   ↓
                                                  [X live / dry-run]
```

## Stage cadence

| Stage | Frequency | Trigger | Auto? |
|-------|-----------|---------|-------|
| monitor.py | Every 2h | OpenClaw cron | ✅ |
| post-to-discord.cjs | After each monitor run | Cron wrapper | ✅ |
| analyze.py | Every 4h | OpenClaw cron | ✅ |
| execute_actions.py | Manual | Human decision | ❌ |

## Anti-concurrency locks

Every stage uses a PID-based lock file to prevent overlapping runs.

| File | Stage | Location |
|------|-------|----------|
| `.monitor.lock` | monitor.py | x-monitor/ |
| `.post-discord.lock` | post-to-discord.cjs | x-monitor/ |
| `.analyze.lock` | run-engage.sh | x-engage/ |
| `.executor.lock` | execute_actions.py | x-engage/ |

If a script is killed mid-run, its lock may be stale. Check:

```bash
# Check who holds a lock
cat ~/.openclaw/x-monitor/.monitor.lock
kill -0 $(cat ~/.openclaw/x-monitor/.monitor.lock) 2>/dev/null && echo "still running" || echo "stale"
```

Remove stale locks manually:

```bash
rm ~/.openclaw/x-monitor/.monitor.lock
```

## Dry-run testing

All Python scripts support `--dry-run`:

```bash
cd ~/projects/openclaw/x-monitor
DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py --dry-run
```

## Manual execution

If the cron agent is unavailable, run stages manually:

```bash
# Stage 1: Monitor
cd ~/projects/openclaw/x-monitor
DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py

# Stage 2: Discord alert
cd ~/projects/openclaw/x-monitor
node post-to-discord.cjs

# Stage 3: Engagement analysis
cd ~/projects/openclaw/x-engage
./run-engage.sh

# Stage 4: Dry-run executor
cd ~/projects/openclaw/x-engage
./run-executor.sh

# Stage 4: Live executor (after human review)
cd ~/projects/openclaw/x-engage
./run-executor.sh --live
```

## Rollback procedure

If a script change causes unexpected behavior:

1. **Remove all lock files** to clear stuck instances
2. **Restore the previous version** from git:

   ```bash
   cd ~/projects/openclaw/x-monitor
   git checkout HEAD~1 -- monitor.py post-to-discord.cjs
   
   cd ~/projects/openclaw/x-engage
   git checkout HEAD~1 -- run-engage.sh execute_actions.py
   ```
3. **Verify state files are healthy** — check that pending_alerts.json and tweets_window.json are valid JSON
4. **Re-run** to confirm normal operation

## Account profiles

x-engage supports multiple X accounts via `config.json` → `x_accounts`.
Each account can have its own `browser_profile` path.

Currently the executor uses:
- `X_BROWSER_PROFILE` env var if set
- otherwise `~/.x-engage-browser-profile`

**Note**: Per-account browser profile wiring is partial. The config stores per-account browser paths, but the executor uses a single profile path. Coordinate with the team before running concurrent executions on multiple accounts.

## Failure recovery

If `pending_alerts.json` grows stale or `tweets_window.json` is corrupted:

```bash
cd ~/projects/openclaw/x-monitor
# Remove state to force full re-fetch
DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py --reset
```

The `--reset` flag clears `state.json` (seen IDs), so all tweets will be re-fetched as "new".
