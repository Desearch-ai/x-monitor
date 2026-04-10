# ADR 0001: File-based monitor pipeline

- Status: Accepted
- Date: 2026-04-10

## Context

X Monitor runs as a cron-oriented passive monitoring repo. It needs to:
- collect posts from monitored accounts and keyword searches
- avoid re-alerting the same content
- support both immediate alerts and short-window analysis
- stay easy to inspect and debug on a single host

## Decision

Keep the pipeline file-based:
- use `state.json` for deduplication state
- use `pending_alerts.json` for newly discovered alert payloads
- use `tweets_window.json` for a rolling 24 hour analysis window

## Alternatives considered

### Use a database or queue
Pros:
- stronger durability and richer querying
- clearer separation between producers and consumers

Cons:
- more infrastructure than this cron-driven repo needs
- harder local debugging than inspecting JSON files directly

### Keep only one output file
Pros:
- fewer moving parts

Cons:
- immediate alerts and rolling analysis have different data-retention needs
- consumers would need more logic to decide what is fresh versus historical

## Consequences

Positive:
- simple cron deployment
- easy inspection of intermediate artifacts
- low operational overhead

Negative:
- consumers must coordinate through local files
- concurrent runs would need care to avoid clobbering state
- the repo remains tied to one-machine execution assumptions
