#!/usr/bin/env python3
"""
X Monitor v2 - Dual-lane signal ingestion for founder + brand.
Fetches new tweets from monitored accounts and keywords.
Uses Desearch AI API via the desearch.py skill script.

Output: JSON with new tweets only (deduplicates against state.json)
Also maintains tweets_window.json: all fetched tweets with timestamps (sliding 24h window)

Key v2 changes:
- Config expresses lanes (founder, brand) and buckets instead of flat category lists
- Every collected tweet is normalized with richer metadata: bucket, lanes, route_hint
- Downstream tools can route founder vs brand signals differently

Usage:
    DESEARCH_API_KEY=xxx uv run python x-monitor.py
    DESEARCH_API_KEY=xxx uv run python x-monitor.py --reset   # clear state and re-fetch
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "state.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
WINDOW_FILE = SCRIPT_DIR / "tweets_window.json"
PENDING_ALERTS_FILE = SCRIPT_DIR / "pending_alerts.json"
MONITOR_LOCK_FILE = SCRIPT_DIR / ".monitor.lock"
PENDING_ALERTS_LOCK_FILE = SCRIPT_DIR / ".pending-alerts.lock"
DESEARCH_SCRIPT = Path.home() / ".openclaw/workspace/skills/desearch-x-search/scripts/desearch.py"
DEFAULT_DISCORD_CHANNEL = "1498287725223215185"
MANAGED_RUNTIME_PATH_ENV_VARS = (
    "X_MONITOR_RUNTIME_PATH",
    "SOCIAL_OS_X_MONITOR_PATH",
    "SOCIAL_OS_RUNTIME_PATH",
)
DEFAULT_MANAGED_RUNTIME_PATHS = (
    Path.home() / "projects/desearch/social-os/runtime/x-monitor.json",
    Path.home() / "projects/desearch/social-os/runtime/social-os-x-runtime.json",
)
SUPABASE_URL_ENV_VARS = (
    "SOCIAL_OS_SUPABASE_URL",
    "SUPABASE_URL",
    "VITE_SUPABASE_URL",
)
SUPABASE_ANON_KEY_ENV_VARS = (
    "SOCIAL_OS_SUPABASE_KEY",
    "SOCIAL_OS_SUPABASE_ANON_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "VITE_SUPABASE_ANON_KEY",
)
SUPABASE_FETCH_TIMEOUT_SECONDS = 10
SUPABASE_WRITE_TIMEOUT_SECONDS = 5
SOCIAL_RUNTIME_SERVICE = "x-monitor"
REPRESENTATIVE_SIGNAL_LIMIT = 10


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"monitor already running, lock held at {lock_path}")
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": {}, "last_run": None}


def save_state(state: dict):
    atomic_write_json(STATE_FILE, state)


def load_window() -> list:
    """Load the sliding tweet window (tweets_window.json)."""
    if WINDOW_FILE.exists():
        try:
            data = json.loads(WINDOW_FILE.read_text())
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_window(tweets: list):
    """Merge new tweets into window, deduplicate by id, prune to last 24h."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    seen_ids: set = set()
    deduped: list = []
    for t in tweets:
        tid = str(t.get("id") or t.get("id_str") or "")
        if tid and tid in seen_ids:
            continue
        if tid:
            seen_ids.add(tid)

        created_at = t.get("created_at", "")
        if not created_at:
            t = dict(t)
            t["created_at"] = now.isoformat()
            created_at = t["created_at"]

        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except Exception:
            pass

        deduped.append(t)

    atomic_write_json(WINDOW_FILE, deduped)
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
                    os.environ[k] = v


def normalize_string(value: object, default: str = "") -> str:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else default
    return default


def normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = normalize_string(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return items


def normalize_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def normalize_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_account_filter_map(filters: object) -> dict:
    if not isinstance(filters, dict):
        return {}

    normalized: dict[str, dict] = {}
    for account, raw in filters.items():
        handle = normalize_string(account).lstrip("@")
        if not handle or not isinstance(raw, dict):
            continue
        normalized[handle] = {
            "positive": normalize_string_list(
                raw.get("positive") or raw.get("positive_keywords")
            ),
            "negative": normalize_string_list(
                raw.get("negative") or raw.get("negative_keywords")
            ),
            "goal": normalize_string(
                raw.get("goal") or raw.get("account_goal") or raw.get("account_goal_hint")
            ),
        }
    return normalized


def normalize_account_goal_map(goals: object) -> dict[str, str]:
    if not isinstance(goals, dict):
        return {}

    normalized: dict[str, str] = {}
    for account, raw in goals.items():
        handle = normalize_string(account).lstrip("@")
        if not handle:
            continue
        if isinstance(raw, dict):
            goal = normalize_string(
                raw.get("goal") or raw.get("account_goal") or raw.get("account_goal_hint")
            )
        else:
            goal = normalize_string(raw)
        if goal:
            normalized[handle] = goal
    return normalized


def text_matches(text: str, terms: list[str]) -> list[str]:
    haystack = text.lower()
    matches: list[str] = []
    for term in terms:
        if term.lower() in haystack and term not in matches:
            matches.append(term)
    return matches


def account_handles_for_lanes(lanes: list[str], config: dict) -> list[str]:
    handles: list[str] = []
    for account in config.get("accounts", []):
        if not isinstance(account, dict):
            continue
        username = normalize_string(account.get("username")).lstrip("@")
        if not username:
            continue
        account_lanes = normalize_string_list(account.get("lanes"))
        if any(lane in account_lanes for lane in lanes) and username not in handles:
            handles.append(username)
    return handles


def build_account_eligibility_metadata(signal: dict, config: dict | None) -> dict:
    data = config if isinstance(config, dict) else {}
    lanes = normalize_string_list(signal.get("_monitor_lanes"))
    source_text = " ".join(
        normalize_string(value)
        for value in (
            signal.get("text"),
            signal.get("full_text"),
            signal.get("_monitor_bucket"),
            signal.get("_monitor_context"),
            signal.get("_monitor_source"),
        )
        if normalize_string(value)
    )
    account_filters = normalize_account_filter_map(data.get("account_filters"))
    account_goals = normalize_account_goal_map(data.get("account_goals"))
    lane_accounts = account_handles_for_lanes(lanes, data)

    eligible: list[str] = []
    goals: dict[str, str] = {}
    negative_matches: dict[str, list[str]] = {}
    positive_reasons: list[str] = []

    candidate_accounts = list(dict.fromkeys([*lane_accounts, *account_filters.keys()]))
    for handle in candidate_accounts:
        rule = account_filters.get(handle, {})
        positive = normalize_string_list(rule.get("positive"))
        negative = normalize_string_list(rule.get("negative"))
        positives_hit = text_matches(source_text, positive) if positive else []
        negatives_hit = text_matches(source_text, negative) if negative else []
        if negatives_hit:
            negative_matches[handle] = negatives_hit
            continue

        routed_by_lane = handle in lane_accounts
        matched_positive = bool(positives_hit)
        if matched_positive or (routed_by_lane and not positive):
            eligible.append(handle)
            goal = normalize_string(rule.get("goal") or account_goals.get(handle))
            if goal:
                goals[handle] = goal
            if matched_positive:
                positive_reasons.append(f"{handle}: positive filter matched {', '.join(positives_hit)}")
            elif routed_by_lane:
                positive_reasons.append(f"{handle}: lane route matched {', '.join(lanes)}")

    skipped_reason = ""
    if not eligible:
        if negative_matches:
            skipped_reason = "negative_filter_match"
        elif account_filters:
            skipped_reason = "no_account_positive_filter_match"
        elif lane_accounts:
            skipped_reason = "lane_routed_no_account_strategy"

    return {
        "collection_stage": "raw_collected",
        "filter_stage": "candidate" if eligible else "unqualified",
        "eligible_accounts": eligible,
        "account_goal_hint": goals,
        "signal_value_reason": (
            "; ".join(positive_reasons)
            if positive_reasons
            else "No account-specific positive filter matched."
        ),
        "negative_filter_matches": negative_matches,
        "skipped_reason": skipped_reason,
    }


def candidate_signal_count(signals: list | None) -> int:
    return sum(
        1
        for signal in (signals or [])
        if isinstance(signal, dict)
        and normalize_string_list(signal.get("_monitor_eligible_accounts"))
        and normalize_string(signal.get("_monitor_filter_stage"), "candidate") == "candidate"
    )

def read_json_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def normalize_supabase_rest_url(raw_url: str) -> str:
    base_url = raw_url.strip().rstrip("/")
    if base_url.endswith("/rest/v1"):
        return base_url
    return f"{base_url}/rest/v1"


def build_social_runtime_config_url(raw_url: str) -> str:
    query = urllib.parse.urlencode(
        {
            "select": "*",
            "is_active": "eq.true",
            "order": "updated_at.desc",
            "limit": "1",
        }
    )
    return f"{normalize_supabase_rest_url(raw_url)}/social_runtime_configs?{query}"


def build_social_runtime_events_url(raw_url: str) -> str:
    return f"{normalize_supabase_rest_url(raw_url)}/social_runtime_events"


def stable_json_dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_fingerprint(payload: object) -> str:
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def build_telemetry_input_snapshot(config: dict | None) -> dict:
    data = config if isinstance(config, dict) else {}
    accounts = [
        normalize_string(account.get("username"))
        for account in data.get("accounts", [])
        if isinstance(account, dict) and normalize_string(account.get("username"))
    ]
    keywords = [
        normalize_string(keyword.get("query"))
        for keyword in data.get("keywords", [])
        if isinstance(keyword, dict) and normalize_string(keyword.get("query"))
    ]

    lane_summary: dict[str, dict] = {}
    for lane in data.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        lane_id = normalize_string(lane.get("id"))
        if not lane_id:
            continue
        lane_summary[lane_id] = {
            "buckets": normalize_string_list(lane.get("buckets")),
            "route_hint": normalize_string(lane.get("route_hint")),
        }

    watchlists: list[dict] = []
    for account in data.get("accounts", []):
        if not isinstance(account, dict):
            continue
        username = normalize_string(account.get("username"))
        if not username:
            continue
        bucket = normalize_string(account.get("bucket"), "general")
        lanes = resolve_lanes(account, bucket, data)
        watchlists.append({
            "kind": "account",
            "value": username,
            "bucket": bucket,
            "lanes": lanes,
            "route_hints": resolve_route_hints(lanes, data),
            "importance": normalize_string(account.get("importance"), "high"),
            "context": normalize_string(account.get("context")),
        })

    for keyword in data.get("keywords", []):
        if not isinstance(keyword, dict):
            continue
        query = normalize_string(keyword.get("query"))
        if not query:
            continue
        bucket = normalize_string(keyword.get("bucket"), "keyword")
        lanes = resolve_lanes(keyword, bucket, data)
        watchlists.append({
            "kind": "keyword",
            "value": query,
            "bucket": bucket,
            "lanes": lanes,
            "route_hints": resolve_route_hints(lanes, data),
            "importance": normalize_string(keyword.get("importance"), "high"),
            "context": normalize_string(keyword.get("context")),
        })

    snapshot = {
        "accounts": accounts,
        "keywords": keywords,
        "counts": {
            "accounts": len(accounts),
            "keywords": len(keywords),
            "lanes": len(lane_summary),
        },
        "lane_summary": lane_summary,
        "watchlists": watchlists,
    }
    snapshot["config_fingerprint"] = config_fingerprint(snapshot)
    return snapshot


def tweet_external_id(tweet: dict) -> str:
    return str(tweet.get("id") or tweet.get("id_str") or tweet.get("tweet_id") or "").strip()


def tweet_author(tweet: dict) -> dict:
    user = tweet.get("user") if isinstance(tweet.get("user"), dict) else {}
    handle = normalize_string(
        tweet.get("username")
        or tweet.get("author_username")
        or tweet.get("screen_name")
        or user.get("username")
        or user.get("screen_name")
    ).lstrip("@")
    author: dict[str, str] = {}
    if handle:
        author["handle"] = handle
    name = normalize_string(tweet.get("author_name") or tweet.get("name") or user.get("name"))
    if name:
        author["name"] = name
    author_id = normalize_string(tweet.get("author_id") or user.get("id") or user.get("id_str"))
    if author_id:
        author["id"] = author_id
    return author


def tweet_source_url(tweet: dict) -> str:
    explicit = normalize_string(tweet.get("url") or tweet.get("tweet_url") or tweet.get("expanded_url"))
    if explicit:
        return explicit
    author = tweet_author(tweet).get("handle", "")
    external_id = tweet_external_id(tweet)
    if author and external_id:
        return f"https://x.com/{author}/status/{external_id}"
    return ""


def content_snippet(tweet: dict, limit: int = 280) -> str:
    text = normalize_string(tweet.get("text") or tweet.get("full_text") or tweet.get("content"))
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 1)].rstrip()}…"


