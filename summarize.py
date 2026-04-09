#!/usr/bin/env python3
"""
X Monitor Summarizer — Reads tweets_window.json (sliding 24h window),
filters to the requested time window, sends to OpenRouter for analysis,
and posts the summary to Discord #x-alerts.

Usage:
    python3 summarize.py                  # summarize last 4h (default)
    python3 summarize.py --hours 24       # summarize last 24h
    python3 summarize.py --dry-run        # print summary, don't post to Discord
    python3 summarize.py --dry-run --hours 24
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

SCRIPT_DIR = Path(__file__).parent
WINDOW_FILE = SCRIPT_DIR / "tweets_window.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemma-3-4b-it:free"


def load_env():
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def load_window() -> list:
    """Load tweets_window.json — the sliding 24h tweet window."""
    if not WINDOW_FILE.exists():
        return []
    try:
        data = json.loads(WINDOW_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def filter_by_hours(tweets: list, hours: int) -> list:
    """Return only tweets whose created_at >= now - hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for t in tweets:
        created_at = t.get("created_at", "")
        if not created_at:
            # No timestamp — include it (conservative)
            result.append(t)
            continue
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                result.append(t)
        except Exception:
            result.append(t)  # Can't parse — include
    return result


def format_tweets_for_llm(tweets: list) -> str:
    """Format tweets into a concise text for the LLM."""
    lines = []
    for t in tweets[:50]:  # cap at 50 to stay within context
        user = (t.get("user") or {}).get("username", "unknown")
        text = t.get("text", "").replace("\n", " ").strip()
        likes = t.get("like_count", 0) or 0
        rts = t.get("retweet_count", 0) or 0
        category = t.get("_monitor_category", "")
        url = t.get("url", "") or f"https://x.com/{user}/status/{t.get('id', '')}"
        date = t.get("created_at", "")[:16]

        lines.append(
            f"[{category.upper()}] @{user} (❤{likes} 🔄{rts}) [{date}]\n"
            f"  {text[:150]}\n"
            f"  {url}"
        )
    return "\n\n".join(lines)


def call_openrouter(prompt: str) -> str:
    """Use requests.post with hard 90s wall-clock timeout.

    Why requests instead of urllib.urlopen:
      urlopen(timeout=N) resets on *any* received byte — a streaming LLM response
      that trickles bytes can hang indefinitely.  requests.post(timeout=N) enforces
      a hard wall-clock limit on the entire operation.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    try:
        resp = requests.post(
            OPENROUTER_API,
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1500,
                "temperature": 0.4,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://desearch.ai",
                "X-Title": "Desearch X Monitor",
            },
            timeout=90,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenRouter timed out after 90s — model may be overloaded")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"OpenRouter request failed: {e}")

    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned empty choices (free-tier rate limit?): {data}")
    return choices[0]["message"]["content"].strip()


def send_discord(text: str, channel_id: str) -> bool:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[WARN] DISCORD_BOT_TOKEN not set, skipping send", file=sys.stderr)
        return False

    # Discord message limit is 2000 chars; truncate if needed
    if len(text) > 1990:
        text = text[:1990] + "\n…"

    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json={"content": text},
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Discord request failed: {e}", file=sys.stderr)
        return False

    if resp.status_code >= 400:
        hint = ""
        if resp.status_code == 401:
            hint = " (bad token — check DISCORD_BOT_TOKEN)"
        elif resp.status_code == 403:
            hint = " (forbidden — bot lacks Send Messages permission)"
        elif resp.status_code == 404:
            hint = " (channel not found — check discord.alerts_channel in config.json)"
        print(f"[ERROR] Discord HTTP {resp.status_code}{hint}: {resp.text}", file=sys.stderr)
        return False

    return "id" in resp.json()


SYSTEM_PROMPT = """You are a sharp analyst for Desearch AI — a Bittensor Subnet 22 search product.

Analyze the following X (Twitter) posts collected in the monitoring window.
Provide a concise intelligence summary in this structure:

🔥 **HIGHLIGHTS** (2-4 most important findings — competitor moves, viral brand mentions, key Bittensor news)

🔍 **DESEARCH MENTIONS** (any direct mentions of @desearch_ai, #desearch, SN22 — what people are saying)

🦾 **BITTENSOR PULSE** (community sentiment, key discussions, price talk if notable)

🏆 **COMPETITOR WATCH** (ExaAI or other search/AI tools mentioned)

🤝 **INFLUENCER ACTIVITY** (marclou, johnrushx, markjeffrey, SiamKidd — anything relevant or usable for campaigns)

💡 **CONTENT OPPORTUNITIES** (1-3 concrete ideas: tweet angles, responses to write, topics to engage with based on what you saw)

Keep it tight. No fluff. Emojis are fine. Output in English."""


def main():
    parser = argparse.ArgumentParser(description="X Monitor Summarizer")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Discord post")
    parser.add_argument("--hours", type=int, default=4,
                        help="Summarize tweets from the last N hours (default: 4)")
    args = parser.parse_args()

    load_env()
    config = load_config()

    # Load window and filter to requested time range
    all_tweets = load_window()
    tweets = filter_by_hours(all_tweets, args.hours)

    if not tweets:
        print(f"No tweets in the last {args.hours}h window — nothing to summarize.")
        print(f"(tweets_window.json has {len(all_tweets)} total tweets)")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tweet_text = format_tweets_for_llm(tweets)

    prompt = (
        f"{SYSTEM_PROMPT}\n\n---\n\n"
        f"WINDOW: last {args.hours}h | {len(tweets)} tweets | as of {now}\n\n"
        f"{tweet_text}"
    )

    print(f"Summarizing {len(tweets)} tweets from last {args.hours}h "
          f"(window total: {len(all_tweets)})...", file=sys.stderr)

    try:
        summary = call_openrouter(prompt)
    except Exception as e:
        print(f"[ERROR] OpenRouter call failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Strip any HTML tags (plain text for Discord)
    summary_clean = re.sub(r'<[^>]+>', '', summary)
    header = f"📡 **X Monitor Summary** — {now}\n📊 {len(tweets)} tweets (last {args.hours}h)\n\n"
    full_message = header + summary_clean

    print(full_message)

    if not args.dry_run:
        # Post to Discord #x-alerts (channel from config.json discord.alerts_channel)
        discord_channel = str(config["discord"]["alerts_channel"])
        ok = send_discord(full_message, discord_channel)
        if ok:
            print("Posted to Discord.", file=sys.stderr)
        else:
            print("[WARN] Discord post failed.", file=sys.stderr)

    # Note: tweets_window.json is NOT cleared — it auto-expires via timestamp pruning in monitor.py


if __name__ == "__main__":
    main()
