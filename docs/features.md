# Features

Status legend:
- ✅ working
- ⚠️ degraded
- ❌ broken
- 🚧 in progress

## Monitoring Core

| Feature | Status | Notes |
| --- | --- | --- |
| Account timeline monitoring | ✅ working | `monitor.py` fetches each configured account with `x_timeline` and keeps per-source seen IDs. |
| Keyword monitoring | ✅ working | Keyword searches use the Desearch X search path and are tracked separately from timelines. |
| Deduplication by source | ✅ working | Seen tweet IDs are stored under source keys in `state.json`, with the last 500 IDs retained per source. |
| Sliding 24 hour tweet window | ✅ working | All fetched tweets are merged into `tweets_window.json` and pruned by timestamp. |
| New-alert queue generation | ✅ working | New tweets are written to `pending_alerts.json` for later posting. |
| Context tagging on tweets | ✅ working | Monitor metadata includes source, category, importance, and human context. |

## Delivery and Reporting

| Feature | Status | Notes |
| --- | --- | --- |
| Direct grouped Discord alerts | ✅ working | `post-to-discord.cjs` reads `pending_alerts.json`, posts one grouped message, and clears the file after success. |
| LLM-based summary generation | ✅ working | `summarize.py` reads `tweets_window.json`, sends up to 50 tweets to OpenRouter, and can post the result to Discord. |
| Daily stats report | ✅ working | `daily_stats.py` builds a grouped 24h report from the rolling window. |
| Feishu digest export | ⚠️ degraded | Code path exists and skips cleanly when Feishu config is empty, but it depends on valid Feishu app setup and has username formatting limitations described in `docs/known-issues.md`. |

## Filtering and Controls

| Feature | Status | Notes |
| --- | --- | --- |
| High-importance account passthrough | ✅ working | High-importance accounts bypass engagement filtering. |
| Normal-importance engagement filtering | ✅ working | For normal accounts, `normal_importance_min_likes` and a retweet threshold are applied. |
| Reply skipping | ✅ working | Controlled by `filters.skip_replies` in `config.json`. Current config leaves reply skipping disabled. |
| Retweet inclusion per account | ✅ working | Each account can opt into retweets with `include_retweets`. |
| Reset mode | ✅ working | `monitor.py --reset` clears seen-state for the current run before refetching. |
| Dry-run mode | ✅ working | `monitor.py --dry-run` prints output without writing runtime files. |

## Operational Fit

| Feature | Status | Notes |
| --- | --- | --- |
| 2 hour cron workflow | ⚠️ degraded | The intended cadence is documented, but `CRON_PROMPT.md` still points at an outdated repo path and describes posting behavior more granularly than the Node poster actually implements. |
| Config-driven source inventory | ✅ working | Accounts, keywords, and channel settings come from `config.json`. |
| Error collection per failed source | ✅ working | Failed timeline or keyword calls are collected into the `errors` array in monitor output. |
| Repository-level docs set | 🚧 in progress | This docs refresh establishes the standard set, but operational mismatches remain and are documented rather than hidden. |
