# X Monitor runtime verification

This file captures the manual verification artifact for the Discord delivery fix.

## Build and test verification

```bash
node --test tests/test_post_to_discord.cjs
python3 -m unittest discover -s tests
python3 -m py_compile monitor.py summarize.py
```

Expected result: all tests pass and Python files compile without errors.

## Manual end-to-end Discord verification

Run from the repo root with a real pending queue and bot token available in the environment or `~/.openclaw/openclaw.json`:

```bash
node post-to-discord.cjs
```

Observed verification output on the repaired flow:

```text
Discord channel: 1498287725223215185
Pending alerts: 1
Built 1 Discord chunk(s) from 1 tweet(s)
Posting chunk 1/1 (194 chars, 1 tweet(s))
  chunk 1 OK — 0 alert(s) still pending
Done: 1 message(s) posted, pending_alerts.json cleared
```

## Queue lifecycle contract

- successful chunk: remove only that chunk's tweets from `pending_alerts.json`
- failed chunk: leave the remaining queue untouched so the retry starts from the first unsent chunk
- retry after partial success: does not duplicate already delivered chunks in Discord
- Discord 429/rate-limit responses: retry with bounded `Retry-After`/`retry_after` backoff before failing the chunk; default cron attempts are capped so a rate limit cannot hang forever
- lock handling: active `.pending-alerts.lock` directories are respected; stale dead locks are recovered before cron posts

## Task #1443 stale lock/backlog hygiene verification

Observed on Giga's local runtime path `/Users/giga/projects/openclaw/x-monitor` for O-69 hygiene:

- stale lock files `.monitor.lock` and `.pending-alerts.lock` both contained PID `6295`; `ps -p 6295` returned no process and non-blocking lock probes succeeded before rotation
- `pending_alerts.json` contained 543 preserved alerts before rotation, oldest `2016-05-05T23:53:28+00:00`, newest `2026-05-09T14:08:16+00:00`
- the full queue was backed up before the active queue was atomically reset to `[]`; the active queue verified at count `0` after rotation
- no live X actions and no Discord backlog drain were executed; backlog handling remains digest/preserve/rate-limited, not blind mass posting

Canonical artifact with exact backup paths and hashes:

```text
/Users/giga/.docs/cosmic-brain/projects/x-monitor/tasks/ops/2026-05-x-monitor-locks-backlog-hygiene.md
```
