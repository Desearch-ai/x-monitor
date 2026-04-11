# X Monitor Features

Status legend:
- ✅ working
- ⚠️ degraded
- ❌ broken
- 🚧 in progress

## Monitoring and collection

### ✅ Dual-lane account monitoring
- `monitor.py` fetches account timelines through the Desearch X search script.
- Each account now maps to a `bucket` and a `lanes` array.
- Tweets from monitored accounts are tagged with `_monitor_lanes` and `_monitor_route_hints`.

### ✅ Dual-lane keyword monitoring
- Keyword searches are similarly tagged with lane metadata.
- Direct mentions (`@desearch_ai`) and brand hashtags (`#desearch`) route to the `brand` lane.
- Community keywords (`sn22 bittensor`, `subnet22`) also route to `brand`.

### ✅ Per-source deduplication
- `state.json` stores seen IDs under keys like `timeline:<username>` and `keyword:<query>`.
- Keeps only the last 500 IDs per source to bound file growth.
- Rolling-window dedupe is preserved from v1.

### ✅ Rolling 24-hour tweet window
- All fetched tweets (seen or not) are merged into `tweets_window.json`.
- Deduplicated by tweet ID and pruned to 24 hours.
- Window powers downstream summarization and reporting.

### ✅ Lane-based output filtering
- `--lane-filter <founder|brand>` CLI option filters output to only tweets in that lane.
- Useful for downstream tools that only process one lane.

### ✅ Reply filtering (configurable)
- `filters.skip_replies` in config skips replies when enabled.
- Shipped config currently sets it to `false`.

### ✅ Per-account retweet inclusion
- `include_retweets: true` on `@desearch_ai` includes retweets from that account.
- Other accounts exclude retweets by default.

### ⚠️ Normal-account engagement thresholds exist but are dormant
- `is_important_enough()` applies `normal_importance_min_likes` for `importance: "normal"` accounts.
- All shipped accounts are `high`, so this path is not currently exercised.

### ❌ `filters.skip_retweets_for_normal` has no runtime effect
- The config key exists, but no source file reads it.

## Delivery and reporting

### ✅ Discord alert queueing
- Collection writes unseen tweets to `pending_alerts.json`.
- Posting happens separately via `post-to-discord.cjs`.

### ✅ Discord chunking
- Tweets are grouped by `_monitor_category` and split into sub-2000-character chunks.
- Each chunk is removed from the queue individually after successful posting.

### ✅ LLM summaries
- `summarize.py` reads `tweets_window.json`, formats up to 50 tweets, calls OpenRouter.

### ✅ Daily stats
- `daily_stats.py` produces deterministic grouped counts and top posts.

### ⚠️ Feishu digest username extraction is brittle
- Uses `tweet.get("username")` or `author_id`; payloads with only `user.username` degrade.

## Operations

### 🚧 Tests for v2 dual-lane behavior
- `tests/test_monitor.py` has 11 tests covering lane metadata, bucket mapping, and dedupe safety.
- Tests for helper scripts (`test_post_to_discord.cjs`, `test_summarize.py`) exist separately.

### ⚠️ Cron prompt references old repo path
- `CRON_PROMPT.md` points at `/Users/giga/.openclaw/workspace/x-monitor` instead of the actual path.

### ⚠️ Cron prompt formatting does not match the actual Discord poster
- `CRON_PROMPT.md` describes posting category batches with different high-vs-normal formatting rules.
- `post-to-discord.cjs` actually builds one grouped message for all queued tweets.
