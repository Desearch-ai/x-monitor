# X Monitor Known Issues

These are source-verified issues from the current repo state. They reflect real behavior gaps, not wishlist items.

## 1. `filters.skip_retweets_for_normal` is dead config

**Status:** ❌ broken

`config.json` exposes `filters.skip_retweets_for_normal`, but no source file reads it.

Retweet filtering for normal-importance accounts never executes regardless of the config value.

## 2. `post-to-discord.cjs` removal is URL-only

**Status:** ⚠️ degraded

After a successful Discord chunk post, items are removed from `pending_alerts.json` by URL only. If two items share a URL or a tweet lacks a stable URL, the queue can remove the wrong item or preserve the wrong one after a partial failure.

## 3. `pending_alerts.json` is not durable across monitor reruns after a failed delivery

**Status:** ⚠️ degraded

`post-to-discord.cjs` preserves `pending_alerts.json` when a Discord send fails, but `monitor.py` overwrites `pending_alerts.json` with only the latest `new_tweets` on the next successful monitor run.

Impact: if Discord posting fails and the monitor runs again before the old queue is retried, previously unsent alerts can be lost.

## 4. Feishu digest username extraction degrades

**Status:** ⚠️ degraded

`feishu_digest.py` uses `tweet.get("username") or tweet.get("author_id") or "unknown"`. Payloads that only include `user.username` can produce degraded labels or broken `x.com` links.

Feishu export is optional and disabled by default because `config.json` ships with empty Feishu credentials.

## 5. Cron prompt points at old workspace path

**Status:** ⚠️ degraded

`CRON_PROMPT.md` tells automation to run from `/Users/giga/.openclaw/workspace/x-monitor`, but the repo lives at `/Users/giga/projects/openclaw/x-monitor`. The described flow is correct but the path is stale.

## 6. Cron prompt formatting does not match the actual Discord poster

**Status:** ⚠️ degraded

`CRON_PROMPT.md` describes posting category batches with different high-vs-normal formatting rules. `post-to-discord.cjs` actually builds one grouped message for all queued tweets.

Impact: operators should not assume the current live alert renderer follows the richer prompt format.

## 7. Some helper-script tests expect exports that don't exist

**Status:** ⚠️ degraded

- `tests/test_post_to_discord.cjs` expects a `removeChunkTweets` export not present in production code
- `tests/test_post_daily_stats.cjs` expects a `getChannelId` export not present in production code

Tests and helper scripts are out of sync on these specific exports.

## 8. `post-daily-stats.cjs` is less portable than the other Discord poster

**Status:** ⚠️ degraded

`post-daily-stats.cjs` reads the bot token directly from `/Users/giga/.openclaw/openclaw.json` and hardcodes the Discord channel, while `post-to-discord.cjs` can also use `DISCORD_BOT_TOKEN` from the environment.

Impact: daily stats posting is harder to reuse outside the current host setup.

## 9. X search dependency lives outside the repo

**Status:** ⚠️ degraded

`monitor.py` shells out to `~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`. A fresh clone is not self-contained — if that shared script is missing or changed incompatibly, monitoring fails.