def source_match_value(source: str, prefix: str) -> str:
    if not source.startswith(prefix):
        return ""
    return normalize_string(source[len(prefix):])


def signal_matched_terms(tweet: dict, config: dict | None = None) -> list[str]:
    explicit = normalize_string_list(tweet.get("_monitor_matched_terms") or tweet.get("matched_terms"))
    if explicit:
        return explicit
    source = normalize_string(tweet.get("_monitor_source") or tweet.get("source"))
    if source.startswith("keyword:"):
        value = source_match_value(source, "keyword:")
        return [value] if value else []

    terms: list[str] = []
    data = config if isinstance(config, dict) else {}
    text = content_snippet(tweet).lower()
    for keyword in data.get("keywords", []):
        if not isinstance(keyword, dict):
            continue
        query = normalize_string(keyword.get("query"))
        if query and query.lower() in text and query not in terms:
            terms.append(query)
    return terms


def signal_matched_accounts(tweet: dict) -> list[str]:
    explicit = normalize_string_list(tweet.get("_monitor_matched_accounts") or tweet.get("matched_accounts"))
    if explicit:
        return [item.lstrip("@") for item in explicit]
    source = normalize_string(tweet.get("_monitor_source") or tweet.get("source"))
    if source.startswith("account:"):
        value = source_match_value(source, "account:").lstrip("@")
        return [value] if value else []
    return []


def signal_risk_flags(tweet: dict) -> list[str]:
    flags: list[str] = []
    negative_matches = tweet.get("_monitor_negative_filter_matches")
    if isinstance(negative_matches, dict) and negative_matches:
        flags.append("negative_filter_match")
    skipped_reason = normalize_string(tweet.get("_monitor_skipped_reason"))
    if skipped_reason and skipped_reason not in flags:
        flags.append(skipped_reason)
    filter_stage = normalize_string(tweet.get("_monitor_filter_stage"))
    if filter_stage == "unqualified" and skipped_reason and "unqualified" not in flags:
        flags.append("unqualified")
    if tweet.get("possibly_sensitive") is True:
        flags.append("possibly_sensitive")
    if tweet.get("is_retweet") is True:
        flags.append("retweet")
    if tweet.get("in_reply_to_status_id"):
        flags.append("reply")
    return flags


def signal_score(tweet: dict) -> int:
    explicit = normalize_int(tweet.get("_monitor_score"), -1)
    if explicit >= 0:
        return max(0, min(explicit, 100))
    importance = normalize_string(tweet.get("_monitor_importance"), "normal")
    score = 90 if importance == "high" else 60
    score += min(normalize_int(tweet.get("like_count") or tweet.get("favorite_count"), 0), 20)
    score += min(normalize_int(tweet.get("retweet_count"), 0) * 2, 10)
    if signal_risk_flags(tweet):
        score -= 25
    return max(0, min(score, 100))


def build_normalized_signal(
    tweet: dict,
    *,
    observed_at: str | None = None,
    config: dict | None = None,
) -> dict:
    """Build the standalone X-Monitor v1 Signal contract from a monitor tweet."""
    if not isinstance(tweet, dict):
        raise TypeError("tweet must be a dict")

    source = normalize_string(tweet.get("_monitor_source") or tweet.get("source"), "unknown")
    external_id = tweet_external_id(tweet)
    source_url = tweet_source_url(tweet)
    created_at = normalize_string(tweet.get("created_at") or tweet.get("timestamp"))
    observed = normalize_string(observed_at or tweet.get("_monitor_observed_at") or created_at)
    if not observed:
        observed = datetime.now(timezone.utc).isoformat()

    basis = "|".join(part for part in ("x", source, external_id, source_url, created_at) if part)
    signal_id = normalize_string(tweet.get("_monitor_signal_id")) or hashlib.sha256(
        basis.encode("utf-8")
    ).hexdigest()[:24]

    signal_value_reason = normalize_string(tweet.get("_monitor_signal_value_reason"))
    context = normalize_string(tweet.get("_monitor_context"))
    why_parts = [f"Matched {source} watchlist"]
    if signal_value_reason and signal_value_reason != "No account-specific positive filter matched.":
        why_parts.append(signal_value_reason)
    if context:
        why_parts.append(context)
    why_now = "; ".join(why_parts)

    filter_stage = normalize_string(tweet.get("_monitor_filter_stage"), "candidate")
    matched_terms = signal_matched_terms(tweet, config=config)
    matched_accounts = signal_matched_accounts(tweet)

    return {
        "id": signal_id,
        "platform": "x",
        "source": source,
        "source_url": source_url,
        "external_id": external_id,
        "author": tweet_author(tweet),
        "content_snippet": content_snippet(tweet),
        "matched_terms": matched_terms,
        "matched_accounts": matched_accounts,
        "route_hints": normalize_string_list(tweet.get("_monitor_route_hints")),
        "score": signal_score(tweet),
        "why_now": why_now,
        "reason": why_now,
        "risk_flags": signal_risk_flags(tweet),
        "observed_at": observed,
        "created_at": created_at,
        "bucket": normalize_string(tweet.get("_monitor_bucket") or tweet.get("_monitor_category")),
        "lanes": normalize_string_list(tweet.get("_monitor_lanes")),
        "qualification": filter_stage,
        "collection_stage": normalize_string(tweet.get("_monitor_collection_stage"), "raw_collected"),
    }


