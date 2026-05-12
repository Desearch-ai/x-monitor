# X Monitor

Automated X/Twitter monitoring for Desearch AI. This repo polls a fixed set of X accounts and search queries, deduplicates newly seen posts, stores a rolling 24 hour cache, and prepares alerts for Discord `#x-monitor`.

## Purpose

### Dual-lane model

The system routes signals through two lanes:

- **Founder Lane** — signals relevant to the founder personally (personal brand, interests, relationships)
- **Brand Lane** — signals relevant to Desearch as a product and company (brand mentions, subnet discussions, competitors)

Each watched account or keyword maps to a **bucket** and one or more **lanes**. Tweets are normalized with `_monitor_lanes` and `_monitor_route_hints` so downstream tools can route them appropriately.

X Monitor is a standalone passive signal intelligence service/UI for the Desearch social workflow. It is responsible for:
- polling configured X timelines and search queries
- letting operators/agents inspect and safely edit local watchlists
- deduplicating posts against local seen-state
- saving a 24 hour rolling window for summaries and stats
- exposing normalized Signal records for Socialos and agents
- queuing newly detected posts for Discord delivery
- optionally exporting a digest to Feishu

It does not perform active engagement actions such as replies, likes, follows, account authentication, approvals, scheduling, or publishing. Socialos owns publishing/account auth/approval/execution, and `x-engage` owns approval-aware execution runtime code.

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
- `X_MONITOR_RUNTIME_PATH` when you want to point at an explicit Social OS runtime export instead of the default `~/projects/desearch/social-os/runtime/x-monitor.json` location
- Feishu app credentials in `config.json` for digest export fallback

### Setup

1. Copy `.env.example` into a local `.env`, or export the same keys in your shell.
2. In normal operation, let Social OS manage the runtime contract and expose either:
   - `~/projects/desearch/social-os/runtime/x-monitor.json`, or
   - an explicit path via `X_MONITOR_RUNTIME_PATH`.
3. Keep `config.json` only as an emergency local fallback when the managed runtime export is unavailable.
4. Run a dry run before enabling cron.

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

### 5. Start the standalone watchlist/signals UI/API

```bash
cd ~/projects/openclaw/x-monitor
uv run python x_monitor_api.py --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766` for the operator UI. The local API is additive: page loads and API reads do **not** trigger X fetches or mutate monitor state. Watchlist writes persist to `config.json` by default, or to an explicit dev path with:

```bash
X_MONITOR_ADMIN_CONFIG_PATH=/tmp/x-monitor-config.json uv run python x_monitor_api.py
```

Routes:

| Route | Method | Purpose |
|---|---:|---|
| `/` | GET | Polished operator console with command-doc sections, watchlist table, searchable signal history, and route provenance |
| `/api/health` | GET | Local health/version check |
| `/api/watchlist` | GET | Return accounts, keywords, mentions, lists, route hints/labels, lane source, counts, and agent setup |
| `/api/watchlist` | POST | Add one `account`, `keyword`, `mention`, or `list` item |
| `/api/watchlist/{id}` | PATCH | Update bucket/lanes/importance/context/value for one item |
| `/api/watchlist/{id}` | DELETE | Remove one item |
| `/api/signals?limit=50` | GET | Return latest normalized Signal records from `tweets_window.json` + `pending_alerts.json`, including additive provenance fields |

The API rejects publishing/account-auth fields such as credentials, sessions, approvals, schedules, and publish/execution controls. Those controls belong in Socialos, not X-Monitor.

Operator route labels are shown as `socialos/*`. Existing `x-engage/*` route hints remain backward-compatible internal aliases and are explicitly labeled as legacy in the UI/API docs.

### 6. Post queued alerts to Discord

```bash
node post-to-discord.cjs
```

### 7. Enable cron

Cron job ID: `cf7191f8-4097-4cc0-9c90-64a86c663366` — runs every 2 hours for passive ingestion only. Live X actions remain in `x-engage` and stay behind manual approval.

## Runtime Behavior

### Monitor flow

