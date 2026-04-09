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

### 4. Manual end-to-end verification

Run each step and check the output:

```bash
# 1. Check what's currently queued
cat pending_alerts.json | python3 -m json.tool | head -30

# 2. Run the monitor (real run — writes pending_alerts.json)
python3 monitor.py

# 3. Inspect queued alerts after monitor run
cat pending_alerts.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} alert(s) queued')"

# 4. Post queued alerts to Discord (dry-run: inspect chunks without sending)
node -e "
const {buildChunks} = require('./post-to-discord.cjs');
" 2>/dev/null || node post-to-discord.cjs --help 2>/dev/null; \
node -e "
const fs = require('fs');
const tweets = JSON.parse(fs.readFileSync('pending_alerts.json','utf8'));
console.log('tweets:', tweets.length);
" 2>/dev/null

# 5. Post for real — verify chunk count, char lengths, and final clear
node post-to-discord.cjs

# 6. Confirm pending_alerts.json was cleared
cat pending_alerts.json
# Expected: []
```

Key log lines to look for in `node post-to-discord.cjs` output:
- `Discord channel: 1477727527618347340` — confirms channel read from config.json
- `Pending alerts: N` — how many tweets were queued
- `Built N Discord chunk(s) from M tweet(s)` — confirms splitting happened
- `Posting chunk 1/N (XXXX chars)` — each chunk size (must be ≤ 2000)
- `Done: N message(s) posted, pending_alerts.json cleared` — success

### 5. Manual verification — summarize flow

```bash
# 1. Dry-run: prints the summary, does NOT post to Discord
python3 summarize.py --dry-run

# With a wider time window (last 24h):
python3 summarize.py --dry-run --hours 24

# 2. Post the summary for real (reads discord.alerts_channel from config.json)
python3 summarize.py

# Expected output:
#   Summarizing N tweets from last 4h (window total: M)...  ← stderr
#   📡 **X Monitor Summary** — YYYY-MM-DD HH:MM UTC         ← stdout (full message)
#   📊 N tweets (last 4h)
#   ...
#   Posted to Discord.  ← stderr on success

# 3. Run via the Node wrapper (cron uses this)
node run-summarizer.cjs
# Expected output:
#   Summarizer done
#   <last 5 lines of summarize.py output including "Posted to Discord.">
```

### 6. Enable cron job

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