def build_representative_signal_metadata(signals: list, limit: int = REPRESENTATIVE_SIGNAL_LIMIT) -> list[dict]:
    representative: list[dict] = []
    for signal in signals[:limit]:
        if not isinstance(signal, dict):
            continue
        tweet_id = str(signal.get("id") or signal.get("id_str") or "").strip()
        item = {
            "source": normalize_string(signal.get("_monitor_source")),
            "bucket": normalize_string(signal.get("_monitor_bucket") or signal.get("_monitor_category")),
            "lanes": normalize_string_list(signal.get("_monitor_lanes")),
            "route_hints": normalize_string_list(signal.get("_monitor_route_hints")),
            "importance": normalize_string(signal.get("_monitor_importance")),
            "context": normalize_string(signal.get("_monitor_context")),
            "collection_stage": normalize_string(signal.get("_monitor_collection_stage"), "raw_collected"),
            "filter_stage": normalize_string(
                signal.get("_monitor_filter_stage"),
                "candidate"
                if normalize_string_list(signal.get("_monitor_eligible_accounts"))
                else "unqualified",
            ),
            "eligible_accounts": normalize_string_list(signal.get("_monitor_eligible_accounts")),
            "account_goal_hint": (
                signal.get("_monitor_account_goal_hint")
                if isinstance(signal.get("_monitor_account_goal_hint"), dict)
                else {}
            ),
            "signal_value_reason": normalize_string(signal.get("_monitor_signal_value_reason")),
            "negative_filter_matches": (
                signal.get("_monitor_negative_filter_matches")
                if isinstance(signal.get("_monitor_negative_filter_matches"), dict)
                else {}
            ),
            "skipped_reason": normalize_string(signal.get("_monitor_skipped_reason")),
        }
        if tweet_id:
            item["tweet_id"] = tweet_id
        url = normalize_string(signal.get("url") or signal.get("tweet_url") or signal.get("expanded_url"))
        if url:
            item["url"] = url
        timestamp = normalize_string(signal.get("created_at") or signal.get("timestamp"))
        if timestamp:
            item["timestamp"] = timestamp
        representative.append(item)
    return representative


def iso_duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        return round((finished - started).total_seconds(), 3)
    except Exception:
        return None


def build_telemetry_output_summary(
    *,
    started_at: str,
    finished_at: str | None,
    status: str,
    stats: dict | None,
    emitted_signals: list | None,
    errors: list | None,
    pending_alerts_count: int | None,
) -> dict:
    stats_data = stats if isinstance(stats, dict) else {}
    output = {
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "duration_seconds": iso_duration_seconds(started_at, finished_at),
        "accounts_checked": normalize_int(stats_data.get("accounts_checked"), 0),
        "keywords_checked": normalize_int(stats_data.get("keywords_checked"), 0),
        "total_fetched": normalize_int(stats_data.get("total_fetched"), 0),
        "new_count": normalize_int(stats_data.get("new_count"), 0),
        "deduped_count": normalize_int(stats_data.get("deduped_count"), 0),
        "emitted_count": normalize_int(stats_data.get("emitted_count"), 0),
        "queued_count": normalize_int(stats_data.get("queued_count"), 0),
        "lanes_routed": stats_data.get("lanes_routed") if isinstance(stats_data.get("lanes_routed"), dict) else {},
        "source_errors": errors if isinstance(errors, list) else [],
        "raw_collected_count": normalize_int(stats_data.get("total_fetched"), 0),
        "candidate_signal_count": candidate_signal_count(emitted_signals),
        "publishable_signal_count": 0,
        "signal_stage_note": "x-monitor is passive: collected and candidate signals are not publishable drafts.",
        "representative_signals": build_representative_signal_metadata(emitted_signals or []),
    }
    if pending_alerts_count is not None:
        output["pending_alerts_count"] = pending_alerts_count
    return output


def build_social_runtime_event(
    *,
    lifecycle: str,
    status: str,
    mode: str,
    run_id: str,
    started_at: str,
    finished_at: str | None = None,
    config: dict | None = None,
    stats: dict | None = None,
    emitted_signals: list | None = None,
    errors: list | None = None,
    pending_alerts_count: int | None = None,
    message: str | None = None,
) -> dict:
    source_errors = errors if isinstance(errors, list) else []
    event_type = "info"
    if lifecycle == "error" or status == "error":
        event_type = "error"
    elif lifecycle == "skipped" or source_errors:
        event_type = "warn"

    metadata = {
        "run_id": run_id,
        "lifecycle": lifecycle,
        "status": status,
        "mode": mode,
        "input": build_telemetry_input_snapshot(config),
        "output": build_telemetry_output_summary(
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            stats=stats,
            emitted_signals=emitted_signals,
            errors=source_errors,
            pending_alerts_count=pending_alerts_count,
        ),
    }
    return {
        "service": SOCIAL_RUNTIME_SERVICE,
        "event_type": event_type,
        "message": (message or f"x-monitor {lifecycle}: {status} ({mode})")[:500],
        "metadata": metadata,
        "created_at": finished_at or started_at,
    }


