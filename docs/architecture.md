# X Monitor Architecture

## Overview

X Monitor is a file-backed pipeline that polls X/Twitter for monitored accounts and keywords, deduplicates results, normalizes them with rich routing metadata, and queues output for Discord delivery.

The system has two layers:
1. **Collection** (`monitor.py`) — fetches tweets, normalizes with lane metadata, writes file artifacts
2. **Delivery** (helper scripts) — reads artifacts and posts to Discord, Feishu, or other destinations

## Dual-Lane Signal Model (v2)

v2 introduces a dual-lane routing model. Each watched account or keyword now maps to:

- a **bucket** (e.g., `bittensor`, `desearch`, `influencer`) — the watchlist category
- one or more **lanes** (e.g., `founder`, `brand`) — routing destinations for downstream tools
- a **route_hint** per lane (e.g., `x-engage/founder`) — tells downstream consumers where to route the signal

The `founder` lane captures signals relevant to the founder personally. The `brand` lane captures signals relevant to Desearch as a product and company.

## Repository structure

```
x-monitor/
  monitor.py              # v2 collection entry point
  config.json            # lanes, accounts, keywords, filters
  state.json             # per-source seen-ID dedupe store
  tweets_window.json     # rolling 24h tweet cache
  pending_alerts.json    # Discord delivery queue
  tests/
    test_monitor.py     # v2 lane metadata + dedupe tests
  docs/
    architecture.md      # this file
    features.md         # feature inventory
    known-issues.md     # current limitations
```

## Collection flow

```
config.json (lanes + accounts + keywords)
        |
        v
   monitor.py
        |
        +--> state.json              (per-source dedupe, 500 IDs/source)
        +--> tweets_window.json       (all tweets, rolling 24h, deduplicated)
        +--> pending_alerts.json      (new unseen tweets for Discord)
        |
        +--> stdout JSON ------------+--> feishu_digest.py
                                      +--> summarize.py
                                      +--> saved output

pending_alerts.json --> post-to-discord.cjs --> Discord #x-alerts
```

## Normalization

`normalize_tweet()` adds these v2 metadata fields:

| Field | Description |
|---|---|
| `_monitor_source` | Source tag: `account:<username>` or `keyword:<query>` |
| `_monitor_bucket` | Watchlist bucket (e.g., `bittensor`, `desearch`) |
| `_monitor_category` | Backward compat; mirrors `_monitor_bucket` |
| `_monitor_importance` | Importance level (`high`/`normal`) |
| `_monitor_context` | Context string from config |
| `_monitor_lanes` | List of lanes the tweet belongs to (e.g., `["founder", "brand"]`) |
| `_monitor_route_hints` | List of route_hint strings per lane |

## Lane resolution

Each tweet is tagged with lanes through a two-step lookup:

1. **Explicit lanes** — if an account or keyword entry in config has a `lanes` array, use it directly
2. **Bucket fallback** — if no explicit lanes, look up which lanes include the tweet's bucket in their `buckets` list

```
account: { "username": "const", "bucket": "bittensor", "lanes": ["founder", "brand"] }
  → lanes = ["founder", "brand"]

keyword: { "query": "#desearch", "bucket": "desearch" }
  bucket "desearch" is in brand lane's buckets list
  → lanes = ["brand"]
```

Route hints come from the lane definitions:

```
lane: { "id": "founder", "buckets": [...], "route_hint": "x-engage/founder" }
  → route_hint = "x-engage/founder"
```

## CLI options

```bash
DESEARCH_API_KEY=xxx uv run python monitor.py --dry-run   # no state write
DESEARCH_API_KEY=xxx uv run python monitor.py --reset     # rebuild dedupe state
DESEARCH_API_KEY=xxx uv run python monitor.py --lane-filter founder  # only founder tweets
```

## Design decisions

### File-based pipeline over service stack
JSON artifacts (state, window, alerts) serve as handoff points between collection and delivery. This enables local inspection, retry, and independent rerunning of each stage without a database.

### Lanes are orthogonal to buckets
Buckets drive what we watch; lanes drive where signals go. One account can be in `bittensor` bucket but route to both `founder` and `brand` lanes, because the founder (const) is also central to Bittensor.

### Route hints instead of hardcoded routing
`route_hint` strings (e.g., `x-engage/founder`) let downstream tools decide what to do with a signal without the monitor knowing the details. x-engage and Mission Control can interpret these hints however they like.

### Category field preserved for backward compatibility
`_monitor_category` is set equal to `_monitor_bucket` so existing consumers that read `_monitor_category` continue to work unchanged.
