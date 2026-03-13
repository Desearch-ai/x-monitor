#!/usr/bin/env python3
"""
Feishu Digest Writer for X Monitor.

Reads monitor output (JSON from stdin or --file) and appends a daily tweet
digest to the configured Feishu document, organized by category.

Usage:
    uv run python monitor.py | uv run python feishu_digest.py
    uv run python feishu_digest.py --file monitor_output.json

Config required in config.json:
    feishu.doc_token       — Feishu docx token (from URL: .../docx/TOKEN)
    feishu.app_id          — Feishu Open Platform App ID
    feishu.app_secret      — Feishu Open Platform App Secret

Get credentials at: https://open.feishu.cn/app
Create an app, enable docx:document scope, install to workspace, then paste
app_id and app_secret here. Set doc_token from the doc URL.
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import urllib.request
import urllib.error

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"

# Category display names & icons
CATEGORY_META = {
    "bittensor":   {"icon": "🦾", "label": "Bittensor"},
    "desearch":    {"icon": "🔍", "label": "Desearch"},
    "brand":       {"icon": "🔍", "label": "Brand Mentions"},
    "competitor":  {"icon": "🏆", "label": "Competitors"},
    "influencer":  {"icon": "🤝", "label": "Influencers"},
    "subnet":      {"icon": "#️⃣", "label": "Subnet 22"},
    "ai":          {"icon": "🤖", "label": "AI"},
    "content":     {"icon": "📝", "label": "Content"},
    "system":      {"icon": "⚙️",  "label": "System"},
    "keyword":     {"icon": "#️⃣", "label": "Keywords"},
}


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """Get a Feishu tenant_access_token using app credentials."""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth error: {data}")
    return data["tenant_access_token"]


def append_to_doc(doc_token: str, token: str, markdown: str):
    """Append markdown text to the end of a Feishu docx document."""
    # We use the docx content append API (blocks)
    # Feishu supports adding paragraph blocks via batch_update
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token}/children"

    # Convert markdown to a list of paragraph texts (simple approach)
    lines = markdown.split("\n")
    children = []
    for line in lines:
        stripped = line.rstrip()
        # Heading detection
        if stripped.startswith("# "):
            block = {
                "block_type": 2,  # Heading1
                "heading1": {"elements": [{"text_run": {"content": stripped[2:]}}]},
            }
        elif stripped.startswith("## "):
            block = {
                "block_type": 3,  # Heading2
                "heading2": {"elements": [{"text_run": {"content": stripped[3:]}}]},
            }
        elif stripped.startswith("### "):
            block = {
                "block_type": 4,  # Heading3
                "heading3": {"elements": [{"text_run": {"content": stripped[4:]}}]},
            }
        elif stripped.startswith("- ") or stripped.startswith("• "):
            block = {
                "block_type": 12,  # BulletBlock
                "bullet": {"elements": [{"text_run": {"content": stripped[2:]}}]},
            }
        elif stripped == "---" or stripped == "":
            block = {
                "block_type": 1,  # Text (empty divider)
                "text": {"elements": [{"text_run": {"content": ""}}]},
            }
        else:
            block = {
                "block_type": 1,  # Text
                "text": {"elements": [{"text_run": {"content": stripped}}]},
            }
        children.append(block)

    payload = json.dumps({"children": children, "index": -1}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        if result.get("code") != 0:
            raise RuntimeError(f"Feishu append error: {result}")
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body}")


def format_digest(monitor_output: dict) -> str:
    """Build markdown digest from monitor output."""
    now = datetime.now(timezone(timedelta(hours=4)))  # Georgia/Tbilisi UTC+4
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    tweets = monitor_output.get("new_tweets", [])
    stats = monitor_output.get("stats", {})
    errors = monitor_output.get("errors", [])

    if not tweets:
        return ""  # Nothing to write

    # Group by category
    by_category: dict[str, list] = {}
    for tweet in tweets:
        cat = tweet.get("_monitor_category", "other")
        by_category.setdefault(cat, []).append(tweet)

    lines = []
    lines.append(f"# 📊 X Monitor Digest — {date_str} {time_str}")
    lines.append(f"")
    lines.append(f"**{len(tweets)} new posts** · {stats.get('accounts_checked', 0)} accounts · {stats.get('keywords_checked', 0)} keywords")
    lines.append("")
    lines.append("---")
    lines.append("")

    for cat, cat_tweets in sorted(by_category.items()):
        meta = CATEGORY_META.get(cat, {"icon": "📌", "label": cat.title()})
        lines.append(f"## {meta['icon']} {meta['label']} ({len(cat_tweets)})")
        lines.append("")

        for tweet in cat_tweets:
            username = tweet.get("username") or tweet.get("author_id") or "unknown"
            text = tweet.get("text") or tweet.get("full_text") or ""
            # Truncate long tweets
            if len(text) > 240:
                text = text[:237] + "..."
            tid = tweet.get("id") or tweet.get("id_str") or ""
            likes = tweet.get("like_count") or tweet.get("favorite_count") or 0
            rts = tweet.get("retweet_count") or 0
            importance = tweet.get("_monitor_importance", "normal")

            prefix = "🔔" if importance == "high" else "•"
            lines.append(f"{prefix} **@{username}** ❤️{likes} 🔄{rts}")
            lines.append(f"  {text}")
            if tid:
                lines.append(f"  🔗 https://x.com/{username}/status/{tid}")
            lines.append("")

    if errors:
        lines.append("## ⚠️ Errors")
        lines.append("")
        for err in errors:
            lines.append(f"- **{err.get('source')}**: {err.get('error')}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Feishu Digest Writer")
    parser.add_argument("--file", help="Read monitor JSON from file instead of stdin")
    parser.add_argument("--dry-run", action="store_true", help="Print digest, don't write to Feishu")
    args = parser.parse_args()

    config = load_config()
    feishu_cfg = config.get("feishu", {})
    doc_token = feishu_cfg.get("doc_token", "").strip()
    app_id = feishu_cfg.get("app_id", "").strip()
    app_secret = feishu_cfg.get("app_secret", "").strip()

    if not doc_token:
        print("[feishu_digest] doc_token not configured — skipping", file=sys.stderr)
        sys.exit(0)

    if not app_id or not app_secret:
        print("[feishu_digest] app_id/app_secret not configured — skipping", file=sys.stderr)
        sys.exit(0)

    # Read monitor output
    if args.file:
        monitor_output = json.loads(Path(args.file).read_text())
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            print("[feishu_digest] No input received", file=sys.stderr)
            sys.exit(1)
        monitor_output = json.loads(raw)

    total_new = monitor_output.get("total_new", 0)
    if total_new == 0:
        print("[feishu_digest] No new tweets — nothing to write", file=sys.stderr)
        sys.exit(0)

    digest_md = format_digest(monitor_output)
    if not digest_md:
        print("[feishu_digest] Empty digest — skipping", file=sys.stderr)
        sys.exit(0)

    if args.dry_run:
        print("=== DRY RUN — would append to Feishu doc ===")
        print(digest_md)
        sys.exit(0)

    # Auth + append
    print(f"[feishu_digest] Authenticating with Feishu...", file=sys.stderr)
    access_token = get_tenant_access_token(app_id, app_secret)

    print(f"[feishu_digest] Appending digest to doc {doc_token}...", file=sys.stderr)
    append_to_doc(doc_token, access_token, digest_md)

    print(f"[feishu_digest] ✅ Appended digest ({total_new} tweets) to Feishu doc", file=sys.stderr)


if __name__ == "__main__":
    main()
