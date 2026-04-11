# X Monitor Known Issues

These are source-verified issues from the current repo state.

## 1. `filters.skip_retweets_for_normal` is dead config

**Status:** ❌ broken

`config.json` exposes `filters.skip_retweets_for_normal`, but no source file reads it.

Retweet filtering for normal-importance accounts never executes regardless of the config value.

## 2. `post-to-discord.cjs` removal is URL-only

**Status:** ⚠️ degraded

After a successful Discord chunk post, items are removed from `pending_alerts.json` by URL only. If two items share a URL or a tweet lacks a stable URL, the queue can remove the wrong item or preserve the wrong one after a partial failure.

## 3. Feishu digest username extraction degrades

**Status:** ⚠️ degraded

`feishu_digest.py` uses `tweet.get("username") or tweet.get("author_id") or "unknown"`. Payloads that only include `user.username` can produce degraded labels or broken `x.com` links.

## 4. Cron prompt points at old workspace path

**Status:** ⚠️ degraded

`CRON_PROMPT.md` tells automation to run from `/Users/giga/.openclaw/workspace/x-monitor`, but the repo lives at `/Users/giga/projects/openclaw/x-monitor`. The described flow is correct but the path is stale.

## 5. Some helper-script tests expect exports that don't exist

**Status:** ⚠️ degraded

- `tests/test_post_to_discord.cjs` expects a `removeChunkTweets` export not present in production code
- `tests/test_post_daily_stats.cjs` expects a `getChannelId` export not present in production code

Tests and helper scripts are out of sync on these specific exports.

## 6. X search dependency lives outside the repo

**Status:** ⚠️ degraded

`monitor.py` shells out to `~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`. A fresh clone is not self-contained — if that shared script is missing or changed incompatibly, monitoring fails.
