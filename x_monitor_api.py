#!/usr/bin/env python3
"""Standalone local X-Monitor watchlist and Signal API/UI v1.

The server is intentionally local-first and file-backed. It never starts X
collection from UI/API reads and it does not expose publishing, account auth,
approval, scheduling, or execution controls.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import monitor

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
CONFIG_PATH_ENV = "X_MONITOR_ADMIN_CONFIG_PATH"
SIGNALS_PATH_ENV = "X_MONITOR_SIGNALS_PATH"
PENDING_PATH_ENV = "X_MONITOR_PENDING_SIGNALS_PATH"
MAX_BODY_BYTES = 64 * 1024
SAFE_KINDS = {"account", "keyword", "mention", "list"}
BLOCKED_FIELDS = {
    "account_auth",
    "auth",
    "approval",
    "approval_required",
    "credentials",
    "cookie",
    "cookies",
    "execute",
    "execution",
    "oauth",
    "password",
    "publish",
    "publisher",
    "schedule",
    "session",
    "token",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


def config_path_from_env() -> Path:
    return Path(os.environ.get(CONFIG_PATH_ENV, monitor.CONFIG_FILE)).expanduser()


def signals_path_from_env() -> Path:
    return Path(os.environ.get(SIGNALS_PATH_ENV, monitor.WINDOW_FILE)).expanduser()


def pending_path_from_env() -> Path:
    return Path(os.environ.get(PENDING_PATH_ENV, monitor.PENDING_ALERTS_FILE)).expanduser()


def read_config(path: Path) -> dict:
    if not path.exists():
        raise ApiError(404, f"config path does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(500, f"invalid JSON in config path: {path}", {"error": str(exc)}) from exc
    if not isinstance(data, dict):
        raise ApiError(500, "config JSON must be an object")
    data.setdefault("lanes", [])
    data.setdefault("accounts", [])
    data.setdefault("keywords", [])
    data.setdefault("lists", [])
    return data


def write_config(path: Path, config: dict) -> None:
    monitor.atomic_write_json(path, config)


def item_id(kind: str, value: str) -> str:
    normalized = value.strip().lower().lstrip("@") if kind == "account" else value.strip().lower()
    digest = hashlib.sha256(f"{kind}:{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def route_hints_for_lanes(lanes: list[str], config: dict) -> list[str]:
    return monitor.resolve_route_hints(lanes, config)


def route_hint_label(hint: str, lane_id: str) -> dict:
    normalized = monitor.normalize_string(hint)
    lane = monitor.normalize_string(lane_id)
    if normalized.startswith("x-engage/"):
        suffix = normalized.removeprefix("x-engage/")
        display_label = f"socialos/{suffix or lane}"
        legacy_alias = True
    elif normalized.startswith("socialos/"):
        display_label = normalized
        legacy_alias = False
    elif lane:
        display_label = f"socialos/{lane}"
        legacy_alias = False
    else:
        display_label = normalized
        legacy_alias = False
    return {
        "hint": normalized,
        "display_label": display_label,
        "lane": lane,
        "source": "lane_config",
        "legacy_internal_alias": legacy_alias,
    }


def route_hint_labels(lanes: list[str], config: dict) -> list[dict]:
    labels: list[dict] = []
    seen: set[str] = set()
    for lane_id in lanes:
        hint = monitor.get_route_hint_for_lane(lane_id, config) or ""
        if not hint or hint in seen:
            continue
        seen.add(hint)
        labels.append(route_hint_label(hint, lane_id))
    return labels


def normalize_item_kind(kind: object, value: object = "") -> str:
    normalized = monitor.normalize_string(kind).lower()
    if normalized:
        if normalized not in SAFE_KINDS:
            raise ApiError(400, f"unsupported watchlist kind: {normalized}")
        return normalized
    raw_value = monitor.normalize_string(value)
    if raw_value.startswith("@"):
        return "mention"
    return "keyword"


def config_entry_to_item(kind: str, entry: dict, config: dict) -> dict:
    if kind == "account":
        value = monitor.normalize_string(entry.get("username")).lstrip("@")
    elif kind in {"keyword", "mention"}:
        value = monitor.normalize_string(entry.get("query"))
    else:
        value = monitor.normalize_string(entry.get("value") or entry.get("name") or entry.get("url"))
    bucket = monitor.normalize_string(entry.get("bucket"), "general" if kind == "account" else "keyword")
    explicit_lanes = monitor.normalize_string_list(entry.get("lanes"))
    lanes = explicit_lanes or monitor.get_lane_for_bucket(bucket, config)
    return {
        "id": item_id(kind, value),
        "kind": kind,
        "value": value,
        "bucket": bucket,
        "lanes": lanes,
        "lane_source": "explicit_item_config" if explicit_lanes else "bucket_fallback",
        "route_hints": route_hints_for_lanes(lanes, config),
        "route_hint_labels": route_hint_labels(lanes, config),
        "importance": monitor.normalize_string(entry.get("importance"), "high"),
        "context": monitor.normalize_string(entry.get("context")),
        "include_retweets": monitor.normalize_bool(entry.get("include_retweets")),
    }


def split_keyword_items(config: dict) -> tuple[list[dict], list[dict]]:
    keywords: list[dict] = []
    mentions: list[dict] = []
    for entry in config.get("keywords", []):
        if not isinstance(entry, dict):
            continue
        query = monitor.normalize_string(entry.get("query"))
        if not query:
            continue
        kind = "mention" if query.startswith("@") else "keyword"
        item = config_entry_to_item(kind, entry, config)
        (mentions if kind == "mention" else keywords).append(item)
    return keywords, mentions


def load_watchlist(config_path: Path | None = None) -> dict:
    path = config_path or config_path_from_env()
    config = read_config(path)
    accounts = [
        config_entry_to_item("account", entry, config)
        for entry in config.get("accounts", [])
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("username"))
    ]
    keywords, mentions = split_keyword_items(config)
    lists = [
        config_entry_to_item("list", entry, config)
        for entry in config.get("lists", [])
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("value") or entry.get("name") or entry.get("url"))
    ]
    lane_summary = {
        monitor.normalize_string(lane.get("id")): {
            "name": monitor.normalize_string(lane.get("name")),
            "buckets": monitor.normalize_string_list(lane.get("buckets")),
            "route_hint": monitor.normalize_string(lane.get("route_hint")),
        }
        for lane in config.get("lanes", [])
        if isinstance(lane, dict) and monitor.normalize_string(lane.get("id"))
    }
    route_hints = [lane["route_hint"] for lane in lane_summary.values() if lane.get("route_hint")]
    return {
        "version": "v1",
        "persistence_path": str(path),
        "accounts": accounts,
        "keywords": keywords,
        "mentions": mentions,
        "lists": lists,
        "counts": {
            "accounts": len(accounts),
            "keywords": len(keywords),
            "mentions": len(mentions),
            "lists": len(lists),
        },
        "lanes": lane_summary,
        "route_hints": route_hints,
        "agent_setup": {
            "service": "x-monitor",
            "default_channel_id": monitor.DEFAULT_DISCORD_CHANNEL,
            "signal_endpoint": "/api/signals",
            "watchlist_endpoint": "/api/watchlist",
            "boundary": "passive signal intelligence only; Socialos owns publishing/account auth/approval/execution",
        },
    }


def reject_blocked_fields(payload: dict) -> None:
    blocked = sorted(field for field in payload if field.lower() in BLOCKED_FIELDS)
    if blocked:
        raise ApiError(
            400,
            "Publishing/account-auth fields are outside X-Monitor and belong in Socialos.",
            {"blocked_fields": blocked},
        )


def normalize_lanes(value: object) -> list[str]:
    if isinstance(value, str):
        return monitor.normalize_string_list([part.strip() for part in value.split(",")])
    return monitor.normalize_string_list(value)


def validate_item_payload(payload: dict, *, existing: dict | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ApiError(400, "request body must be a JSON object")
    reject_blocked_fields(payload)
    base = existing or {}
    kind = normalize_item_kind(payload.get("kind", base.get("kind", "")), payload.get("value", base.get("value", "")))
    value = monitor.normalize_string(payload.get("value", base.get("value", "")))
    if kind == "account":
        value = value.lstrip("@")
    if kind == "mention" and value and not value.startswith("@"):
        value = f"@{value.lstrip('@')}"
    if not value:
        raise ApiError(400, "watchlist item value is required")
    if len(value) > 240:
        raise ApiError(400, "watchlist item value is too long")
    if kind in {"account", "mention"} and not re.match(r"^@?[A-Za-z0-9_]{1,30}$", value):
        raise ApiError(400, "account/mention value must be a valid X handle")
    return {
        "kind": kind,
        "value": value,
        "bucket": monitor.normalize_string(payload.get("bucket", base.get("bucket", "general" if kind == "account" else "keyword"))),
        "lanes": normalize_lanes(payload.get("lanes", base.get("lanes", []))),
        "importance": monitor.normalize_string(payload.get("importance", base.get("importance", "high"))),
        "context": monitor.normalize_string(payload.get("context", base.get("context", ""))),
        "include_retweets": monitor.normalize_bool(payload.get("include_retweets", base.get("include_retweets", False))),
    }


def find_item_location(config: dict, wanted_id: str) -> tuple[str, int, dict] | None:
    for collection_name, kind in (("accounts", "account"), ("keywords", "keyword"), ("lists", "list")):
        for index, entry in enumerate(config.get(collection_name, [])):
            if not isinstance(entry, dict):
                continue
            actual_kind = kind
            if collection_name == "keywords" and monitor.normalize_string(entry.get("query")).startswith("@"):
                actual_kind = "mention"
            item = config_entry_to_item(actual_kind, entry, config)
            if item["id"] == wanted_id:
                return collection_name, index, item
    return None


def entry_from_item(item: dict) -> tuple[str, dict]:
    kind = item["kind"]
    common = {
        "bucket": item["bucket"],
        "importance": item["importance"],
        "lanes": item["lanes"],
        "context": item["context"],
    }
    if kind == "account":
        return "accounts", {"username": item["value"].lstrip("@"), "include_retweets": item["include_retweets"], **common}
    if kind in {"keyword", "mention"}:
        return "keywords", {"query": item["value"], **common}
    return "lists", {"value": item["value"], **common}


def iter_watchlist_items(config: dict):
    for entry in config.get("accounts", []):
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("username")):
            yield config_entry_to_item("account", entry, config)
    keywords, mentions = split_keyword_items(config)
    yield from keywords
    yield from mentions
    for entry in config.get("lists", []):
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("value") or entry.get("name") or entry.get("url")):
            yield config_entry_to_item("list", entry, config)


def ensure_no_duplicate(config: dict, item: dict, *, excluding_id: str | None = None) -> None:
    for candidate in iter_watchlist_items(config):
        if excluding_id and candidate["id"] == excluding_id:
            continue
        if candidate["kind"] == item["kind"] and candidate["value"].lower() == item["value"].lower():
            raise ApiError(409, "watchlist item already exists", {"id": candidate["id"]})


def add_watchlist_item(config_path: Path, payload: dict) -> dict:
    config = read_config(config_path)
    item = validate_item_payload(payload)
    ensure_no_duplicate(config, item)
    collection, entry = entry_from_item(item)
    config.setdefault(collection, []).append(entry)
    write_config(config_path, config)
    return config_entry_to_item(item["kind"], entry, read_config(config_path))


def update_watchlist_item(config_path: Path, watchlist_id: str, payload: dict) -> dict:
    config = read_config(config_path)
    location = find_item_location(config, watchlist_id)
    if location is None:
        raise ApiError(404, "watchlist item not found")
    collection, index, existing = location
    updated = validate_item_payload(payload, existing=existing)
    ensure_no_duplicate(config, updated, excluding_id=watchlist_id)
    new_collection, entry = entry_from_item(updated)
    if new_collection == collection:
        config[collection][index] = entry
    else:
        del config[collection][index]
        config.setdefault(new_collection, []).append(entry)
    write_config(config_path, config)
    return config_entry_to_item(updated["kind"], entry, read_config(config_path))


def remove_watchlist_item(config_path: Path, watchlist_id: str) -> dict:
    config = read_config(config_path)
    location = find_item_location(config, watchlist_id)
    if location is None:
        raise ApiError(404, "watchlist item not found")
    collection, index, item = location
    del config[collection][index]
    write_config(config_path, config)
    return item




def iter_config_watchlist_entries(config: dict):
    for entry in config.get("accounts", []):
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("username")):
            yield "account", entry, config_entry_to_item("account", entry, config)
    for entry in config.get("keywords", []):
        if not isinstance(entry, dict) or not monitor.normalize_string(entry.get("query")):
            continue
        kind = "mention" if monitor.normalize_string(entry.get("query")).startswith("@") else "keyword"
        yield kind, entry, config_entry_to_item(kind, entry, config)
    for entry in config.get("lists", []):
        if isinstance(entry, dict) and monitor.normalize_string(entry.get("value") or entry.get("name") or entry.get("url")):
            yield "list", entry, config_entry_to_item("list", entry, config)


def source_value(signal: dict, prefix: str) -> str:
    source = monitor.normalize_string(signal.get("source"))
    if not source.startswith(prefix):
        return ""
    return monitor.normalize_string(source.removeprefix(prefix)).lstrip("@")


def watchlist_item_matches_signal(item: dict, signal: dict) -> bool:
    kind = item.get("kind")
    value = monitor.normalize_string(item.get("value")).lstrip("@")
    if not value:
        return False
    lower_value = value.lower()
    matched_terms = {term.lower().lstrip("@") for term in signal.get("matched_terms", []) if isinstance(term, str)}
    matched_accounts = {account.lower().lstrip("@") for account in signal.get("matched_accounts", []) if isinstance(account, str)}
    if kind == "account":
        return source_value(signal, "account:").lower() == lower_value or lower_value in matched_accounts
    if kind in {"keyword", "mention"}:
        return source_value(signal, "keyword:").lower() == lower_value or lower_value in matched_terms
    if kind == "list":
        return source_value(signal, "list:").lower() == lower_value
    return False


def public_watchlist_item_for_provenance(item: dict, config: dict) -> dict:
    lanes = monitor.normalize_string_list(item.get("lanes"))
    return {
        "id": item.get("id"),
        "kind": item.get("kind"),
        "value": item.get("value"),
        "bucket": item.get("bucket"),
        "lanes": lanes,
        "lane_source": item.get("lane_source"),
        "route_hints": route_hint_labels(lanes, config),
        "importance": item.get("importance"),
        "context": item.get("context"),
    }


def infer_lane_source(signal: dict, matched_items: list[dict], config: dict) -> str:
    sources = [monitor.normalize_string(item.get("lane_source")) for item in matched_items if item.get("lane_source")]
    if sources:
        return "explicit_item_config" if "explicit_item_config" in sources else sources[0]
    bucket = monitor.normalize_string(signal.get("bucket"))
    signal_lanes = monitor.normalize_string_list(signal.get("lanes"))
    if bucket and signal_lanes == monitor.get_lane_for_bucket(bucket, config):
        return "bucket_fallback"
    return "signal_metadata"


def signal_route_hints(signal: dict, config: dict) -> list[dict]:
    labels = route_hint_labels(monitor.normalize_string_list(signal.get("lanes")), config)
    known = {label["hint"] for label in labels}
    for hint in monitor.normalize_string_list(signal.get("route_hints")):
        if hint not in known:
            labels.append(route_hint_label(hint, ""))
            known.add(hint)
    return labels


def build_route_explanation(signal: dict, provenance: dict) -> str:
    matched = provenance.get("matched_watchlist_items") or []
    if matched:
        names = ", ".join(f"{item.get('kind')}:{item.get('value')}" for item in matched)
    else:
        names = "signal metadata"
    lanes = ", ".join(provenance.get("lanes") or []) or "none"
    labels = ", ".join(label.get("display_label") or label.get("hint") for label in provenance.get("route_hints") or []) or "none"
    lane_source = provenance.get("lane_source")
    if lane_source == "explicit_item_config":
        source_text = "explicit lanes from the matched watchlist item config"
    elif lane_source == "bucket_fallback":
        source_text = "bucket fallback from lane bucket mapping"
    else:
        source_text = "signal metadata because no exact watchlist item was inferable"
    return f"Matched {names}; bucket {provenance.get('bucket') or 'unknown'} assigned lanes {lanes} via {source_text}; route labels {labels}. Legacy x-engage/* hints are internal aliases for Socialos routes."


def attach_signal_provenance(signal: dict, config: dict) -> dict:
    matched_items = [
        public_watchlist_item_for_provenance(item, config)
        for _, _, item in iter_config_watchlist_entries(config)
        if watchlist_item_matches_signal(item, signal)
    ]
    lanes = monitor.normalize_string_list(signal.get("lanes"))
    if not lanes and matched_items:
        lanes = monitor.normalize_string_list(matched_items[0].get("lanes"))
    provenance = {
        "matched_watchlist_items": matched_items,
        "bucket": signal.get("bucket") or (matched_items[0].get("bucket") if matched_items else ""),
        "lanes": lanes,
        "lane_source": infer_lane_source(signal, matched_items, config),
        "route_hints": signal_route_hints({**signal, "lanes": lanes}, config),
    }
    enriched = {**signal, "provenance": provenance}
    enriched["route_explanation"] = build_route_explanation(enriched, provenance)
    return enriched


def read_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def load_signals(
    limit: int = 50,
    signals_path: Path | None = None,
    pending_path: Path | None = None,
    config_path: Path | None = None,
) -> dict:
    source_path = signals_path or signals_path_from_env()
    pending = pending_path or pending_path_from_env()
    config = read_config(config_path or config_path_from_env())
    raw_items = [*read_json_list(source_path), *read_json_list(pending)]
    signals_by_id: dict[str, dict] = {}
    for item in raw_items:
        signal = monitor.build_normalized_signal(item, config=config)
        signals_by_id[signal["id"]] = attach_signal_provenance(signal, config)
    signals = sorted(
        signals_by_id.values(),
        key=lambda signal: signal.get("observed_at") or signal.get("created_at") or "",
        reverse=True,
    )[: max(0, min(limit, 500))]
    return {
        "version": "v1",
        "signals_path": str(source_path),
        "pending_path": str(pending),
        "count": len(signals),
        "signals": signals,
        "contract": {
            "required_fields": [
                "id",
                "platform",
                "source",
                "source_url",
                "external_id",
                "author",
                "content_snippet",
                "matched_terms",
                "matched_accounts",
                "route_hints",
                "score",
                "why_now",
                "risk_flags",
                "observed_at",
                "created_at",
                "provenance",
                "route_explanation",
            ]
        },
    }


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_request_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    if length > MAX_BODY_BYTES:
        raise ApiError(413, "request body too large")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid JSON body", {"error": str(exc)}) from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "request body must be a JSON object")
    return payload


def render_html() -> bytes:
    watchlist = load_watchlist()
    signals = load_signals(limit=100)

    watchlist_rows = []
    for collection in ("accounts", "keywords", "mentions", "lists"):
        for item in watchlist[collection]:
            route_labels = ", ".join(
                f"{label['display_label']} (legacy {label['hint']})"
                if label.get("legacy_internal_alias")
                else label["display_label"]
                for label in item.get("route_hint_labels", [])
            ) or ", ".join(item["route_hints"])
            watchlist_rows.append(
                f"<tr><td><span class=\"pill\">{html.escape(item['kind'])}</span></td>"
                f"<td>{html.escape(item['value'])}</td>"
                f"<td>{html.escape(item['bucket'])}</td>"
                f"<td>{html.escape(', '.join(item['lanes']))}</td>"
                f"<td>{html.escape(item.get('lane_source', ''))}</td>"
                f"<td>{html.escape(route_labels)}</td>"
                f"<td>{html.escape(item['context'])}</td></tr>"
            )

    signal_cards = []
    for signal in signals["signals"]:
        provenance = signal.get("provenance", {}) if isinstance(signal.get("provenance"), dict) else {}
        matched_items = provenance.get("matched_watchlist_items") or []
        matched_text = "; ".join(
            f"{item.get('kind')}:{item.get('value')} · {item.get('lane_source')}"
            for item in matched_items
        ) or "No exact watchlist item inferred from local metadata"
        route_labels = provenance.get("route_hints") or []
        route_text = ", ".join(
            f"{label.get('display_label')} (legacy {label.get('hint')})"
            if label.get("legacy_internal_alias")
            else (label.get("display_label") or label.get("hint") or "")
            for label in route_labels
        ) or ", ".join(signal.get("route_hints", [])) or "none"
        author = signal.get("author") if isinstance(signal.get("author"), dict) else {}
        author_label = author.get("handle") or author.get("name") or "unknown author"
        date_label = signal.get("observed_at") or signal.get("created_at") or "unknown time"
        search_parts = [
            signal.get("content_snippet", ""),
            signal.get("source", ""),
            author_label,
            signal.get("bucket", ""),
            " ".join(signal.get("lanes", [])),
            " ".join(signal.get("route_hints", [])),
            " ".join(label.get("display_label", "") for label in route_labels),
            " ".join(signal.get("matched_terms", [])),
            " ".join(signal.get("matched_accounts", [])),
            matched_text,
            date_label,
            str(signal.get("score", "")),
            " ".join(signal.get("risk_flags", [])),
        ]
        search_index = html.escape(" ".join(str(part) for part in search_parts).lower(), quote=True)
        risk = ", ".join(signal.get("risk_flags", [])) or "none"
        signal_cards.append(
            f"<article class=\"signal-card\" data-search-index=\"{search_index}\">"
            f"<div class=\"signal-head\"><div><span class=\"score\">{html.escape(str(signal.get('score', '0')))}</span>"
            f"<strong>{html.escape(author_label)}</strong><span>{html.escape(date_label)}</span></div>"
            f"<span class=\"status\">{html.escape(signal.get('qualification', 'candidate'))}</span></div>"
            f"<p>{html.escape(signal.get('content_snippet', ''))}</p>"
            f"<div class=\"meta-grid\"><span>Source <b>{html.escape(signal.get('source', ''))}</b></span>"
            f"<span>Bucket <b>{html.escape(signal.get('bucket', ''))}</b></span>"
            f"<span>Lanes <b>{html.escape(', '.join(signal.get('lanes', [])) or 'none')}</b></span>"
            f"<span>Route <b>{html.escape(route_text)}</b></span>"
            f"<span>Matched <b>{html.escape(matched_text)}</b></span>"
            f"<span>Risk <b>{html.escape(risk)}</b></span></div>"
            f"<details><summary>Route provenance</summary>"
            f"<p>{html.escape(signal.get('route_explanation', 'No route explanation available.'))}</p>"
            f"<ul><li>Matched terms: {html.escape(', '.join(signal.get('matched_terms', [])) or 'none')}</li>"
            f"<li>Matched accounts: {html.escape(', '.join(signal.get('matched_accounts', [])) or 'none')}</li>"
            f"<li>Lane source: {html.escape(provenance.get('lane_source', 'unknown'))}</li>"
            f"<li>Route hints: {html.escape(route_text)}</li></ul></details></article>"
        )

    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>X-Monitor Operator Console</title>
<style>
:root{{--bg:#06070a;--panel:#0f1219;--panel2:#151a24;--line:#252b38;--text:#eef2ff;--muted:#94a3b8;--accent:#8b5cf6;--accent2:#22d3ee;--green:#34d399;}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 20% 0%,#172033 0,#06070a 42%);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}}
a{{color:#a5b4fc;text-decoration:none}} code{{background:#111827;border:1px solid var(--line);border-radius:7px;padding:.12rem .35rem;color:#dbeafe}} h1,h2,h3{{letter-spacing:-.035em;line-height:1.05}} h1{{font-size:48px;margin:0 0 16px}} h2{{font-size:28px;margin:0 0 12px}} h3{{font-size:18px;margin:20px 0 8px}} p{{color:#cbd5e1}} table{{width:100%;border-collapse:collapse;overflow:hidden;border-radius:14px}} th,td{{border-bottom:1px solid var(--line);padding:12px;text-align:left;vertical-align:top}} th{{color:#94a3b8;font-size:12px;text-transform:uppercase;letter-spacing:.08em;background:#111520}} .operator-shell{{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;max-width:1440px;margin:0 auto;padding:34px}} .content{{min-width:0}} .page{{background:linear-gradient(180deg,rgba(21,26,36,.95),rgba(10,13,20,.95));border:1px solid var(--line);border-radius:24px;padding:28px;margin-bottom:22px;box-shadow:0 24px 80px rgba(0,0,0,.35)}} .hero{{min-height:360px;display:grid;align-content:center;position:relative;overflow:hidden}} .hero:after{{content:"";position:absolute;inset:auto -10% -45% 30%;height:260px;background:radial-gradient(circle,rgba(139,92,246,.28),transparent 62%);pointer-events:none}} .eyebrow{{color:var(--accent2);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.14em}} .hero-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}} .button{{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:#111827;border-radius:999px;color:var(--text);padding:9px 13px;font-weight:650}} .button.primary{{background:linear-gradient(135deg,var(--accent),#2563eb);border-color:transparent}} .metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:24px}} .metric,.doc-card,.signal-card{{background:rgba(15,18,25,.86);border:1px solid var(--line);border-radius:18px;padding:16px}} .metric b{{display:block;font-size:24px}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}} .right-menu{{position:sticky;top:24px;align-self:start;background:rgba(15,18,25,.92);border:1px solid var(--line);border-radius:24px;padding:16px;box-shadow:0 20px 70px rgba(0,0,0,.3)}} .right-menu strong{{display:block;margin:8px 10px 12px}} .right-menu a{{display:flex;justify-content:space-between;padding:10px 12px;border-radius:12px;color:#cbd5e1}} .right-menu a:hover{{background:#171d2a;color:white}} .pill,.status{{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#111827;color:#cbd5e1;font-size:12px}} .controls{{display:grid;grid-template-columns:1fr 150px;gap:10px;margin:14px 0}} input,select{{width:100%;border:1px solid var(--line);background:#090d14;color:var(--text);border-radius:14px;padding:12px}} .signal-list{{display:grid;gap:12px}} .signal-head{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}} .signal-head div{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}} .signal-head span:not(.score):not(.status){{color:var(--muted)}} .score{{display:inline-grid;place-items:center;width:38px;height:38px;border-radius:12px;background:rgba(52,211,153,.12);color:var(--green);font-weight:800}} .meta-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}} .meta-grid span{{color:var(--muted);background:#0b0f17;border:1px solid #1f2633;border-radius:12px;padding:9px}} .meta-grid b{{display:block;color:#e5e7eb;font-weight:650;margin-top:2px}} details{{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}} summary{{cursor:pointer;color:#bfdbfe;font-weight:700}} .empty{{border:1px dashed var(--line);border-radius:18px;padding:22px;color:var(--muted)}} .boundary{{border-left:3px solid var(--accent2);padding-left:14px}} @media(max-width:980px){{.operator-shell{{grid-template-columns:1fr;padding:18px}}.right-menu{{position:static;order:-1}}.metrics,.grid,.meta-grid{{grid-template-columns:1fr}}h1{{font-size:36px}}}}
</style></head>
<body><main class="operator-shell">
<section class="content">
<section id="overview" class="page hero" data-page="overview"><span class="eyebrow">X-Monitor · passive signal intelligence</span><h1>Standalone operator console for X signals.</h1><p class="boundary">X-Monitor reads local signal/watchlist files only. It does not publish, authenticate accounts, approve, schedule, or execute — Socialos owns those workflows.</p><div class="hero-actions"><a class="button primary" href="#signals">Search signal history</a><a class="button" href="/api/signals?limit=10">Read API</a><a class="button" href="#read-command">Read command</a></div><div class="metrics"><div class="metric"><span>Accounts</span><b>{watchlist['counts']['accounts']}</b></div><div class="metric"><span>Keywords</span><b>{watchlist['counts']['keywords']}</b></div><div class="metric"><span>Mentions</span><b>{watchlist['counts']['mentions']}</b></div><div class="metric"><span>Signals loaded</span><b>{signals['count']}</b></div></div></section>
<section id="install" class="page" data-page="install"><span class="eyebrow">Install</span><h2>Local-first setup</h2><div class="grid"><div class="doc-card"><h3>Prerequisites</h3><p>Use <code>uv</code> and a local config file. API/UI reads never trigger live X collection.</p></div><div class="doc-card"><h3>Start server</h3><p><code>uv run python x_monitor_api.py --host 127.0.0.1 --port 8766</code></p></div></div></section>
<section id="quick-start" class="page" data-page="quick-start"><span class="eyebrow">Quick Start</span><h2>Operator flow</h2><ol><li>Open this console.</li><li>Review watchlist buckets, lanes, and route labels.</li><li>Search history by text, author/source, bucket, lane, route hint, matched item, date, score, or risk.</li><li>Open a signal detail to verify route provenance before handing it to Socialos/agents.</li></ol></section>
<section id="read-command" class="page" data-page="read-command"><span class="eyebrow">Read Command</span><h2>Read local signals</h2><p><code>GET /api/signals?limit=50</code> returns normalized Signal records plus additive <code>provenance</code> and <code>route_explanation</code> fields. Backward-compatible fields remain unchanged.</p><p>Public route labels use <code>socialos/*</code>. Existing <code>x-engage/*</code> values are shown as legacy internal aliases so operators do not have to guess what they mean.</p></section>
<section id="actions" class="page" data-page="actions"><span class="eyebrow">Actions</span><h2>Passive actions only</h2><div class="grid"><div class="doc-card"><h3>Allowed here</h3><p>Read health, watchlists, local signal history, and provenance. Edit watchlist items through the API.</p></div><div class="doc-card"><h3>Belongs in Socialos</h3><p>Publishing, account auth, approvals, scheduling, execution sessions, and generated post text.</p></div></div></section>
<section id="watchlist" class="page" data-page="watchlist"><span class="eyebrow">Watchlist</span><h2>Configured sources</h2><p>Persistence: <code>{html.escape(watchlist['persistence_path'])}</code></p><table><thead><tr><th>Kind</th><th>Value</th><th>Bucket</th><th>Lanes</th><th>Lane source</th><th>Route labels</th><th>Context</th></tr></thead><tbody>{''.join(watchlist_rows)}</tbody></table></section>
<section id="signals" class="page" data-page="signals"><span class="eyebrow">Signal History</span><h2>Searchable signal history</h2><p>Search covers text, source/author, bucket, lane, route hint/label, matched watchlist item/type, date/time, score, and risk flags.</p><div class="controls"><input id="signal-search" type="search" placeholder="Search local signal history…"><select id="signal-limit"><option value="10">10 signals</option><option value="25" selected>25 signals</option><option value="50">50 signals</option><option value="100">100 signals</option></select></div><div id="signal-count" class="pill">0 visible</div><div id="signal-list" class="signal-list">{''.join(signal_cards) or '<div class="empty">No local signal records found yet.</div>'}</div></section>
<section id="api" class="page" data-page="api"><span class="eyebrow">API</span><h2>Local endpoints</h2><ul><li><code>GET /api/health</code></li><li><code>GET /api/watchlist</code></li><li><code>POST /api/watchlist</code></li><li><code>PATCH /api/watchlist/&lt;id&gt;</code></li><li><code>DELETE /api/watchlist/&lt;id&gt;</code></li><li><code>GET /api/signals?limit=50</code></li></ul></section>
</section>
<aside class="right-menu"><strong>Command menu</strong><nav><a href="#overview">Overview <span>⌘1</span></a><a href="#install">Install <span>⌘2</span></a><a href="#quick-start">Quick Start <span>⌘3</span></a><a href="#read-command">Read Command <span>⌘4</span></a><a href="#actions">Actions <span>⌘5</span></a><a href="#watchlist">Watchlist</a><a href="#signals">Signal History</a><a href="#api">API</a></nav><p class="boundary">Route hints shown as <b>socialos/*</b>; <b>x-engage/*</b> remains only as legacy internal aliases.</p></aside>
</main><script>
const search=document.getElementById('signal-search'); const limit=document.getElementById('signal-limit'); const cards=[...document.querySelectorAll('.signal-card')]; const count=document.getElementById('signal-count');
function applySignalFilters(){{const q=(search?.value||'').toLowerCase().trim(); const max=parseInt(limit?.value||'25',10); let visible=0; cards.forEach(card=>{{const ok=!q || card.dataset.searchIndex.includes(q); visible += ok ? 1 : 0; card.style.display=(ok && visible<=max)?'block':'none';}}); if(count) count.textContent=`${{Math.min(visible,max)}} visible of ${{visible}} matched`;}}
search?.addEventListener('input',applySignalFilters); limit?.addEventListener('change',applySignalFilters); applySignalFilters();
</script></body></html>"""
    return body.encode("utf-8")


