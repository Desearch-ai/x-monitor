#!/usr/bin/env python3
"""
X Monitor - Fetches new tweets from monitored accounts and keywords.
Uses Desearch AI API via the desearch.py skill script.

Output: JSON with new tweets only (deduplicates against state.json)
Also maintains tweets_window.json: all fetched tweets with timestamps (sliding 24h window)

Usage:
    DESEARCH_API_KEY=xxx uv run python monitor.py
    DESEARCH_API_KEY=xxx uv run python monitor.py --reset   # clear state and re-fetch
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
WINDOW_FILE = SCRIPT_DIR / "tweets_window.json"
PENDING_ALERTS_FILE = SCRIPT_DIR / "pending_alerts.json"
DESEARCH_SCRIPT = Path.home() / ".openclaw/workspace/skills/desearch-x-search/scripts/desearch.py"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": {}, "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_window() -> list:
    """Load the sliding tweet window (tweets_window.json)."""
    if WINDOW_FILE.exists():
        try:
            data = json.loads(WINDOW_FILE.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def load_pending_alerts() -> list:
    """Load queued alerts waiting for Discord delivery."""
    if PENDING_ALERTS_FILE.exists():
        try:
            data = json.loads(PENDING_ALERTS_FILE.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_window(tweets: list):
    """Merge new tweets into window, deduplicate by id, prune to last 24h."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # Deduplicate by tweet id
    seen_ids: set = set()
    deduped: list = []
    for t in tweets:
        tid = str(t.get("id") or t.get("id_str") or "")
        if tid and tid in seen_ids:
            continue
        if tid:
            seen_ids.add(tid)

        # Ensure created_at is present and timezone-aware
        created_at = t.get("created_at", "")
        if not created_at:
            t = dict(t)  # don't mutate original
            t["created_at"] = now.isoformat()
            created_at = t["created_at"]

        # Prune: keep only tweets within last 24h
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except Exception:
            pass  # If we can't parse timestamp, keep the tweet

        deduped.append(t)

    WINDOW_FILE.write_text(json.dumps(deduped, indent=2, ensure_ascii=False))
    return len(deduped)


def load_env():
    """Load .env file from script directory into os.environ if key not already set."""
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if k:
                    os.environ[k] = v  # always prefer .env over inherited shell env


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def run_desearch(args: list) -> tuple[dict | list | None, str | None]:
    """Run the desearch.py script and return parsed JSON result."""
    api_key = os.environ.get("DESEARCH_API_KEY")
    if not api_key:
        return None, "DESEARCH_API_KEY not set"

    # Use uv if available, else fall back to python3
    import shutil
    py = shutil.which("uv")
    cmd = ([py, "run", "python"] if py else ["python3"]) + [str(DESEARCH_SCRIPT)] + args
    env = {**os.environ}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        if result.returncode != 0:
            return None, result.stderr.strip() or result.stdout.strip()
        return json.loads(result.stdout), None
    except subprocess.TimeoutExpired:
        return None, "Request timed out"
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}\nOutput: {result.stdout[:200]}"
    except Exception as e:
        return None, str(e)


def get_timeline(username: str, count: int = 15) -> tuple[list, str | None]:
    """Fetch recent timeline posts for a user."""
    data, err = run_desearch(["x_timeline", username, f"--count={count}"])
    if err or not data:
        return [], err or "No data"
    if isinstance(data, dict):
        tweets = data.get("tweets", [])
    elif isinstance(data, list):
        tweets = data
    else:
        return [], "Unexpected response format"
    return tweets, None


def search_keyword(query: str, count: int = 25) -> tuple[list, str | None]:
    """Search for recent posts matching a keyword/hashtag."""
    data, err = run_desearch(["x", query, "--sort=Latest", f"--count={count}"])
    if err or not data:
        return [], err or "No data"
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        return data.get("tweets", data.get("results", [])), None
    return [], "Unexpected response format"


def is_important_enough(tweet: dict, account_config: dict, global_filters: dict) -> bool:
    """Decide if a tweet should be reported."""
    importance = account_config.get("importance", "normal")

    # Always report high importance accounts
    if importance == "high":
        return True

    # For normal accounts, apply engagement filter
    min_likes = global_filters.get("normal_importance_min_likes", 5)
    like_count = tweet.get("like_count", tweet.get("favorite_count", 0)) or 0
    retweet_count = tweet.get("retweet_count", 0) or 0

    return like_count >= min_likes or retweet_count >= 3


def parse_twitter_date(s: str) -> str | None:
    """Convert Twitter's 'Mon Mar 16 07:01:23 +0000 2026' to ISO 8601."""
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.isoformat()
    except Exception:
        return None


def normalize_tweet(tweet: dict, source: str, category: str, importance: str, context: str) -> dict:
    """Add monitor metadata to a tweet dict. Also converts created_at to ISO 8601."""
    tweet["_monitor_source"] = source
    tweet["_monitor_category"] = category
    tweet["_monitor_importance"] = importance
    tweet["_monitor_context"] = context
    # Convert Twitter date format to ISO 8601 for window compatibility
    ca = tweet.get("created_at", "")
    if ca and not ca[0].isdigit():  # Twitter format starts with weekday name
        iso = parse_twitter_date(ca)
        if iso:
            tweet["created_at"] = iso
    return tweet


