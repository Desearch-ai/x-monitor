# X Monitor

Automated X/Twitter monitoring for Desearch AI. This repo polls a fixed set of X accounts and search queries, deduplicates newly seen posts, stores a rolling 24 hour cache, and prepares alerts for Discord `#x-alerts`.

## Purpose

### Dual-lane model

The system routes signals through two lanes:

- **Founder Lane** — signals relevant to the founder personally (personal brand, interests, relationships)
- **Brand Lane** — signals relevant to Desearch as a product and company (brand mentions, subnet discussions, competitors)

Each watched account or keyword maps to a **bucket** and one or more **lanes**. Tweets are normalized with `_monitor_lanes` and `_monitor_route_hints` so downstream tools can route them appropriately.

X Monitor is the passive monitoring half of the Desearch social workflow. It is responsible for:
- polling configured X timelines and search queries
- deduplicating posts against local seen-state
- saving a 24 hour rolling window for summaries and stats
- queuing newly detected posts for Discord delivery
- optionally exporting a digest to Feishu

It does not perform active engagement actions such as replies, likes, or follows. That belongs in the separate `x-engage` repo.

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

## Quick Start

### Prerequisites
- Python 3
- `uv` on PATH for the Python entry points
- `DESEARCH_API_KEY` set in a local `.env` or shell

```bash
export DESEARCH_API_KEY="your_key_from_console.desearch.ai"
```

Optional, depending on which paths you use:
- `OPENROUTER_API_KEY` for `summarize.py`
- `DISCORD_BOT_TOKEN` when posting outside an OpenClaw-managed runtime
- Feishu app credentials in `config.json` for digest export

### Setup

1. Copy `.env.example` into a local `.env`, or export the same keys in your shell.
2. Review `config.json` and confirm the accounts, keyword queries, Discord channel, and Feishu settings.
3. Run a dry run before enabling cron.

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

## Runtime Behavior

### Monitor flow

`monitor.py` is the collector. On each run it:
1. loads `.env`
2. reads monitored accounts, keyword queries, and filters from `config.json`
3. calls the shared Desearch X search script for timelines and keyword searches
4. tags tweets with monitor metadata such as source, bucket, lanes, route hints, importance, and context
5. skips already seen tweet IDs using `state.json`
6. writes all fetched tweets into `tweets_window.json`, pruned to the last 24 hours
7. writes newly detected tweets into `pending_alerts.json`
8. prints a JSON payload with stats, new tweets, and source-specific errors

### Cron behavior

The intended production cadence is every 2 hours. `CRON_PROMPT.md` describes the expected automation flow:
- run `monitor.py`
- exit silently when `total_new == 0`
- post new alerts to Discord channel `1477727527618347340`
- optionally pipe the output into `feishu_digest.py` when Feishu credentials are configured
- report compact source failures when any monitored source errors

### Runtime files

These files are created or updated by the monitor path:
- `state.json`: per-source seen tweet IDs plus `last_run`
- `tweets_window.json`: rolling 24 hour cache used by `summarize.py` and `daily_stats.py`
- `pending_alerts.json`: new alert payloads consumed by `post-to-discord.cjs`

## Commands

| Command | Purpose |
|---|---|
| `uv run python monitor.py --dry-run` | Fetch without saving state |
| `uv run python monitor.py --reset` | Rebuild dedupe state |
| `uv run python monitor.py --lane-filter <lane>` | Filter output by lane (`founder` or `brand`) |
| `node post-to-discord.cjs` | Post queued alerts to Discord |
| `python3 summarize.py --dry-run --hours 4` | Preview summary for last 4h |
| `python3 summarize.py --hours 4` | Generate and post summary |
| `node run-summarizer.cjs 12` | Wrapper that runs `summarize.py` for the last 12 hours |
| `python3 daily_stats.py --hours 24` | Print grouped window stats |
| `node post-daily-stats.cjs 24` | Post daily stats to Discord |
| `uv run python feishu_digest.py --file <output.json>` | Append monitor output to Feishu doc |

## Configuration

### Environment variables

From `.env.example`:
- `DESEARCH_API_KEY`: required for timeline and search lookups
- `OPENROUTER_API_KEY`: required for `summarize.py`
- `DISCORD_BOT_TOKEN`: required for direct Discord posting unless the runtime provides a bot token automatically

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

## Feishu Digest

Feishu export is optional. To enable it, populate these `config.json` keys:
- `feishu.doc_token`
- `feishu.app_id`
- `feishu.app_secret`

When those values are present, `feishu_digest.py` can append a category-grouped digest of newly detected tweets to the target document.

## Architecture

See `docs/architecture.md` for the dual-lane pipeline diagram and design decisions.
See `docs/features.md` for the feature inventory.
See `docs/known-issues.md` for current limitations.
