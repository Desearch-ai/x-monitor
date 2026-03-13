#!/usr/bin/env python3
"""
X Monitor Summarizer — Reads latest_batch.json, sends to OpenRouter Nemotron,
posts analysis summary to Telegram.

Usage:
    python3 summarize.py
    python3 summarize.py --dry-run   # print summary, don't post to Telegram
    python3 summarize.py --clear     # clear batch after summarizing
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).parent
BATCH_FILE = SCRIPT_DIR / "latest_batch.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


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


def load_batch() -> list:
    if not BATCH_FILE.exists():
        return []
    try:
        data = json.loads(BATCH_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def clear_batch():
    if BATCH_FILE.exists():
        BATCH_FILE.write_text("[]")


def format_tweets_for_llm(tweets: list) -> str:
    """Format tweets into a concise text for the LLM."""
    lines = []
    for t in tweets[:120]:  # cap at 120 to stay within context
        user = (t.get("user") or {}).get("username", "unknown")
        text = t.get("text", "").replace("\n", " ").strip()
        likes = t.get("like_count", 0) or 0
        rts = t.get("retweet_count", 0) or 0
        source = t.get("_monitor_source", "")
        category = t.get("_monitor_category", "")
        url = t.get("url", "") or f"https://x.com/{user}/status/{t.get('id','')}"
        date = t.get("created_at", "")[:16]

        lines.append(
            f"[{category.upper()}] @{user} (❤{likes} 🔄{rts}) [{date}]\n"
            f"  {text[:200]}\n"
            f"  {url}"
        )
    return "\n\n".join(lines)


def call_openrouter(prompt: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.4,
    }).encode()

    req = Request(
        OPENROUTER_API,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://desearch.ai",
            "X-Title": "Desearch X Monitor",
        },
        method="POST",
    )

    with urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())

    return result["choices"][0]["message"]["content"].strip()


def send_telegram(text: str, chat_id: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[WARN] TELEGRAM_BOT_TOKEN not set, skipping send", file=sys.stderr)
        return False

    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()

    req = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        return result.get("ok", False)
    except HTTPError as e:
        err = e.read().decode()
        print(f"[ERROR] Telegram: {err}", file=sys.stderr)
        return False


SYSTEM_PROMPT = """You are a sharp analyst for Desearch AI — a Bittensor Subnet 22 search product.

Analyze the following X (Twitter) posts collected in the last ~4 hours by our monitoring system.
Provide a concise intelligence summary in this structure:

🔥 **HIGHLIGHTS** (2-4 most important findings — competitor moves, viral brand mentions, key Bittensor news)

🔍 **DESEARCH MENTIONS** (any direct mentions of @desearch_ai, #desearch, SN22 — what people are saying)

🦾 **BITTENSOR PULSE** (community sentiment, key discussions, price talk if notable)

🏆 **COMPETITOR WATCH** (ExaAI or other search/AI tools mentioned)

🤝 **INFLUENCER ACTIVITY** (marclou, johnrushx, markjeffrey, SiamKidd — anything relevant or usable for campaigns)

💡 **CONTENT OPPORTUNITIES** (1-3 concrete ideas: tweet angles, responses to write, topics to engage with based on what you saw)

Keep it tight. Telegram-formatted. No fluff. Emojis are fine. Output in English."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Telegram post")
    parser.add_argument("--clear", action="store_true", help="Clear batch file after summarizing")
    args = parser.parse_args()

    load_env()
    config = load_config()

    tweets = load_batch()
    if not tweets:
        print("No tweets in batch — nothing to summarize.")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tweet_text = format_tweets_for_llm(tweets)

    prompt = f"{SYSTEM_PROMPT}\n\n---\n\nBATCH: {len(tweets)} tweets collected up to {now}\n\n{tweet_text}"

    print(f"Sending {len(tweets)} tweets to Nemotron for analysis...", file=sys.stderr)

    try:
        summary = call_openrouter(prompt)
    except Exception as e:
        print(f"[ERROR] OpenRouter call failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Build final message (plain text for Discord)
    import re
    summary_clean = re.sub(r'<[^>]+>', '', summary)
    header = f"📡 **X Monitor Summary** — {now}\n📊 {len(tweets)} tweets analyzed\n\n"
    full_message = header + summary_clean

    print(full_message)

    if args.clear and not args.dry_run:
        pass  # cleared below

    if args.clear:
        clear_batch()
        print("Batch cleared.", file=sys.stderr)


if __name__ == "__main__":
    main()
