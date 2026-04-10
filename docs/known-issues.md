# Known Issues

These are current repo limitations verified from source. They are real behavior gaps, not wishlist items.

## 1. `CRON_PROMPT.md` points at an outdated repo path

`CRON_PROMPT.md` tells the cron agent to run from `/Users/giga/.openclaw/workspace/x-monitor`, but the task context and active repo live at `/Users/giga/projects/openclaw/x-monitor`.

Why unresolved:
- the prompt is documentation, not the runtime entry point itself
- changing it safely should be coordinated with the actual cron job configuration so the prompt and job stay aligned

Impact:
- anyone following the prompt literally may run the wrong path or assume the repo still lives in the workspace mirror

## 2. Summarizer reads the wrong config key for Discord channel selection

`summarize.py` looks for `config.get("discord_channel_id", "1477727527618347340")`, but `config.json` stores the channel under `discord.alerts_channel`.

Why unresolved:
- the hardcoded fallback keeps posting working for the current alert channel, so this has not blocked production usage
- fixing it changes config behavior and should be tested together with cron

Impact:
- changing `config.json` alone does not re-route summarizer posts unless the code is updated too

## 3. Cron prompt formatting does not match the actual Discord poster

`CRON_PROMPT.md` describes posting one message per category with special handling for high vs normal importance. `post-to-discord.cjs` actually posts one grouped message covering all pending alerts.

Why unresolved:
- the simpler single-message poster is fast and operationally cheap
- the prompt and script have drifted and need a deliberate decision on the preferred output format

Impact:
- operators reading the prompt should not assume the current alert renderer follows the richer category-batch format

## 4. Feishu digest username extraction is brittle

`feishu_digest.py` formats tweet usernames using `tweet.get("username")` or `author_id`, while other scripts commonly read usernames from `tweet["user"]["username"]`.

Why unresolved:
- Feishu export is optional and disabled by default because `config.json` ships with empty Feishu credentials
- the path has likely seen less production traffic than Discord posting

Impact:
- Feishu digests can show incomplete author naming or weaker links than the Discord and summary flows

## 5. `post-daily-stats.cjs` uses a narrower token-loading path than other posters

`post-daily-stats.cjs` reads the Discord token directly from `/Users/giga/.openclaw/openclaw.json`, while `post-to-discord.cjs` can also use `DISCORD_BOT_TOKEN` from the environment.

Why unresolved:
- the script appears to target the OpenClaw host setup specifically
- broadening token lookup is a small change, but it still needs validation across local and cron environments

Impact:
- daily stats posting is less portable than the other delivery scripts

## 6. The repo depends on an external shared search script path

`monitor.py` shells out to `~/.openclaw/workspace/skills/desearch-x-search/scripts/desearch.py`.

Why unresolved:
- centralizing X search access in one shared script reduces duplicated integration code
- decoupling this repo would require packaging or vendoring that dependency

Impact:
- the monitor will fail if that shared script moves, is removed, or diverges unexpectedly from the output shape this repo expects