`monitor.py` is the collector. On each run it:
1. loads `.env`
2. loads the managed Social OS runtime contract/projection first, then falls back to local `config.json` only if no managed runtime export is available
3. calls the shared Desearch X search script for timelines and keyword searches
4. tags tweets with monitor metadata such as source, bucket, lanes, route hints, importance, and context
5. skips already seen tweet IDs using `state.json`
6. writes all fetched tweets into `tweets_window.json`, pruned to the last 24 hours
7. writes newly detected tweets into `pending_alerts.json` using atomic replace semantics
8. exits with a compact `skipped` payload instead of overlapping when another run already holds `.monitor.lock`
9. emits best-effort Social OS runtime telemetry (`service = x-monitor`) for started, finished, error, and lock-skipped lifecycle states when Supabase credentials are configured
10. prints a JSON payload with stats, new tweets, source-specific errors, and whether Social OS telemetry insertion succeeded

### Cron behavior

The intended production cadence is:
- every 2 hours: `X_MONITOR_RUN_MODE=cron monitor.py` then `post-to-discord.cjs`
- every 4 hours: `x-engage/run-engage.sh analyze` (separate repo)
- manual approval review before any live action
- optional/manual live execution only after approval

Manual runs default to `mode = manual`; `--dry-run` reports `mode = dry-run`; cron can set `X_MONITOR_RUN_MODE=cron` so Social OS can separate scheduled collection from ad-hoc operator checks. `CRON_PROMPT.md` documents the passive monitor job. The monitor path now uses `.monitor.lock` plus a shared `.pending-alerts.lock` so collection and Discord posting cannot corrupt or resurrect queued alerts.

### Social OS runtime telemetry

When `SOCIAL_OS_SUPABASE_URL` plus a supported Supabase key are present, `monitor.py` writes compact rows to the existing `social_runtime_events` table with `service = x-monitor`. Telemetry is best-effort and fail-soft: insert failures are logged to stderr and never block collection, state updates, pending-alert writes, or normal JSON output.

Each run emits operator-oriented lifecycle events:
- `started`: run ID, mode, config fingerprint, monitored accounts/keywords, lane bucket summary, route hints, importance, and watchlist context
- `finished`: started/finished timestamps, status, duration, accounts/keywords checked, total fetched, new/deduped/emitted/queued counts, lanes routed, pending-alert count when applicable, source-specific errors, and representative emitted signal metadata
- `error`: same run context plus the fail-soft error payload before the exception exits
- `skipped`: lock-skip visibility when another non-dry-run collection already holds `.monitor.lock`

Representative signal telemetry intentionally includes only routing metadata plus tweet ID/url/timestamp when available. It does not dump full tweet payloads or text into Social OS runtime events. If credentials are absent, the monitor still runs and stderr clearly prints `Social OS telemetry skipped: missing Supabase URL/key ...`.

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
- `SOCIAL_OS_SUPABASE_URL` + `SOCIAL_OS_SUPABASE_ANON_KEY`: optional live Social OS Supabase runtime source and runtime telemetry target. `SOCIAL_OS_SUPABASE_KEY`, `SUPABASE_URL`/`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` aliases are also accepted. If no URL/key pair is configured, runtime telemetry is skipped with a clear stderr message and collection continues.
- `X_MONITOR_RUN_MODE`: optional `cron` or `manual` telemetry mode override for non-dry-runs; `--dry-run` always reports `dry-run`
- `X_MONITOR_RUNTIME_PATH`: optional explicit path to a managed Social OS runtime JSON payload

### Managed runtime is the default

`monitor.py` now resolves watchlists, bucket→lane routing, and route hints from the live active Social OS runtime row first, then managed runtime files before touching repo-local config. It accepts any of these JSON payloads:

- the full Social OS runtime contract (`lanes`, `watchlists`, `services`, `defaults`)
- a `social_runtime_configs` row export with the contract under `config`
- a direct x-monitor projection payload (`lanes`, `accounts`, `keywords`, `filters`, `discord`)

Lookup order:
1. Active Supabase `social_runtime_configs` row when a supported Supabase URL/key env pair is present
2. `X_MONITOR_RUNTIME_PATH`
3. `SOCIAL_OS_X_MONITOR_PATH`
4. `SOCIAL_OS_RUNTIME_PATH`
5. `~/projects/desearch/social-os/runtime/x-monitor.json`
6. `~/projects/desearch/social-os/runtime/social-os-x-runtime.json`
7. repo-local `config.json` fallback

