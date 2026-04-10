# X Monitor

Automated X/Twitter monitoring for Desearch AI. The repo polls a fixed list of X accounts and keyword searches, deduplicates new posts, stores a rolling 24 hour tweet window, and prepares alerts for Discord `#x-alerts`.

## Quick Start

### Prerequisites
- Python 3
- `uv` available on the machine that runs `monitor.py` and `feishu_digest.py`
- `DESEARCH_API_KEY` set in the shell or in a local `.env`

Optional, depending on which flows you use:
- `OPENROUTER_API_KEY` for `summarize.py`
- `DISCORD_BOT_TOKEN` when posting outside an OpenClaw-managed environment
- Feishu app credentials in `config.json` for digest export

### Setup

1. Copy env values from `.env.example` into a local `.env`, or export them in your shell.
2. Review `config.json` and confirm the monitored accounts, keyword queries, Discord channel, and Feishu settings.
3. Run a dry run before enabling automation.

```bash
DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run
```

## Purpose

This repo is the passive monitoring half of the Desearch social workflow.

It is responsible for:
- polling monitored X accounts with timeline queries
- searching X for configured brand and subnet keywords
- deduplicating posts against `state.json`
- saving a rolling 24 hour cache in `tweets_window.json`
- writing new alert candidates to `pending_alerts.json`
- supporting downstream Discord posting, summaries, stats, and optional Feishu digests

It does not perform engagement actions such as replying, liking, or following. That belongs in the separate `x-engage` repo.

## Runtime Behavior

### Monitor loop

`monitor.py` is the main collector.

At runtime it:
1. loads `.env` from the repo if present
2. reads monitored accounts and keyword searches from `config.json`
3. calls the shared `desearch.py` X search script for timelines and keyword search
4. tags tweets with monitor metadata such as category, source, importance, and context
5. skips already seen tweet IDs using `state.json`
6. writes all fetched tweets into `tweets_window.json`, pruned to the last 24 hours
7. writes only newly detected tweets into `pending_alerts.json`
8. prints a JSON payload with stats, new tweets, and any source errors

### Cron behavior

The repo is intended to run every 2 hours. `CRON_PROMPT.md` describes the expected cron agent flow:
- run `monitor.py`
- exit silently when `total_new == 0`
- post new tweets to Discord channel `1477727527618347340`
- optionally append a digest to Feishu when `feishu.doc_token` and `feishu.app_id` are configured
- report compact source failures when any monitored source errors

### State files

These runtime files are generated or updated by the monitor:
- `state.json`: per-source seen tweet IDs and `last_run`
- `tweets_window.json`: sliding 24 hour cache used by the summarizer and stats scripts
- `pending_alerts.json`: new tweets waiting for `post-to-discord.cjs`

## Commands

| Command | What it does |
| --- | --- |
| `DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run` | Run the collector without writing state files |
| `DESEARCH_API_KEY=xxx uv run python monitor.py` | Run the collector and update runtime state |
| `DESEARCH_API_KEY=xxx uv run python monitor.py --reset` | Clear seen-state for this run and refetch everything |
| `python3 summarize.py --dry-run` | Build an LLM summary from the current rolling tweet window without posting |
| `python3 summarize.py --hours 24` | Summarize the last 24 hours of cached tweets |
| `node run-summarizer.cjs 12` | Wrapper that runs `summarize.py` for the last 12 hours |
| `node post-to-discord.cjs` | Read `pending_alerts.json`, post one grouped alert message, then clear the file |
| `python3 daily_stats.py --hours 24` | Print grouped activity stats from the rolling window |
| `node post-daily-stats.cjs 24` | Generate the 24 hour stats report and post it to Discord |
| `uv run python feishu_digest.py --file monitor_output.json --dry-run` | Render the Feishu digest without writing to Feishu |

## Configuration

### Environment variables

From `.env.example`:
- `DESEARCH_API_KEY`: required for X search and timeline lookups
- `OPENROUTER_API_KEY`: required for `summarize.py`
- `DISCORD_BOT_TOKEN`: required for direct Discord posting unless the runtime injects one

### Repo config

`config.json` controls:
- monitored accounts, categories, and context notes
- keyword searches
- Discord alert channel
- optional Feishu document destination
- monitor filters such as reply handling and normal-importance thresholds

## Architecture Overview

High level flow:

1. `monitor.py` fetches timelines and keyword matches through the shared Desearch search script.
2. New tweets are deduplicated against `state.json`.
3. All fetched tweets are merged into `tweets_window.json` for later analysis.
4. New tweets are copied into `pending_alerts.json` for alert delivery.
5. Downstream scripts either:
   - post grouped Discord alerts directly
   - generate an LLM summary from the rolling window
   - print daily stats from the rolling window
   - append the monitor output to Feishu

See `docs/architecture.md` for the repo structure and design tradeoffs.

## Monitored Sources

The current config tracks:
- account timelines including `@const`, `@desearch_ai`, `@SiamKidd`, `@ExaAILabs`, `@openclaw`, `@opentensor`, and other ecosystem accounts
- keyword searches for `#desearch`, `@desearch_ai`, `sn22 bittensor`, and `subnet22`

The exact source list lives in `config.json` and should be treated as the source of truth.

## Feishu Digest

Feishu export is optional.

To enable it, fill the `feishu` section in `config.json`:
- `doc_token`
- `app_id`
- `app_secret`

When both the document token and app credentials are present, `feishu_digest.py` can append a category-grouped digest of newly discovered posts to the target Feishu doc.

## Known Issues

Current repo limitations and mismatches are tracked in `docs/known-issues.md`. Read that file before changing cron wiring or relying on the summarizer configuration keys.
