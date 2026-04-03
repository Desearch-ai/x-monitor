#!/usr/bin/env python3
"""
X Monitor Summarizer — Compact, scannable, actionable format.

Generates the summary directly from tweets_window.json (no LLM for structure).
Uses LLM only for the single Opportunity line.

Max ~15 lines output. Plain URLs. No walls of text.

Usage:
    python3 summarize.py                  # summarize last 4h (default)
    python3 summarize.py --hours 12       # summarize last 12h
    python3 summarize.py --dry-run        # print only, don't post to Discord
    python3 summarize.py --dry-run --hours 12
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).parent
WINDOW_FILE = SCRIPT_DIR / "tweets_window.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemma-3-4b-it:free"

# Keywords that flag a tweet as a Desearch mention
DESEARCH_KEYWORDS = [
    "desearch", "@desearch_ai", "#desearch",
    "sn22", "subnet22", "subnet 22",
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

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
            result.append(t)
            continue
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                result.append(t)
        except Exception:
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def engagement(t: dict) -> int:
    """Weighted engagement score: likes + 2×retweets."""
    return (t.get("like_count", 0) or 0) + (t.get("retweet_count", 0) or 0) * 2


def is_desearch_tweet(t: dict) -> bool:
    text = (t.get("text", "") or "").lower()
    category = (t.get("_monitor_category", "") or "").lower()
    if "desearch" in category:
        return True
    return any(kw in text for kw in DESEARCH_KEYWORDS)


def tweet_url(t: dict) -> str:
    """Return a plain (non-angle-bracketed) URL for the tweet."""
    url = t.get("url", "") or ""
    # Strip any accidental angle brackets
    url = url.strip("<>")
    if url.startswith("http"):
        return url
    user = (t.get("user") or {}).get("username", "unknown")
    return f"https://x.com/{user}/status/{t.get('id', '')}"


def trunc(text: str, max_len: int = 75) -> str:
    text = text.replace("\n", " ").strip()
    return text[:max_len] + "…" if len(text) > max_len else text


# ---------------------------------------------------------------------------
# LLM: one-line opportunity (with graceful fallback)
# ---------------------------------------------------------------------------

def get_opportunity(tweets: list) -> str:
    """
    Ask the LLM for ONE concrete action line based on what's trending.
    Falls back to a static message if the API call fails.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key or not tweets:
        return "No actionable signals detected this window."

    # Feed top-10 by engagement as context
    top = sorted(tweets, key=engagement, reverse=True)[:10]
    context_lines = []
    for t in top:
        user = (t.get("user") or {}).get("username", "unknown")
        context_lines.append(f"@{user}: {trunc(t.get('text', ''), 100)}")

    prompt = (
        "You are a social media strategist for Desearch AI (Bittensor SN22 search subnet).\n"
        "Based on these recent X posts, write exactly ONE action line (max 20 words).\n"
        "It must be a concrete action Desearch should take right now "
        "(e.g. reply to someone, post on a topic, engage with a trend).\n"
        "No preamble. No label. Just the one action.\n\n"
        "Posts:\n" + "\n".join(context_lines)
    )

    try:
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 60,
            "temperature": 0.3,
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
        with urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
        raw = result["choices"][0]["message"]["content"].strip()
        # Ensure it's one line and not too long
        return raw.split("\n")[0][:130]
    except Exception as e:
        print(f"[WARN] LLM opportunity call failed: {e}", file=sys.stderr)
        return "No actionable signals detected this window."


# ---------------------------------------------------------------------------
# Format builder
# ---------------------------------------------------------------------------

def build_summary(tweets: list, hours: int) -> str:
    """
    Build the compact summary string.
    Target: max ~15 lines, plain URLs, scannable.

    Structure:
      📡 header
      (blank)
      🔍 Desearch Mentions   (up to 5, or "None this window")
      (blank)
      🏆 Top Posts           (exactly 3 highest-engagement tweets)
      (blank)
      💡 Opportunity         (1 line)
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    # ── Header (1 line) ────────────────────────────────────────────────────
    lines.append(f"📡 X Monitor | {now} | {hours}h window | {len(tweets)} tweets")
    lines.append("")

    # ── Desearch Mentions ─────────────────────────────────────────────────
    lines.append("🔍 Desearch Mentions")
    desearch_tweets = [t for t in tweets if is_desearch_tweet(t)]
    if desearch_tweets:
        # Sort by engagement, cap at 5
        for t in sorted(desearch_tweets, key=engagement, reverse=True)[:5]:
            user = (t.get("user") or {}).get("username", "unknown")
            text = trunc(t.get("text", ""), 70)
            url = tweet_url(t)
            likes = t.get("like_count", 0) or 0
            rts = t.get("retweet_count", 0) or 0
            lines.append(f"• @{user} (❤{likes} 🔄{rts}): {text} — {url}")
    else:
        lines.append("None this window")
    lines.append("")

    # ── Top Posts ─────────────────────────────────────────────────────────
    lines.append("🏆 Top Posts")
    top3 = sorted(tweets, key=engagement, reverse=True)[:3]
    for t in top3:
        user = (t.get("user") or {}).get("username", "unknown")
        text = trunc(t.get("text", ""), 70)
        url = tweet_url(t)
        likes = t.get("like_count", 0) or 0
        rts = t.get("retweet_count", 0) or 0
        lines.append(f"• @{user} (❤{likes} 🔄{rts}): {text} — {url}")
    lines.append("")

    # ── Opportunity ───────────────────────────────────────────────────────
    lines.append("💡 Opportunity")
    lines.append(get_opportunity(tweets))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def send_discord(text: str, channel_id: str) -> bool:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[WARN] DISCORD_BOT_TOKEN not set, skipping Discord post", file=sys.stderr)
        return False

    if len(text) > 1990:
        text = text[:1990] + "\n…"

    body = json.dumps({"content": text}).encode()
    req = Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        return "id" in result
    except HTTPError as e:
        err = e.read().decode()
        print(f"[ERROR] Discord API: {err}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="X Monitor Summarizer — compact format")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary only, do not post to Discord")
    parser.add_argument("--hours", type=int, default=4,
                        help="Summarize tweets from the last N hours (default: 4)")
    args = parser.parse_args()

    load_env()
    config = load_config()

    all_tweets = load_window()
    tweets = filter_by_hours(all_tweets, args.hours)

    if not tweets:
        print(f"No tweets in the last {args.hours}h window — nothing to summarize.")
        print(f"(tweets_window.json has {len(all_tweets)} total tweets)")
        return

    print(f"Building summary: {len(tweets)} tweets (last {args.hours}h) "
          f"of {len(all_tweets)} in window…", file=sys.stderr)

    summary = build_summary(tweets, args.hours)
    line_count = len(summary.splitlines())
    print(f"Output: {line_count} lines", file=sys.stderr)

    # Always print to stdout
    print(summary)

    if not args.dry_run:
        discord_channel = (
            config.get("discord", {}).get("alerts_channel")
            or config.get("discord_channel_id")
            or "1477727527618347340"
        )
        ok = send_discord(summary, discord_channel)
        if ok:
            print("✓ Posted to Discord.", file=sys.stderr)
        else:
            print("[WARN] Discord post failed.", file=sys.stderr)


if __name__ == "__main__":
    main()
