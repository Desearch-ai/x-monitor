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
Discord channel: 1477727527618347340
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
