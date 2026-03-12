#!/usr/bin/env python3
"""
X Monitor - Fetches new tweets from monitored accounts and keywords.
Uses Desearch AI API via the desearch.py skill script.

Output: JSON with new tweets only (deduplicates against state.json)

Usage:
    DESEARCH_API_KEY=xxx uv run python monitor.py
    DESEARCH_API_KEY=xxx uv run python monitor.py --reset   # clear state and re-fetch
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
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
                if k and k not in os.environ:
                    os.environ[k] = v


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


def normalize_tweet(tweet: dict, source: str, category: str, importance: str, context: str) -> dict:
    """Add monitor metadata to a tweet dict."""
    tweet["_monitor_source"] = source
    tweet["_monitor_category"] = category
    tweet["_monitor_importance"] = importance
    tweet["_monitor_context"] = context
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
    errors: list = []
    stats: dict = {"accounts_checked": 0, "keywords_checked": 0, "total_fetched": 0}

    global_filters = config.get("filters", {})

    # ── Monitor accounts ──────────────────────────────────────────────
    for account in config.get("accounts", []):
        username = account["username"]
        key = f"timeline:{username}"
        seen = set(seen_ids.get(key, []))

        tweets, err = get_timeline(username, count=15)
        stats["accounts_checked"] += 1

        if err:
            errors.append({"source": f"@{username}", "error": err})
            continue

        new_for_account = []
        new_ids = []
        for tweet in tweets:
            tid = str(tweet.get("id") or tweet.get("id_str") or "")
            if not tid or tid in seen:
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
            if not tid or tid in seen:
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

    # ── Output ────────────────────────────────────────────────────────
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "total_new": len(new_tweets),
        "new_tweets": new_tweets,
        "errors": errors,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
