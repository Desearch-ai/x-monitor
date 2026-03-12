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

### 2. (Optional) Set Feishu Doc

Edit `config.json` and set `feishu.doc_token` to your doc token.
Get it from the Feishu doc URL: `https://xxx.feishu.cn/docx/TOKEN`

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