If an explicit managed-runtime env path is set but missing, the monitor exits instead of silently drifting back to stale local routing.

## Config shape (v2 fallback / legacy local recovery)

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
| `config.json` | Emergency fallback for lanes, accounts, keywords, and Discord/Feishu settings |
| `monitor.py` | Collection entry point with dual-lane normalization |
| `state.json` | Per-source seen-ID dedupe store |
| `tweets_window.json` | Rolling 24h tweet cache |
| `pending_alerts.json` | Discord delivery queue |

## Standalone Signal contract v1

`GET /api/signals` returns normalized Signal records for Socialos/agents without exposing raw full tweet payloads as the primary contract. Each Signal includes:

| Field | Meaning |
|---|---|
| `id` | Stable X-Monitor signal id, derived from platform/source/external id/url |
| `platform` | Always `x` for this service |
| `source` | Matched monitor source such as `keyword:sn22 bittensor` or `account:const` |
| `source_url` | Canonical X post URL when available |
| `external_id` | X post/tweet id when available |
| `author` | `{ handle, name?, id? }` when present in source data |
| `content_snippet` | Compacted text snippet capped for operator display |
| `matched_terms` | Keyword/mention terms that matched the watchlist |
| `matched_accounts` | Account handles that matched the watchlist |
| `route_hints` | Backward-compatible downstream route hints resolved from lanes; `x-engage/*` values are legacy internal aliases |
| `provenance` | Additive route provenance: matched watchlist items, bucket, lanes, lane source (`explicit_item_config`, `bucket_fallback`, or `signal_metadata`), and route labels |
| `route_explanation` | Human-readable explanation of what matched, how lanes were assigned, and how route hints map to Socialos labels |
| `score` | 0-100 local relevance score from importance + engagement - risk |
| `why_now` / `reason` | Human-readable match reason/context |
| `risk_flags` | Local risk qualifiers such as `negative_filter_match`, `reply`, `retweet`, `possibly_sensitive` |
| `observed_at` | When X-Monitor observed or normalized the record |
| `created_at` | Source post creation timestamp when available |

Example:

```json
{
  "id": "a2b5f7d3e0c9a11844d200ff",
  "platform": "x",
  "source": "keyword:sn22 bittensor",
  "source_url": "https://x.com/builderdao/status/1888",
  "external_id": "1888",
  "author": { "handle": "builderdao" },
  "content_snippet": "Desearch SN22 launch signal from builder",
  "matched_terms": ["sn22 bittensor"],
  "matched_accounts": [],
  "route_hints": ["x-engage/brand"],
  "provenance": {
    "matched_watchlist_items": [
      {"kind": "keyword", "value": "sn22 bittensor", "bucket": "subnet", "lanes": ["brand"], "lane_source": "explicit_item_config"}
    ],
    "bucket": "subnet",
    "lanes": ["brand"],
    "lane_source": "explicit_item_config",
    "route_hints": [
      {"hint": "x-engage/brand", "display_label": "socialos/brand", "lane": "brand", "source": "lane_config", "legacy_internal_alias": true}
    ]
  },
  "route_explanation": "Matched keyword:sn22 bittensor; bucket subnet assigned lanes brand via explicit lanes from the matched watchlist item config; route labels socialos/brand. Legacy x-engage/* hints are internal aliases for Socialos routes.",
  "score": 100,
  "why_now": "Matched keyword:sn22 bittensor watchlist; Subnet 22 mentions",
  "reason": "Matched keyword:sn22 bittensor watchlist; Subnet 22 mentions",
  "risk_flags": [],
  "observed_at": "2026-05-12T00:02:00+00:00",
  "created_at": "2026-05-12T00:01:00+00:00"
}
```

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

Feishu export is optional. To enable it, populate these `config.json` fallback keys:
- `feishu.doc_token`
- `feishu.app_id`
- `feishu.app_secret`

When those values are present, `feishu_digest.py` can append a category-grouped digest of newly detected tweets to the target document.

## Architecture

See `docs/architecture.md` for the dual-lane pipeline diagram and design decisions.
See `docs/features.md` for the feature inventory.
See `docs/known-issues.md` for current limitations.
