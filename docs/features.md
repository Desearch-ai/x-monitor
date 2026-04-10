# Features

Status legend:
- ✅ working
- ⚠️ degraded
- ❌ broken
- 🚧 in progress

## Monitoring Core

| Feature | Status | Notes |
| --- | --- | --- |
| Account timeline monitoring | ✅ working | `monitor.py` fetches each configured account with `x_timeline` and tracks seen IDs per source key such as `timeline:const`. |
| Keyword monitoring | ✅ working | Keyword searches run through the Desearch X search path and use separate source keys such as `keyword:#desearch`. |
| Per-source deduplication | ✅ working | `state.json` stores seen IDs per source and trims each list to the latest 500 IDs. |
| Sliding 24 hour tweet window | ✅ working | All fetched tweets are merged into `tweets_window.json` and pruned by timestamp in `save_window()`. |
| New alert queue generation | ✅ working | Newly detected tweets are written to `pending_alerts.json` for the Discord poster. |
| Metadata tagging | ✅ working | Tweets are enriched with `_monitor_source`, `_monitor_category`, `_monitor_importance`, and `_monitor_context`. |
| Date normalization | ✅ working | Twitter-style `created_at` strings are converted to ISO 8601 when possible for safer window pruning. |

## Delivery and Reporting

| Feature | Status | Notes |
| --- | --- | --- |
| Direct Discord alert posting | ✅ working | `post-to-discord.cjs` reads `pending_alerts.json`, builds one grouped message, posts it, then clears the queue on success. |
| LLM summary generation | ✅ working | `summarize.py` reads the rolling window, formats up to 50 tweets, calls OpenRouter, and can post the summary to Discord. |
| Daily stats reporting | ✅ working | `daily_stats.py` builds grouped source and category stats from `tweets_window.json`. |
| Feishu digest export | ⚠️ degraded | The path exists and skips cleanly when Feishu config is empty, but it depends on external Feishu app setup and has brittle username handling. |

## Filters and Controls

| Feature | Status | Notes |
| --- | --- | --- |
| High-importance passthrough | ✅ working | Accounts marked `importance: high` bypass engagement filtering. |
| Normal-importance engagement filtering | ✅ working | Lower-priority accounts use `normal_importance_min_likes` plus a retweet threshold in `is_important_enough()`. |
| Reply skipping | ✅ working | Controlled by `filters.skip_replies`; current config sets it to `false`, so replies are currently included. |
| Per-account retweet control | ✅ working | Each account can opt in to retweets with `include_retweets`. |
| Dry-run mode | ✅ working | `monitor.py --dry-run` prints monitor output without writing runtime files. |
| Reset mode | ✅ working | `monitor.py --reset` ignores saved seen-state for that run. |

## Operational Fit

| Feature | Status | Notes |
| --- | --- | --- |
| Config-driven source inventory | ✅ working | Accounts, keywords, channel settings, and filters come from `config.json`. |
| Source-level error collection | ✅ working | Failed account or keyword fetches are added to the `errors` array in monitor output. |
| 2 hour cron workflow | ⚠️ degraded | The cadence is documented, but `CRON_PROMPT.md` still points at an outdated repo path and describes richer category formatting than the live poster implements. |
| Alert queue durability across failed delivery | ⚠️ degraded | `post-to-discord.cjs` preserves the queue on send failure, but a later `monitor.py` run overwrites `pending_alerts.json` instead of merging unsent items. |
| Standard docs set | 🚧 in progress | This refresh establishes the standard docs set and records the current operational mismatches instead of hiding them. |
