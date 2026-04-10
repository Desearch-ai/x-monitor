# Known Issues

These are current limitations verified from source. They reflect real behavior gaps, not wishlist items.

## 1. `CRON_PROMPT.md` points at an outdated repo path

`CRON_PROMPT.md` tells the cron agent to run from `/Users/giga/.openclaw/workspace/x-monitor`, while the active project repo for this task lives at `/Users/giga/projects/openclaw/x-monitor`.

Why unresolved:
- the prompt is documentation for the cron agent, not the runtime code itself
- updating it safely should be coordinated with the live cron configuration so the prompt and scheduler stay aligned

Impact:
- anyone following the prompt literally can run the wrong checkout or assume the repo still lives in the workspace mirror

## 2. `pending_alerts.json` is not durable across monitor reruns after a failed delivery

`post-to-discord.cjs` preserves `pending_alerts.json` when a Discord send fails, but `monitor.py` overwrites `pending_alerts.json` with only the latest `new_tweets` on the next successful monitor run.

Why unresolved:
- the repo currently assumes a simple monitor-then-post cadence
- preserving unsent items correctly requires a deliberate merge policy rather than a one-line docs tweak

Impact:
- if Discord posting fails and the monitor runs again before the old queue is retried, previously unsent alerts can be lost

## 3. Cron prompt formatting does not match the actual Discord poster

`CRON_PROMPT.md` describes posting category batches with different high-vs-normal formatting rules. `post-to-discord.cjs` actually builds one grouped message for all queued tweets.

Why unresolved:
- the simpler grouped poster is operationally cheap and easy to run
- the prompt and implementation have drifted, and the preferred output format has not been re-decided yet

Impact:
- operators should not assume the current live alert renderer follows the richer prompt format

## 4. Feishu digest username extraction is brittle

`feishu_digest.py` prefers `tweet.get("username")` or `author_id`, while the rest of the repo commonly reads usernames from `tweet["user"]["username"]`.

Why unresolved:
- Feishu export is optional and disabled by default because `config.json` ships with empty Feishu credentials
- the path likely sees less production traffic than Discord posting

Impact:
- Feishu digests can show weaker author naming and links than the Discord and summary flows

## 5. `post-daily-stats.cjs` is less portable than the other Discord poster

`post-daily-stats.cjs` reads the bot token directly from `/Users/giga/.openclaw/openclaw.json` and hardcodes the Discord channel, while `post-to-discord.cjs` can also use `DISCORD_BOT_TOKEN` from the environment.

Why unresolved:
- the script appears to be optimized for the OpenClaw host where it currently runs
- broadening token and channel lookup is a real behavior change that should be tested with the cron environment

Impact:
- daily stats posting is harder to reuse outside the current host setup

## 6. The repo depends on an external shared search script path

`monitor.py` shells out to `~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`.

Why unresolved:
- centralizing X search access in one shared script avoids duplicating search integration logic
- decoupling this repo would require vendoring or packaging that dependency

Impact:
- the monitor will fail if that shared script moves, is removed, or changes output shape unexpectedly