def main():
    parser = argparse.ArgumentParser(description="X Monitor")
    parser.add_argument("--reset", action="store_true", help="Clear state and re-fetch all")
    parser.add_argument("--dry-run", action="store_true", help="Don't save state")
    args = parser.parse_args()

    load_env()
    config = load_config()
    state = load_state() if not args.reset else {"seen_ids": {}, "last_run": None}

    seen_ids: dict = state.get("seen_ids", {})
    new_tweets: list = []
    all_window_tweets: list = []  # ALL fetched tweets for tweets_window.json
    errors: list = []
    stats: dict = {"accounts_checked": 0, "keywords_checked": 0, "total_fetched": 0}

    global_filters = config.get("filters", {})

    # ── Monitor accounts ──────────────────────────────────────────────
    for account in config.get("accounts", []):
        username = account["username"]
        key = f"timeline:{username}"
        seen = set(seen_ids.get(key, []))

        tweets, err = get_timeline(username, count=50)
        stats["accounts_checked"] += 1

        if err:
            errors.append({"source": f"@{username}", "error": err})
            continue

        new_for_account = []
        new_ids = []
        for tweet in tweets:
            tid = str(tweet.get("id") or tweet.get("id_str") or "")
            if not tid:
                continue

            # Add ALL fetched tweets to window (regardless of seen status)
            t_copy = dict(tweet)
            normalize_tweet(t_copy, f"account:{username}", account.get("category", "general"),
                            account.get("importance", "normal"), account.get("context", ""))
            all_window_tweets.append(t_copy)

            if tid in seen:
                continue
            stats["total_fetched"] += 1

            # Skip retweets if not wanted
            if tweet.get("is_retweet") and not account.get("include_retweets", False):
                new_ids.append(tid)  # still mark as seen
                continue

            # Skip replies if configured
            if global_filters.get("skip_replies", True) and tweet.get("in_reply_to_status_id"):
                new_ids.append(tid)
                continue

            if is_important_enough(tweet, account, global_filters):
                normalize_tweet(tweet, f"account:{username}", account.get("category", "general"),
                                account.get("importance", "normal"), account.get("context", ""))
                new_for_account.append(tweet)

            new_ids.append(tid)

        # Keep last 500 seen IDs per source
        all_ids = list(seen) + new_ids
        seen_ids[key] = all_ids[-500:]
        new_tweets.extend(new_for_account)

    # ── Monitor keywords ──────────────────────────────────────────────
    for kw in config.get("keywords", []):
        query = kw["query"]
        key = f"keyword:{query}"
        seen = set(seen_ids.get(key, []))

        tweets, err = search_keyword(query, count=25)
        stats["keywords_checked"] += 1

        if err:
            errors.append({"source": f"keyword:{query}", "error": err})
            continue

        new_for_kw = []
        new_ids = []
        for tweet in tweets:
            tid = str(tweet.get("id") or tweet.get("id_str") or "")
            if not tid:
                continue

            # Add ALL fetched tweets to window (regardless of seen status)
            t_copy = dict(tweet)
            normalize_tweet(t_copy, f"keyword:{query}", kw.get("category", "keyword"),
                            kw.get("importance", "high"), kw.get("context", ""))
            all_window_tweets.append(t_copy)

            if tid in seen:
                continue
            stats["total_fetched"] += 1

            normalize_tweet(tweet, f"keyword:{query}", kw.get("category", "keyword"),
                            kw.get("importance", "high"), kw.get("context", ""))
            new_for_kw.append(tweet)
            new_ids.append(tid)

        all_ids = list(seen) + new_ids
        seen_ids[key] = all_ids[-500:]
        new_tweets.extend(new_for_kw)

    # ── Save state ────────────────────────────────────────────────────
    if not args.dry_run:
        state["seen_ids"] = seen_ids
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    # ── Save tweets to sliding window for summarizer ──────────────────
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "total_new": len(new_tweets),
        "new_tweets": new_tweets,
        "errors": errors,
    }

    if not args.dry_run:
        # Merge all fetched tweets into the sliding 24h window
        existing_window = load_window()
        merged = existing_window + all_window_tweets
        window_count = save_window(merged)
        output["window_updated"] = window_count

        # Merge newly fetched alerts into the pending Discord queue.
        # This preserves unsent alerts across monitor runs if Discord delivery failed.
        # post-to-discord.cjs clears the file only after all chunks are confirmed sent.
        queued_alerts = load_pending_alerts()
        merged_alerts = queued_alerts + new_tweets
        deduped_alerts: list = []
        seen_pending_ids: set[str] = set()
        for tweet in merged_alerts:
            tid = str(tweet.get("id") or tweet.get("id_str") or "")
            dedupe_key = tid or json.dumps(tweet, sort_keys=True, ensure_ascii=False)
            if dedupe_key in seen_pending_ids:
                continue
            seen_pending_ids.add(dedupe_key)
            deduped_alerts.append(tweet)

        PENDING_ALERTS_FILE.write_text(json.dumps(deduped_alerts, indent=2, ensure_ascii=False))
        output["pending_alerts"] = len(deduped_alerts)

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
