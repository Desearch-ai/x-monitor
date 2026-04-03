#!/usr/bin/env python3
"""
X Monitor — Daily Stats Report
Prints a grouped-by-account summary of posts fetched in the last 24h.
Reads from tweets_window.json (sliding 24h window maintained by monitor.py).

Usage:
    python3 daily_stats.py              # last 24h
    python3 daily_stats.py --hours 48   # last 48h
    python3 daily_stats.py --post       # post to Discord after printing
"""
import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).parent
WINDOW_FILE = SCRIPT_DIR / "tweets_window.json"
STATE_FILE = SCRIPT_DIR / "state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"


def load_env():
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k not in os.environ:
                    os.environ[k] = v


def load_window() -> list:
    if WINDOW_FILE.exists():
        try:
            return json.loads(WINDOW_FILE.read_text())
        except Exception:
            return []
    return []


def filter_by_hours(tweets: list, hours: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for t in tweets:
        ca = t.get("created_at", "")
        if not ca:
            continue
        try:
            ts = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                result.append(t)
        except Exception:
            pass
    return result


def build_report(hours: int) -> str:
    tweets = filter_by_hours(load_window(), hours)

    # Group by source account
    by_account: dict = {}

    for t in tweets:
        source = t.get("_monitor_source", "unknown")
        if source.startswith("account:"):
            acct = source.replace("account:", "@")
        elif source.startswith("keyword:"):
            acct = f"🔍 {source.replace('keyword:', '')}"
        else:
            acct = source

        if acct not in by_account:
            by_account[acct] = {"count": 0, "likes": 0, "rt": 0, "tweets": []}
        by_account[acct]["count"] += 1
        by_account[acct]["likes"] += t.get("like_count", 0) or 0
        by_account[acct]["rt"] += t.get("retweet_count", 0) or 0
        by_account[acct]["tweets"].append(t)

    total = len(tweets)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"📊 **X Monitor — Daily Stats {date_str}**",
        f"Last {hours}h: **{total} posts** across {len(by_account)} accounts",
        "",
    ]

    if not by_account:
        lines.append("⚠️ No posts in window. Monitor may need a fresh run.")
        return "\n".join(lines)

    # Sort by count descending
    sorted_accounts = sorted(by_account.items(), key=lambda x: x[1]["count"], reverse=True)

    lines.append("**By account:**")
    for acct, data in sorted_accounts:
        lines.append(
            f"  {acct:<22}  {data['count']:>3} posts  E:{data['likes']:>4}  RT:{data['rt']:>3}"
        )

    lines.append("")

    # Top 3 posts by engagement (likes + 2*retweets)
    top = sorted(
        tweets,
        key=lambda t: (t.get("like_count", 0) or 0) + (t.get("retweet_count", 0) or 0) * 2,
        reverse=True,
    )[:3]
    if top:
        lines.append("**🔥 Top 3 Posts:**")
        for i, t in enumerate(top, 1):
            username = ""
            u = t.get("user", {})
            if isinstance(u, dict):
                username = f"@{u.get('username', '?')}"
            src = t.get("_monitor_source", "")
            if src.startswith("account:"):
                username = src.replace("account:", "@")
            likes = t.get("like_count", 0) or 0
            text = (t.get("text") or "")[:120].replace("\n", " ")
            url = t.get("url", "")
            lines.append(f"{i}. {username} ({likes} likes) — {text}")
            if url:
                lines.append(f"   {url}")

    return "\n".join(lines)


def post_to_discord(text: str):
    """Post stats to Discord #x-alerts channel."""
    import urllib.request
    import urllib.parse

    config = json.loads(CONFIG_FILE.read_text())
    channel_id = config.get("discord", {}).get("alerts_channel", "")
    if not channel_id:
        print("No Discord channel configured", file=sys.stderr)
        return

    # Use OpenClaw message tool via CLI isn't available here — print and let cron agent post
    print(text)


def main():
    load_env()
    parser = argparse.ArgumentParser(description="X Monitor Daily Stats")
    parser.add_argument("--hours", type=int, default=24, help="Hours to look back (default: 24)")
    parser.add_argument("--post", action="store_true", help="Post to Discord (handled by cron agent)")
    args = parser.parse_args()

    report = build_report(args.hours)
    print(report)


if __name__ == "__main__":
    main()
