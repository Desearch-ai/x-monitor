# X Monitor System

Automated X (Twitter) monitoring for Desearch AI using the Desearch API.

## What it monitors

### Accounts
| Account | Category | Importance |
|---------|----------|------------|
| @const | bittensor | HIGH (all posts) |
| @desearch_ai | desearch | HIGH (all posts) |
| @SiamKidd | bittensor | HIGH (all posts) |
| @ExaAILabs | competitor | HIGH (all posts) |
| @openclaw | system | HIGH (all posts) |
| @marclou | influencer | normal (5+ likes) |
| @johnrushx | influencer | normal (5+ likes) |
| @markjeffrey | content | normal (5+ likes) |
| @numinous_ai | ai | normal (5+ likes) |

### Keywords
- `#desearch` — brand hashtag
- `@desearch_ai` — direct mentions
- `sn22 bittensor` — Subnet 22
- `subnet22` — Subnet 22

## Setup

### 1. Set API Key

Add to your shell or OpenClaw env:
```bash
export DESEARCH_API_KEY="your_key_from_console.desearch.ai"
```

Or set it in `~/.zshrc` for permanent use.

### 2. (Optional) Set Feishu Doc for Daily Digest

Edit `config.json` and fill in the `feishu` section:

```json
"feishu": {
  "doc_token": "TOKEN_FROM_URL",
  "app_id": "cli_xxxxx",
  "app_secret": "xxxxxxxxxx"
}
```

Steps:
1. Go to https://open.feishu.cn/app → Create a new app
2. Enable the `docx:document` permission scope
3. Install the app to your workspace
4. Copy `App ID` and `App Secret` into config.json
5. Open or create the target Feishu doc, get `doc_token` from the URL:
   `https://xxx.feishu.cn/docx/TOKEN_HERE`

The digest script (`feishu_digest.py`) will append a dated section per category
each time the cron runs and finds new tweets.

### 3. Test manually

```bash
cd x-monitor
DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run
```

`--dry-run` won't save state, so you can test repeatedly.

### 4. Enable cron job

Once working, enable the cron in OpenClaw:
- Cron job ID: `cf7191f8-4097-4cc0-9c90-64a86c663366`
- Runs every 2 hours
- Posts to Discord #x-alerts

## Files

- `config.json` — accounts, keywords, Discord/Feishu settings
- `monitor.py` — main script (fetches, deduplicates, outputs JSON)
- `state.json` — auto-created, tracks seen tweet IDs (don't edit manually)

## Adding more accounts/keywords

Edit `config.json`:
```json
{
  "accounts": [
    { "username": "newaccount", "category": "bittensor", "importance": "high", "context": "Why we monitor this" }
  ]
}
```

Importance levels:
- `"high"` — all posts reported
- `"normal"` — only posts with 5+ likes

## Cron Schedule

Default: every 2 hours. Change in OpenClaw cron settings.

## Queue Lifecycle (pending_alerts.json)

`pending_alerts.json` is the handoff file between `monitor.py` and `post-to-discord.cjs`.

```
monitor.py  ──writes──▶  pending_alerts.json  ──reads──▶  post-to-discord.cjs
```

**Write side (monitor.py):**
- Each run overwrites `pending_alerts.json` with only the *new* tweets found in that run.
- If `post-to-discord.cjs` has not yet consumed a previous batch, that batch will be
  replaced. Design the cron so the poster runs immediately after the monitor.

**Read/consume side (post-to-discord.cjs):**
- Reads all pending tweets, groups them by `_monitor_category`, and splits into
  Discord-safe chunks (≤ 1900 chars each).
- Posts chunks one at a time. **After each chunk posts successfully**, those tweets are
  removed from `pending_alerts.json` immediately (incremental update).
- If a later chunk fails, all earlier chunks are already durably removed. The remaining
  tweets stay in `pending_alerts.json` and a non-zero exit code is returned.
- Re-running `post-to-discord.cjs` after a partial failure is safe: it will only send
  the chunks that are still pending — no duplicates.

**Channel configuration:**
- Channel ID must be set as `config.discord.alerts_channel` in `config.json`.
- Both `post-to-discord.cjs` and `summarize.py` read from this single source of truth.

**Dry-run verification:**
```bash
# Check what would be posted without actually posting:
node -e "
  const {buildChunks, readPendingAlerts} = require('./post-to-discord.cjs');
  const t = readPendingAlerts('./pending_alerts.json');
  const c = buildChunks(t);
  console.log(c.length + ' chunk(s), tweets: ' + t.length);
  c.forEach((ch, i) => console.log('Chunk ' + (i+1) + ' (' + ch.tweets.length + ' tweets, ' + ch.text.length + ' chars)'));
"
```
