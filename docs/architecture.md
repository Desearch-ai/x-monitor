# Architecture

## Overview

X Monitor is a cron-oriented, file-based pipeline for passive X/Twitter monitoring.

Core roles:
- `monitor.py` collects and normalizes tweet data
- `post-to-discord.cjs` delivers fresh alerts
- `summarize.py` generates an LLM summary from the rolling cache
- `daily_stats.py` reports grouped activity stats
- `feishu_digest.py` writes a digest to Feishu when configured

The design intentionally uses local JSON files instead of a database or queue.

## Repo Structure

- `monitor.py`: main polling, normalization, deduplication, and fan-out entry point
- `summarize.py`: OpenRouter-backed summarizer for `tweets_window.json`
- `run-summarizer.cjs`: thin wrapper around `summarize.py`
- `daily_stats.py`: grouped stats report from the rolling window
- `post-daily-stats.cjs`: Node wrapper that posts the stats report to Discord
- `post-to-discord.cjs`: grouped Discord alert poster for `pending_alerts.json`
- `feishu_digest.py`: Feishu document writer for new monitor output
- `config.json`: source inventory, filters, Discord config, and Feishu config
- `CRON_PROMPT.md`: intended cron-agent behavior
- `.env.example`: required and optional secret names

Generated runtime files:
- `state.json`
- `tweets_window.json`
- `pending_alerts.json`

## Monitoring Flow

### 1. Collection

`monitor.py` reads `config.json` and loops over two source classes:
- account timelines, fetched with `x_timeline`
- keyword searches, fetched with the generic `x` search command

Both paths shell out to the shared Desearch script at:
`~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`

### 2. Normalization

Each tweet is enriched with monitor metadata:
- `_monitor_source`
- `_monitor_category`
- `_monitor_importance`
- `_monitor_context`

When `created_at` arrives in Twitter's textual format, `parse_twitter_date()` converts it to ISO 8601 so the rolling window can be pruned reliably.

### 3. Deduplication

Seen tweet IDs are tracked per source key, for example:
- `timeline:const`
- `keyword:#desearch`

Each source keeps its latest 500 IDs. That is enough to suppress repeated alerts without growing `state.json` forever.

### 4. Fan-out to local files

The collector writes two different downstream artifacts:
- `pending_alerts.json`: newly discovered tweets for immediate alerting
- `tweets_window.json`: all fetched tweets still inside the last 24 hours for analysis and reporting

That split is the repo's core pipeline decision. Alerts only need fresh items. Summaries and stats need a broader rolling context.

### 5. Consumers

#### Discord alert poster
`post-to-discord.cjs` reads `pending_alerts.json`, groups tweets by `_monitor_category`, builds one Discord message, posts it, and clears the queue on success.

#### Summarizer
`summarize.py` reads `tweets_window.json`, filters by an hour window, formats up to 50 tweets for the LLM, and optionally posts the summary to Discord.

#### Daily stats
`daily_stats.py` reads the same rolling window, groups by source and category, and prints a compact stats report.

#### Feishu digest
`feishu_digest.py` expects `monitor.py` JSON output and appends grouped new tweets into a configured Feishu doc.

## Design Decisions

### File-based state instead of a database
The repo stores state in local JSON files next to the scripts. That keeps cron execution simple and makes debugging easy because every intermediate artifact is inspectable.

### Separate fresh-alert and rolling-window outputs
Immediate alerting and later analysis have different data-retention needs. `pending_alerts.json` supports alert delivery, while `tweets_window.json` preserves enough history for summaries and 24 hour stats.

### Shared search backend
The monitor does not talk to X directly. It delegates timeline and search calls to the shared Desearch search script. That keeps search integration centralized, but ties this repo to an external path and output shape.

### Mixed Python and Node tooling
Collection, normalization, and LLM summarization live in Python. Discord posting wrappers live in Node. The split is pragmatic: Python handles data shaping, while the Node scripts provide small direct-posting entry points for cron.

## Why the current design exists

The repo is optimized for scheduled automation on one host, not for an always-on service.

That explains:
- command-line entry points instead of a web service
- JSON files instead of persistent infra
- small helper scripts for specific delivery/reporting paths
- conservative error handling that leaves failures visible in command output

## Boundaries

This repo is for passive monitoring and reporting.

It should:
- detect and package relevant X posts
- maintain enough local state for dedupe and short-window analysis
- hand off alerts, summaries, and digests to downstream channels

It should not:
- execute engagement actions on X
- act as a long-term analytics warehouse
- assume direct X API access exists inside the repo itself

## ADRs

See `docs/decisions/0001-file-based-monitor-pipeline.md` for the primary architecture decision recorded during this refresh.
