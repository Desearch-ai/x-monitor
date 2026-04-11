# X Monitor System

Automated X (Twitter) monitoring for Desearch AI using the Desearch API.

## What it monitors

### Dual-lane model

The system routes signals through two lanes:

- **Founder Lane** — signals relevant to the founder personally (personal brand, interests, relationships)
- **Brand Lane** — signals relevant to Desearch as a product and company (brand mentions, subnet discussions, competitors)

Each watched account or keyword maps to a **bucket** and one or more **lanes**. Tweets are normalized with `_monitor_lanes` and `_monitor_route_hints` so downstream tools can route them appropriately.

### Accounts

| Account | Bucket | Lanes | Notes |
|---|---|---|---|
| @const | bittensor | founder, brand | Bittensor founder — all posts |
| @desearch_ai | desearch | brand | Our X account |
| @SiamKidd | bittensor | founder, brand | Bittensor investor |
| @ExaAILabs | competitor | brand | Direct competitor |
| @openclaw | openclaw | founder | Our platform |
| @steipete | openclaw | founder | OpenClaw founder |
| @markjeffrey | bittensor | founder, brand | Bittensor Fund |
| @numinous_ai | subnet | brand | Partner subnet |
| @opentensor | bittensor | brand | Official Bittensor |
| @vaNlabs | bittensor | founder, brand | Bittensor investor |
| @YVR_Trader | bittensor | founder, brand | VC/Angel investor |
| @DrocksAlex2 | community | brand | Miner/trader |
| @HungNgu76442123 | community | brand | Miner/trader |
| @marclou | influencer | founder | Startup founder |
| @johnrushx | influencer | founder | Startup founder |
| @AlexFinn | builder | founder | Indie builder |

### Keywords

| Query | Bucket | Lanes | Notes |
|---|---|---|---|
| `#desearch` | desearch | brand | Brand hashtag |
| `@desearch_ai` | desearch | brand | Direct mentions |
| `sn22 bittensor` | subnet | brand | Subnet 22 mentions |
| `subnet22` | subnet | brand | Subnet 22 shorthand |

## Setup

### 1. Set API Key

```bash
export DESEARCH_API_KEY="your_key_from_console.desearch.ai"
```

Or set it in `~/.zshrc` for permanent use.

### 2. Test manually

```bash
cd ~/projects/openclaw/x-monitor
DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run
```

`--dry-run` fetches tweets without writing state, so you can test repeatedly.

### 3. Run a real collection pass

```bash
DESEARCH_API_KEY=xxx uv run python monitor.py
```

### 4. Filter by lane

```bash
# Only founder-lane tweets in output
DESEARCH_API_KEY=xxx uv run python monitor.py --lane-filter founder

# Only brand-lane tweets
DESEARCH_API_KEY=xxx uv run python monitor.py --lane-filter brand
```

### 5. Post queued alerts to Discord

```bash
node post-to-discord.cjs
```

### 6. Enable cron

Cron job ID: `cf7191f8-4097-4cc0-9c90-64a86c663366` — runs every 2 hours, posts to #x-alerts.

## Commands

| Command | Purpose |
|---|---|
| `uv run python monitor.py --dry-run` | Fetch without saving state |
| `uv run python monitor.py --reset` | Rebuild dedupe state |
| `uv run python monitor.py --lane-filter <lane>` | Filter output by lane (`founder` or `brand`) |
| `node post-to-discord.cjs` | Post queued alerts to Discord |
| `python3 summarize.py --dry-run --hours 4` | Preview summary for last 4h |
| `python3 summarize.py --hours 4` | Generate and post summary |
| `python3 daily_stats.py --hours 24` | Print grouped window stats |
| `node post-daily-stats.cjs 24` | Post daily stats to Discord |
| `uv run python feishu_digest.py --file <output.json>` | Append monitor output to Feishu doc |

## Config shape (v2)

```json
{
  "lanes": [
    { "id": "founder", "buckets": ["bittensor", "builder", "influencer"], "route_hint": "x-engage/founder" },
    { "id": "brand", "buckets": ["desearch", "subnet", "competitor", "community"], "route_hint": "x-engage/brand" }
  ],
  "accounts": [
    { "username": "const", "bucket": "bittensor", "lanes": ["founder", "brand"], "importance": "high", "include_retweets": false }
  ],
  "keywords": [
    { "query": "#desearch", "bucket": "desearch", "lanes": ["brand"], "importance": "high" }
  ]
}
```

## Files

| File | Purpose |
|---|---|
| `config.json` | Lanes, accounts, keywords, Discord/Feishu settings |
| `monitor.py` | Collection entry point with dual-lane normalization |
| `state.json` | Per-source seen-ID dedupe store |
| `tweets_window.json` | Rolling 24h tweet cache |
| `pending_alerts.json` | Discord delivery queue |

## Output metadata (v2)

Each normalized tweet includes:

- `_monitor_source` — `account:<username>` or `keyword:<query>`
- `_monitor_bucket` — watchlist bucket
- `_monitor_category` — backward-compat alias for `_monitor_bucket`
- `_monitor_importance` — `high` or `normal`
- `_monitor_context` — context string from config
- `_monitor_lanes` — e.g. `["founder", "brand"]`
- `_monitor_route_hints` — e.g. `["x-engage/founder", "x-engage/brand"]`

## Architecture

See `docs/architecture.md` for the dual-lane pipeline diagram and design decisions.
See `docs/features.md` for the feature inventory.
See `docs/known-issues.md` for current limitations.
