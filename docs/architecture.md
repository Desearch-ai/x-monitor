# Architecture

## Overview

X Monitor is a small file-based pipeline for passive X/Twitter monitoring.

It has one primary collector, then several downstream consumers:
- `monitor.py` collects and normalizes tweet data
- `post-to-discord.cjs` publishes fresh alerts
- `summarize.py` produces an LLM summary from the rolling cache
- `daily_stats.py` produces a time-window activity report
- `feishu_digest.py` appends a digest of new tweets to Feishu

The design favors simple local files over queues or databases.

## Repo Structure

- `monitor.py`: main polling and deduplication entry point
- `summarize.py`: OpenRouter-backed summarizer for the rolling tweet window
- `run-summarizer.cjs`: lightweight wrapper for `summarize.py`
- `daily_stats.py`: grouped stats report built from cached tweets
- `post-daily-stats.cjs`: posts the stats report to Discord
- `post-to-discord.cjs`: posts queued alerts from `pending_alerts.json`
- `feishu_digest.py`: Feishu doc writer for newly discovered tweets
- `config.json`: monitored sources, filters, Discord settings, Feishu settings
- `CRON_PROMPT.md`: intended cron-agent behavior and output expectations
- `.env.example`: expected secret names

Generated runtime files:
- `state.json`
- `tweets_window.json`
- `pending_alerts.json`

## Monitoring Flow

### 1. Collection

`monitor.py` reads `config.json` and loops over two source classes:
- accounts, fetched with `x_timeline`
- keywords, fetched with the generic X search command

Both routes use the shared Desearch script at:
`~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`

### 2. Normalization

Each tweet is enriched with monitor metadata:
- `_monitor_source`
- `_monitor_category`
- `_monitor_importance`
- `_monitor_context`

Twitter-style date strings are converted to ISO timestamps when possible so the rolling window can prune safely.

### 3. Deduplication

Seen IDs are tracked per source key, for example:
- `timeline:const`
- `keyword:#desearch`

The monitor keeps the latest 500 IDs per source. This avoids re-alerting old content while keeping the state file small.

### 4. Fan-out to file-based outputs

The collector produces two different outputs for two different jobs:
- `pending_alerts.json`: only newly discovered tweets, for immediate alerting
- `tweets_window.json`: all fetched tweets in the last 24 hours, for analysis and reporting

That split is the core repo design decision. Alerts need only fresh items, while summaries and stats need a broader rolling context.

### 5. Consumers

#### Discord alert poster
`post-to-discord.cjs` reads `pending_alerts.json`, groups tweets by `_monitor_category`, posts a single message, then clears the file on success.

#### Summarizer
`summarize.py` reads `tweets_window.json`, filters by an hour window, formats up to 50 tweets for an LLM, and optionally posts the summary to Discord.

#### Daily stats
`daily_stats.py` reads the same rolling window, groups by source and category, then prints a digestible stats message.

#### Feishu digest
`feishu_digest.py` expects the JSON output of `monitor.py` and appends the newly discovered tweets into a configured Feishu doc.

## Design Decisions

### File-based state instead of a database
This repo stores state in JSON files next to the scripts. That keeps cron execution simple and makes debugging easy because every intermediate artifact is inspectable.

### Separate fresh-alert and rolling-window outputs
Immediate alerting and later analysis have different needs. `pending_alerts.json` supports low-noise alert delivery, while `tweets_window.json` preserves enough history for summaries and 24h stats.

### Shared search backend
The monitor does not talk to X directly. It delegates timeline and search calls to the shared Desearch X search script. That keeps search integration centralized, but also means this repo depends on that external script path existing.

### Mixed Python and Node tooling
Data collection and analysis live in Python. Discord posting wrappers live in Node. The split is practical, not architectural purity: Python handles the monitor and LLM flow, while the Node scripts provide quick direct posting paths.

## Why the current design exists

The repo is optimized for cron-driven automation, not for an always-on service.

That explains several choices:
- command-line entry points instead of a web service
- JSON files instead of persistent infra
- separate helper scripts for posting and reporting
- conservative error handling that keeps partial failures visible in output

## Boundaries

This repo is for passive monitoring and reporting.

It should:
- detect and package relevant posts
- maintain enough local state for dedupe and short-term analysis
- hand off alerts or summaries to downstream channels

It should not:
- execute engagement actions on X
- act as a long-term analytics warehouse
- assume direct X API access exists inside the repo itself

## ADRs

See `docs/decisions/0001-file-based-monitor-pipeline.md` for the main architectural decision recorded during this docs refresh.