def emit_social_runtime_events(events: list[dict]) -> bool:
    """Best-effort batch insert into Social OS social_runtime_events."""
    if not events:
        return False

    supabase_url = first_env_value(SUPABASE_URL_ENV_VARS)
    supabase_key = first_env_value(SUPABASE_ANON_KEY_ENV_VARS)
    if not supabase_url or not supabase_key:
        print(
            "Social OS telemetry skipped: missing Supabase URL/key "
            "(set SOCIAL_OS_SUPABASE_URL and SOCIAL_OS_SUPABASE_ANON_KEY)",
            file=sys.stderr,
        )
        return False

    rows: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    for event in events:
        if not isinstance(event, dict):
            continue
        rows.append({
            "service": normalize_string(event.get("service"), SOCIAL_RUNTIME_SERVICE),
            "event_type": normalize_string(event.get("event_type"), "info"),
            "message": normalize_string(event.get("message"), "x-monitor runtime event")[:500],
            "metadata": event.get("metadata") if isinstance(event.get("metadata"), dict) else {},
            "created_at": normalize_string(event.get("created_at"), now),
        })
    if not rows:
        return False

    request = urllib.request.Request(
        build_social_runtime_events_url(supabase_url),
        data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=SUPABASE_WRITE_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            if int(status) >= 400:
                raise OSError(f"HTTP {status}")
        return True
    except Exception as exc:
        print(f"Social OS telemetry failed: {exc}", file=sys.stderr)
        return False


def resolve_run_mode(args: argparse.Namespace) -> str:
    if getattr(args, "dry_run", False):
        return "dry-run"
    raw = os.environ.get("X_MONITOR_RUN_MODE", "").strip().lower()
    if raw in {"cron", "manual"}:
        return raw
    return "manual"


def new_run_id(started_at: str) -> str:
    compact = started_at.replace("+00:00", "Z").replace(":", "").replace("-", "")
    return f"x-monitor-{compact}-{os.getpid()}"


def fetch_live_social_runtime_config_row() -> dict | None:
    supabase_url = first_env_value(SUPABASE_URL_ENV_VARS)
    anon_key = first_env_value(SUPABASE_ANON_KEY_ENV_VARS)
    if not supabase_url or not anon_key:
        return None

    request = urllib.request.Request(
        build_social_runtime_config_url(supabase_url),
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=SUPABASE_FETCH_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None


def lanes_for_account(handle: str, lane_routing: object) -> list[str]:
    if not isinstance(lane_routing, dict):
        return []

    lanes: list[str] = []
    for lane_id, handles in lane_routing.items():
        normalized_lane = normalize_string(lane_id)
        if not normalized_lane:
            continue
        normalized_handles = normalize_string_list(handles)
        if handle in normalized_handles and normalized_lane not in lanes:
            lanes.append(normalized_lane)
    return lanes


def project_social_runtime_config_row(row: dict) -> dict | None:
    if any(isinstance(row.get(key), dict) for key in ("config", "xMonitor", "x_monitor")):
        projected = project_managed_runtime(row)
        if projected:
            return projected

    watchlist_terms = normalize_string_list(row.get("watchlist_terms"))
    watchlist_accounts = normalize_string_list(row.get("watchlist_accounts"))
    lane_routing = (
        row.get("lane_routing") if isinstance(row.get("lane_routing"), dict) else {}
    )
    if not watchlist_terms and not watchlist_accounts and not lane_routing:
        return None

    lanes = [
        {
            "id": lane_id,
            "name": lane_id.replace("_", " ").title(),
            "route_hint": f"x-engage/{lane_id}",
            "buckets": [],
        }
        for lane_id in normalize_string_list(list(lane_routing.keys()))
    ]
    accounts = [
        {
            "username": handle,
            "bucket": "general",
            "importance": "high",
            "lanes": lanes_for_account(handle, lane_routing),
            "context": "",
            "include_retweets": False,
        }
        for handle in watchlist_accounts
    ]
    keywords = [
        {
            "query": term,
            "bucket": "keyword",
            "importance": "high",
            "lanes": [],
            "context": "",
        }
        for term in watchlist_terms
    ]
    return {
        "lanes": lanes,
        "accounts": accounts,
        "keywords": keywords,
        "discord": {"alerts_channel": DEFAULT_DISCORD_CHANNEL},
        "filters": normalize_filters({}),
        "account_filters": normalize_account_filter_map(
            row.get("account_filters") or row.get("account_strategy") or row.get("account_routing")
        ),
        "account_goals": normalize_account_goal_map(row.get("account_goals")),
    }


def load_live_social_runtime_config() -> dict | None:
    row = fetch_live_social_runtime_config_row()
    if row is None:
        return None
    return project_social_runtime_config_row(row)



def find_managed_runtime_path() -> Path | None:
    for env_name in MANAGED_RUNTIME_PATH_ENV_VARS:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"Managed runtime path from {env_name} does not exist: {path}"
            )
        return path

    for candidate in DEFAULT_MANAGED_RUNTIME_PATHS:
        path = candidate.expanduser()
        if path.exists():
            return path

    return None


def is_projection_shape(payload: dict) -> bool:
    return all(key in payload for key in ("lanes", "accounts", "keywords"))


def normalize_lane_projection(entry: dict) -> dict:
    lane_id = normalize_string(entry.get("id"))
    if not lane_id:
        return {}
    return {
        "id": lane_id,
        "name": normalize_string(entry.get("name"), lane_id),
        "route_hint": normalize_string(entry.get("route_hint")),
        "buckets": normalize_string_list(entry.get("buckets")),
    }


def normalize_account_projection(entry: dict) -> dict:
    username = normalize_string(entry.get("username"))
    if not username:
        return {}
    return {
        "username": username,
        "bucket": normalize_string(entry.get("bucket"), "general"),
        "importance": normalize_string(entry.get("importance"), "high"),
        "lanes": normalize_string_list(entry.get("lanes")),
        "context": normalize_string(entry.get("context")),
        "include_retweets": normalize_bool(entry.get("include_retweets")),
    }


def normalize_keyword_projection(entry: dict) -> dict:
    query = normalize_string(entry.get("query"))
    if not query:
        return {}
    return {
        "query": query,
        "bucket": normalize_string(entry.get("bucket"), "keyword"),
        "importance": normalize_string(entry.get("importance"), "high"),
        "lanes": normalize_string_list(entry.get("lanes")),
        "context": normalize_string(entry.get("context")),
    }


def normalize_filters(filters: object) -> dict:
    data = filters if isinstance(filters, dict) else {}
    return {
        "normal_importance_min_likes": normalize_int(data.get("normal_importance_min_likes", 0) or 0),
        "skip_replies": normalize_bool(data.get("skip_replies")),
        "skip_retweets_for_normal": normalize_bool(data.get("skip_retweets_for_normal")),
    }


def build_x_monitor_projection_from_contract(contract: dict) -> dict:
    lanes = [
        lane
        for lane in (
            normalize_lane_projection(entry)
            for entry in contract.get("lanes", [])
            if isinstance(entry, dict)
        )
        if lane
    ]

    accounts: list[dict] = []
    keywords: list[dict] = []
    for entry in contract.get("watchlists", []):
        if not isinstance(entry, dict):
            continue

        kind = normalize_string(entry.get("kind"), "account")
        base = {
            "bucket": normalize_string(entry.get("bucket"), "general" if kind == "account" else "keyword"),
            "importance": normalize_string(entry.get("importance"), "high"),
            "lanes": normalize_string_list(entry.get("lanes")),
            "context": normalize_string(entry.get("context")),
        }

        if kind == "keyword":
            projected = normalize_keyword_projection({
                "query": entry.get("value"),
                **base,
            })
            if projected:
                keywords.append(projected)
            continue

        projected = normalize_account_projection({
            "username": entry.get("value"),
            "include_retweets": entry.get("include_retweets"),
            **base,
        })
        if projected:
            accounts.append(projected)

    service_settings: dict = {}
    for service in contract.get("services", []):
        if not isinstance(service, dict):
            continue
        if normalize_string(service.get("id")) == "x-monitor":
            settings = service.get("settings")
            if isinstance(settings, dict):
                service_settings = settings
            break

    defaults = contract.get("defaults") if isinstance(contract.get("defaults"), dict) else {}
    discord_channel = normalize_string(
        service_settings.get("discord_channel_id") or defaults.get("discord_channel_id"),
        DEFAULT_DISCORD_CHANNEL,
    )

    return {
        "lanes": lanes,
        "accounts": accounts,
        "keywords": keywords,
        "discord": {"alerts_channel": discord_channel},
        "filters": normalize_filters(service_settings.get("filters")),
        "account_filters": normalize_account_filter_map(
            contract.get("account_filters")
            or contract.get("account_strategy")
            or contract.get("account_routing")
        ),
        "account_goals": normalize_account_goal_map(contract.get("account_goals")),
    }


def project_managed_runtime(payload: dict) -> dict | None:
    for projection_key in ("xMonitor", "x_monitor"):
        nested_projection = payload.get(projection_key)
        if isinstance(nested_projection, dict):
            projected = project_managed_runtime(nested_projection)
            if projected:
                return projected

    nested_contract = payload.get("config")
    if isinstance(nested_contract, dict):
        projected = project_managed_runtime(nested_contract)
        if projected:
            return projected

    if is_projection_shape(payload):
        lanes = [
            lane
            for lane in (
                normalize_lane_projection(entry)
                for entry in payload.get("lanes", [])
                if isinstance(entry, dict)
            )
            if lane
        ]
        accounts = [
            account
            for account in (
                normalize_account_projection(entry)
                for entry in payload.get("accounts", [])
                if isinstance(entry, dict)
            )
            if account
        ]
        keywords = [
            keyword
            for keyword in (
                normalize_keyword_projection(entry)
                for entry in payload.get("keywords", [])
                if isinstance(entry, dict)
            )
            if keyword
        ]
        discord = payload.get("discord") if isinstance(payload.get("discord"), dict) else {}
        return {
            "lanes": lanes,
            "accounts": accounts,
            "keywords": keywords,
            "discord": {
                "alerts_channel": normalize_string(discord.get("alerts_channel"), DEFAULT_DISCORD_CHANNEL),
            },
            "filters": normalize_filters(payload.get("filters")),
            "account_filters": normalize_account_filter_map(
                payload.get("account_filters")
                or payload.get("account_strategy")
                or payload.get("account_routing")
            ),
            "account_goals": normalize_account_goal_map(payload.get("account_goals")),
        }

    if isinstance(payload.get("watchlists"), list) and isinstance(payload.get("lanes"), list):
        return build_x_monitor_projection_from_contract(payload)

    return None


def load_config() -> dict:
    live_runtime_config = load_live_social_runtime_config()
    if live_runtime_config is not None:
        return live_runtime_config

    managed_runtime_path = find_managed_runtime_path()
    if managed_runtime_path is not None:
        managed_payload = read_json_file(managed_runtime_path)
        projected = project_managed_runtime(managed_payload)
        if projected is None:
            raise ValueError(
                f"Unsupported Social OS managed runtime payload in {managed_runtime_path}"
            )
        return projected

    return json.loads(CONFIG_FILE.read_text())


def get_lane_for_bucket(bucket: str, config: dict) -> list[str]:
    """Determine which lanes a bucket belongs to based on config."""
    lanes: list[str] = []
    for lane_def in config.get("lanes", []):
        lane_id = lane_def.get("id")
        if bucket in lane_def.get("buckets", []) and lane_id and lane_id not in lanes:
            lanes.append(lane_id)
    return lanes


def get_route_hint_for_lane(lane_id: str, config: dict) -> str | None:
    """Get the route_hint for a lane from config."""
    for lane_def in config.get("lanes", []):
        if lane_def["id"] == lane_id:
            return lane_def.get("route_hint")
    return None


def resolve_lanes(account_or_kw: dict, bucket: str, config: dict) -> list[str]:
    """Resolve lanes from config: prefer explicit lanes, fallback to bucket mapping."""
    explicit = normalize_string_list(account_or_kw.get("lanes"))
    if explicit:
        return explicit
    return get_lane_for_bucket(bucket, config)


def resolve_route_hints(lanes: list[str], config: dict) -> list[str]:
    """Resolve route hints from lane definitions."""
    hints: list[str] = []
    for lane_id in lanes:
        hint = get_route_hint_for_lane(lane_id, config)
        if hint and hint not in hints:
            hints.append(hint)
    return hints


def load_pending_alerts() -> list:
    if not PENDING_ALERTS_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_ALERTS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def merge_pending_alerts(existing: list, new_items: list) -> list:
    merged: list = []
    seen_ids: set[str] = set()

    for tweet in existing + new_items:
        tid = str(tweet.get("id") or tweet.get("id_str") or "")
        if tid:
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
        merged.append(tweet)

    return merged


def run_desearch(args: list) -> tuple[dict | list | None, str | None]:
    """Run the desearch.py script and return parsed JSON result."""
    api_key = os.environ.get("DESEARCH_API_KEY")
    if not api_key:
        return None, "DESEARCH_API_KEY not set"

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

    if importance == "high":
        return True

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


def normalize_tweet(
    tweet: dict,
    source: str,
    bucket: str,
    importance: str,
    context: str,
    lanes: list,
    route_hints: list,
    config: dict | None = None,
) -> dict:
    """
    Add v2 monitor metadata to a tweet dict.

    New v2 fields:
    - _monitor_source: source identifier
    - _monitor_bucket: watchlist bucket
    - _monitor_category: backward compat
    - _monitor_importance: importance level
    - _monitor_context: context from config
    - _monitor_lanes: list of lanes this tweet belongs to
    - _monitor_route_hints: routing instructions for downstream
    """
    tweet["_monitor_source"] = source
    tweet["_monitor_bucket"] = bucket
    tweet["_monitor_category"] = bucket
    tweet["_monitor_importance"] = importance
    tweet["_monitor_context"] = context
    tweet["_monitor_lanes"] = lanes
    tweet["_monitor_route_hints"] = route_hints
    eligibility = build_account_eligibility_metadata(tweet, config)
    tweet["_monitor_collection_stage"] = eligibility["collection_stage"]
    tweet["_monitor_filter_stage"] = eligibility["filter_stage"]
    tweet["_monitor_eligible_accounts"] = eligibility["eligible_accounts"]
    tweet["_monitor_account_goal_hint"] = eligibility["account_goal_hint"]
    tweet["_monitor_signal_value_reason"] = eligibility["signal_value_reason"]
    tweet["_monitor_negative_filter_matches"] = eligibility["negative_filter_matches"]
    tweet["_monitor_skipped_reason"] = eligibility["skipped_reason"]

    ca = tweet.get("created_at", "")
    if ca and not ca[0].isdigit():
        iso = parse_twitter_date(ca)
        if iso:
            tweet["created_at"] = iso
    return tweet


def main():
    parser = argparse.ArgumentParser(description="X Monitor v2")
    parser.add_argument("--reset", action="store_true", help="Clear state and re-fetch all")
    parser.add_argument("--dry-run", action="store_true", help="Don't save state")
    parser.add_argument("--lane-filter", type=str, default=None, help="Only emit tweets for this lane (founder|brand)")
    args = parser.parse_args()

    load_env()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = new_run_id(started_at)
    mode = resolve_run_mode(args)
    config: dict | None = None
    stats: dict = {
        "accounts_checked": 0,
        "keywords_checked": 0,
        "total_fetched": 0,
        "new_count": 0,
        "deduped_count": 0,
        "emitted_count": 0,
        "queued_count": 0,
        "lanes_routed": {},
    }
    errors: list = []
    new_tweets: list = []
    pending_alerts_count: int | None = None

    lock_handle = None
    if not args.dry_run:
        try:
            lock_handle = acquire_lock(MONITOR_LOCK_FILE)
        except RuntimeError as exc:
            finished_at = datetime.now(timezone.utc).isoformat()
            error_payload = [{"source": "lock", "error": str(exc)}]
            emit_social_runtime_events([
                build_social_runtime_event(
                    lifecycle="skipped",
                    status="skipped",
                    mode=mode,
                    run_id=run_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    config=None,
                    stats=stats,
                    emitted_signals=[],
                    errors=error_payload,
                    pending_alerts_count=None,
                    message=f"x-monitor skipped: lock already held ({mode})",
                )
            ])
            print(json.dumps({"status": "skipped", "reason": str(exc), "lock_file": str(MONITOR_LOCK_FILE)}, indent=2))
            sys.exit(0)

    try:
        config = load_config()
        emit_social_runtime_events([
            build_social_runtime_event(
                lifecycle="started",
                status="running",
                mode=mode,
                run_id=run_id,
                started_at=started_at,
                config=config,
                stats=stats,
                emitted_signals=[],
                errors=[],
                pending_alerts_count=None,
            )
        ])

        state = load_state() if not args.reset else {"seen_ids": {}, "last_run": None}

        seen_ids: dict = state.get("seen_ids", {})
        all_window_tweets: list = []

        global_filters = config.get("filters", {})

        for account in config.get("accounts", []):
            username = account["username"]
            bucket = account.get("bucket", "general")
            key = f"timeline:{username}"
            seen = set(seen_ids.get(key, []))

            tweets, err = get_timeline(username, count=50)
            stats["accounts_checked"] += 1

            if err:
                errors.append({"source": f"@{username}", "error": err})
                continue

            stats["total_fetched"] += len(tweets)
            new_for_account = []
            new_ids = []
            for tweet in tweets:
                tid = str(tweet.get("id") or tweet.get("id_str") or "")
                if not tid:
                    continue

                lanes = resolve_lanes(account, bucket, config)
                route_hints = resolve_route_hints(lanes, config)

                t_copy = dict(tweet)
                normalize_tweet(t_copy, f"account:{username}", bucket, account.get("importance", "normal"), account.get("context", ""), lanes, route_hints, config=config)
                all_window_tweets.append(t_copy)

                if tid in seen:
                    stats["deduped_count"] += 1
                    continue
                stats["new_count"] += 1

                if tweet.get("is_retweet") and not account.get("include_retweets", False):
                    new_ids.append(tid)
                    continue

                if global_filters.get("skip_replies", True) and tweet.get("in_reply_to_status_id"):
                    new_ids.append(tid)
                    continue

                if is_important_enough(tweet, account, global_filters):
                    normalize_tweet(tweet, f"account:{username}", bucket, account.get("importance", "normal"), account.get("context", ""), lanes, route_hints, config=config)
                    new_for_account.append(tweet)

                    for lane in lanes:
                        stats["lanes_routed"][lane] = stats["lanes_routed"].get(lane, 0) + 1

                new_ids.append(tid)

            all_ids = list(seen) + new_ids
            seen_ids[key] = all_ids[-500:]
            new_tweets.extend(new_for_account)

        for kw in config.get("keywords", []):
            query = kw["query"]
            bucket = kw.get("bucket", "keyword")
            key = f"keyword:{query}"
            seen = set(seen_ids.get(key, []))

            tweets, err = search_keyword(query, count=25)
            stats["keywords_checked"] += 1

            if err:
                errors.append({"source": f"keyword:{query}", "error": err})
                continue

            stats["total_fetched"] += len(tweets)
            new_for_kw = []
            new_ids = []
            for tweet in tweets:
                tid = str(tweet.get("id") or tweet.get("id_str") or "")
                if not tid:
                    continue

                lanes = resolve_lanes(kw, bucket, config)
                route_hints = resolve_route_hints(lanes, config)

                t_copy = dict(tweet)
                normalize_tweet(t_copy, f"keyword:{query}", bucket, kw.get("importance", "high"), kw.get("context", ""), lanes, route_hints, config=config)
                all_window_tweets.append(t_copy)

                if tid in seen:
                    stats["deduped_count"] += 1
                    continue
                stats["new_count"] += 1

                normalize_tweet(tweet, f"keyword:{query}", bucket, kw.get("importance", "high"), kw.get("context", ""), lanes, route_hints, config=config)
                new_for_kw.append(tweet)
                new_ids.append(tid)

                for lane in lanes:
                    stats["lanes_routed"][lane] = stats["lanes_routed"].get(lane, 0) + 1

            all_ids = list(seen) + new_ids
            seen_ids[key] = all_ids[-500:]
            new_tweets.extend(new_for_kw)

        if args.lane_filter:
            new_tweets = [t for t in new_tweets if args.lane_filter in t.get("_monitor_lanes", [])]

        stats["emitted_count"] = len(new_tweets)
        if not args.dry_run:
            stats["queued_count"] = len(new_tweets)
            state["seen_ids"] = seen_ids
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            save_state(state)

        finished_at = datetime.now(timezone.utc).isoformat()
        status = "finished_with_errors" if errors else "success"
        output = {
            "timestamp": finished_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "duration_seconds": iso_duration_seconds(started_at, finished_at),
            "stats": stats,
            "total_new": len(new_tweets),
            "new_tweets": new_tweets,
            "errors": errors,
            "v2_lanes": [lane["id"] for lane in config.get("lanes", [])],
        }

        if not args.dry_run:
            existing_window = load_window()
            merged = existing_window + all_window_tweets
            window_count = save_window(merged)
            output["window_updated"] = window_count

            with acquire_lock(PENDING_ALERTS_LOCK_FILE):
                existing_pending = load_pending_alerts()
                merged_pending = merge_pending_alerts(existing_pending, new_tweets)
                atomic_write_json(PENDING_ALERTS_FILE, merged_pending)
                pending_alerts_count = len(merged_pending)
                output["pending_alerts"] = pending_alerts_count

        telemetry_submitted = emit_social_runtime_events([
            build_social_runtime_event(
                lifecycle="finished",
                status=status,
                mode=mode,
                run_id=run_id,
                started_at=started_at,
                finished_at=finished_at,
                config=config,
                stats=stats,
                emitted_signals=new_tweets,
                errors=errors,
                pending_alerts_count=pending_alerts_count,
            )
        ])
        output["telemetry"] = {"social_os_events_submitted": telemetry_submitted}

        print(json.dumps(output, indent=2, ensure_ascii=False))
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        errors = errors or []
        errors.append({"source": "x-monitor", "error": str(exc)})
        emit_social_runtime_events([
            build_social_runtime_event(
                lifecycle="error",
                status="error",
                mode=mode,
                run_id=run_id,
                started_at=started_at,
                finished_at=finished_at,
                config=config,
                stats=stats,
                emitted_signals=new_tweets,
                errors=errors,
                pending_alerts_count=pending_alerts_count,
                message=f"x-monitor error: {str(exc)[:420]}",
            )
        ])
        raise
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    main()
