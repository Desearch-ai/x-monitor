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
    lanes = monitor.resolve_lanes(entry, bucket, config)
    return {
        "id": item_id(kind, value),
        "kind": kind,
        "value": value,
        "bucket": bucket,
        "lanes": lanes,
        "route_hints": route_hints_for_lanes(lanes, config),
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


def load_signals(limit: int = 50, signals_path: Path | None = None, pending_path: Path | None = None) -> dict:
    source_path = signals_path or signals_path_from_env()
    pending = pending_path or pending_path_from_env()
    raw_items = [*read_json_list(source_path), *read_json_list(pending)]
    signals_by_id: dict[str, dict] = {}
    for item in raw_items:
        signal = monitor.build_normalized_signal(item)
        signals_by_id[signal["id"]] = signal
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
    signals = load_signals(limit=25)
    rows = []
    for collection in ("accounts", "keywords", "mentions", "lists"):
        for item in watchlist[collection]:
            rows.append(
                f"<tr><td>{html.escape(item['kind'])}</td><td>{html.escape(item['value'])}</td>"
                f"<td>{html.escape(item['bucket'])}</td><td>{html.escape(', '.join(item['lanes']))}</td>"
                f"<td>{html.escape(', '.join(item['route_hints']))}</td><td>{html.escape(item['context'])}</td></tr>"
            )
    signal_cards = []
    for signal in signals["signals"]:
        signal_cards.append(
            f"<li><strong>{html.escape(str(signal['score']))}</strong> "
            f"{html.escape(signal['content_snippet'])}<br>"
            f"<small>{html.escape(signal['source'])} · {html.escape(signal['why_now'])} · risk: {html.escape(', '.join(signal['risk_flags']) or 'none')}</small></li>"
        )
    body = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>X-Monitor v1</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.5rem;text-align:left}}code{{background:#f4f4f4;padding:.1rem .25rem}}.note{{background:#fff8d7;padding:1rem;border:1px solid #e8d27a}}</style></head>
<body>
<h1>X-Monitor standalone watchlist + signals</h1>
<p class='note'>Boundary: passive signal intelligence only. Socialos owns publishing, account auth, approvals, scheduling, and execution.</p>
<p>Persistence: <code>{html.escape(watchlist['persistence_path'])}</code>. API reads do not trigger live X collection.</p>
<h2>Watchlist</h2>
<table><thead><tr><th>Kind</th><th>Value</th><th>Bucket</th><th>Lanes</th><th>Route hints</th><th>Context</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Latest signals</h2>
<ul>{''.join(signal_cards) or '<li>No local signal records found yet.</li>'}</ul>
<h2>API</h2>
<ul><li><code>GET /api/watchlist</code></li><li><code>POST /api/watchlist</code></li><li><code>PATCH /api/watchlist/&lt;id&gt;</code></li><li><code>DELETE /api/watchlist/&lt;id&gt;</code></li><li><code>GET /api/signals?limit=50</code></li></ul>
</body></html>"""
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
