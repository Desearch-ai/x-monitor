# X Monitor Cron Prompt

This is used by the cron agentTurn job. Run every 2 hours.

## Task

You are the X (Twitter) monitor for Desearch AI. Run the monitor script and process results.

### Step 1: Run the monitor

```bash
cd /Users/giga/.openclaw/workspace/x-monitor && DESEARCH_API_KEY=$DESEARCH_API_KEY uv run python monitor.py
```

If DESEARCH_API_KEY is not set, check /Users/giga/.openclaw/workspace/x-monitor/config.json for notes, then stop with a warning.

### Step 2: Process output

If `total_new == 0`: do nothing, exit silently.

If there are new tweets, group them by category and post to Discord channel **1477727527618347340** (#x-alerts).

### Step 3: Discord formatting

Post one message per category batch (not one per tweet). Use this format:

**For HIGH importance:**
```
🔔 **X Monitor** | [Category Icon] [Category Name]

@username · [engagement: ❤️N 🔄N]
"tweet text (max 280 chars)"
🔗 https://x.com/username/status/id
[context note if helpful]
```

Category icons:
- 🦾 bittensor
- 🔍 desearch / brand  
- 🤝 influencer
- 🏆 competitor
- ⚙️ system
- 🤖 ai
- #️⃣ keyword/subnet

**For NORMAL importance:** batch 3-5 tweets per message, more compact.

### Step 4: Save to Feishu (if doc_token configured)

Read config.json to get feishu.doc_token. If empty, skip this step.

If configured, append a brief summary to the doc:
- Date/time
- New tweets count  
- Short summary per category

### Step 5: Handle errors

If there are errors in the output, log one compact message to Discord mentioning which sources failed.
