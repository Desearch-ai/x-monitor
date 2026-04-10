# X Monitor

Automated X/Twitter monitoring for Desearch AI. This repo polls a fixed set of X accounts and search queries, deduplicates newly seen posts, stores a rolling 24 hour cache, and prepares alerts for Discord `#x-alerts`.

## Purpose

X Monitor is the passive monitoring half of the Desearch social workflow.

It is responsible for:
- polling configured X timelines and search queries
- deduplicating posts against local seen-state
- saving a 24 hour rolling window for summaries and stats
- queuing newly detected posts for Discord delivery
- optionally exporting a digest to Feishu

It does not perform active engagement actions such as replies, likes, or follows. That belongs in the separate `x-engage` repo.

## Quick Start

### Prerequisites
- Python 3
- `uv` on PATH for the Python entry points
- `DESEARCH_API_KEY` set in a local `.env` or shell

Optional, depending on which paths you use:
- `OPENROUTER_API_KEY` for `summarize.py`
- `DISCORD_BOT_TOKEN` when posting outside an OpenClaw-managed runtime
- Feishu app credentials in `config.json` for digest export

### Setup

1. Copy `.env.example` into a local `.env`, or export the same keys in your shell.
2. Review `config.json` and confirm the accounts, keyword queries, Discord channel, and Feishu settings.
3. Run a dry run before enabling cron.

```bash
DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run
```

## Runtime Behavior

### Monitor flow

`monitor.py` is the collector. On each run it:
1. loads `.env`
2. reads monitored accounts, keyword queries, and filters from `config.json`
3. calls the shared Desearch X search script for timelines and keyword searches
4. tags tweets with monitor metadata such as source, category, importance, and context
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

| Command | What it does |
| --- | --- |
| `DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run` | Run the collector without writing runtime state |
| `DESEARCH_API_KEY=xxx uv run python monitor.py` | Run the collector and update `state.json`, `tweets_window.json`, and `pending_alerts.json` |
| `DESEARCH_API_KEY=xxx uv run python monitor.py --reset` | Ignore saved seen-state for this run and refetch everything |
| `python3 summarize.py --dry-run` | Build an LLM summary from the rolling window without posting |
| `python3 summarize.py --hours 24` | Summarize the last 24 hours of cached tweets and post to Discord |
| `node run-summarizer.cjs 12` | Wrapper that runs `summarize.py` for the last 12 hours |
| `node post-to-discord.cjs` | Read `pending_alerts.json`, build one grouped Discord message, post it, then clear the queue on success |
| `python3 daily_stats.py --hours 24` | Print grouped source and category stats from the rolling window |
| `node post-daily-stats.cjs 24` | Generate the 24 hour stats report and post it to Discord |
| `uv run python feishu_digest.py --file monitor_output.json --dry-run` | Render the Feishu digest without writing to Feishu |

## Configuration

### Environment variables

From `.env.example`:
- `DESEARCH_API_KEY`: required for timeline and search lookups
- `OPENROUTER_API_KEY`: required for `summarize.py`
- `DISCORD_BOT_TOKEN`: required for direct Discord posting unless the runtime provides a bot token automatically

### `config.json`

`config.json` is the source of truth for:
- monitored accounts, categories, importance, and context notes
- keyword searches
- Discord alert channel
- optional Feishu destination
- monitor filters such as reply handling and engagement thresholds

The current config includes high-importance tracking for accounts such as `@const`, `@desearch_ai`, `@SiamKidd`, `@ExaAILabs`, `@openclaw`, and a wider Bittensor/OpenClaw ecosystem list, plus keyword searches for `#desearch`, `@desearch_ai`, `sn22 bittensor`, and `subnet22`.

## Architecture Overview

High-level pipeline:
1. `monitor.py` fetches timelines and keyword matches through the shared Desearch script.
2. Seen IDs in `state.json` prevent duplicate alerts.
3. All fetched tweets are merged into `tweets_window.json` for summaries and stats.
4. Newly discovered tweets are written to `pending_alerts.json` for delivery.
5. Downstream scripts either post alerts to Discord, summarize the rolling window, print daily stats, or append a Feishu digest.

See `docs/architecture.md` for the repo structure and design decisions.

## Feishu Digest

Feishu export is optional. To enable it, populate these `config.json` keys:
- `feishu.doc_token`
- `feishu.app_id`
- `feishu.app_secret`

When those values are present, `feishu_digest.py` can append a category-grouped digest of newly detected tweets to the target document.

## Known Issues

Current verified limitations are tracked in `docs/known-issues.md`. Read that file before changing cron wiring or assuming the alert queue is durable across failed delivery attempts.