class XMonitorHandler(BaseHTTPRequestHandler):
    server_version = "x-monitor-api/1.0"

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        config_path = config_path_from_env()
        if self.command == "GET" and path == "/":
            body = render_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.command == "GET" and path == "/api/health":
            json_response(self, 200, {"status": "ok", "service": "x-monitor", "version": "v1"})
            return
        if self.command == "GET" and path == "/api/watchlist":
            json_response(self, 200, load_watchlist(config_path))
            return
        if self.command == "POST" and path == "/api/watchlist":
            json_response(self, 201, {"item": add_watchlist_item(config_path, read_request_json(self))})
            return
        if path.startswith("/api/watchlist/") and self.command in {"PATCH", "DELETE"}:
            watchlist_id = unquote(path.removeprefix("/api/watchlist/"))
            if self.command == "PATCH":
                json_response(self, 200, {"item": update_watchlist_item(config_path, watchlist_id, read_request_json(self))})
            else:
                json_response(self, 200, {"item": remove_watchlist_item(config_path, watchlist_id)})
            return
        if self.command == "GET" and path == "/api/signals":
            query = parse_qs(parsed.query)
            limit = monitor.normalize_int((query.get("limit") or [50])[0], 50)
            json_response(self, 200, load_signals(limit=limit))
            return
        raise ApiError(404, "route not found")

    def do_GET(self) -> None:
        self.respond()

    def do_POST(self) -> None:
        self.respond()

    def do_PATCH(self) -> None:
        self.respond()

    def do_DELETE(self) -> None:
        self.respond()

    def respond(self) -> None:
        try:
            self._handle()
        except ApiError as exc:
            json_response(self, exc.status, {"error": {"message": exc.message, "details": exc.details}})
        except Exception as exc:  # pragma: no cover - defensive server guard
            json_response(self, 500, {"error": {"message": "internal server error", "details": {"error": str(exc)}}})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="X-Monitor standalone local watchlist/signals API v1")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host; default is 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port; default is 8766")
    parser.add_argument("--config", default=str(config_path_from_env()), help="Watchlist config path to edit")
    parser.add_argument("--signals", default=str(signals_path_from_env()), help="Signal window JSON path to read")
    args = parser.parse_args()
    os.environ[CONFIG_PATH_ENV] = args.config
    os.environ[SIGNALS_PATH_ENV] = args.signals
    server = ThreadingHTTPServer((args.host, args.port), XMonitorHandler)
    print(f"X-Monitor API/UI listening on http://{args.host}:{args.port}")
    print(f"Watchlist persistence: {config_path_from_env()}")
    print("Boundary: no publishing/account auth/approval/execution controls live here.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
