import json
import sys
import tempfile
import unittest
from pathlib import Path
import importlib.util

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITOR_PATH = REPO_ROOT / "monitor.py"
API_PATH = REPO_ROOT / "x_monitor_api.py"

spec = importlib.util.spec_from_file_location("monitor", MONITOR_PATH)
monitor = importlib.util.module_from_spec(spec)
sys.modules["monitor"] = monitor
spec.loader.exec_module(monitor)

api_spec = importlib.util.spec_from_file_location("x_monitor_api", API_PATH)
x_monitor_api = importlib.util.module_from_spec(api_spec)
sys.modules["x_monitor_api"] = x_monitor_api
api_spec.loader.exec_module(x_monitor_api)


class NormalizedSignalContractTests(unittest.TestCase):
    def test_build_normalized_signal_contract_from_monitor_tweet(self):
        tweet = monitor.normalize_tweet(
            {
                "id": "1888",
                "text": "Desearch SN22 launch signal from builder",
                "username": "builderdao",
                "url": "https://x.com/builderdao/status/1888",
                "created_at": "2026-05-12T00:01:00+00:00",
                "like_count": 12,
                "retweet_count": 3,
            },
            source="keyword:sn22 bittensor",
            bucket="subnet",
            importance="high",
            context="Subnet 22 mentions",
            lanes=["brand"],
            route_hints=["x-engage/brand"],
        )

        signal = monitor.build_normalized_signal(
            tweet,
            observed_at="2026-05-12T00:02:00+00:00",
        )

        self.assertEqual(signal["platform"], "x")
        self.assertEqual(signal["source"], "keyword:sn22 bittensor")
        self.assertEqual(signal["source_url"], "https://x.com/builderdao/status/1888")
        self.assertEqual(signal["external_id"], "1888")
        self.assertEqual(signal["author"], {"handle": "builderdao"})
        self.assertEqual(signal["content_snippet"], "Desearch SN22 launch signal from builder")
        self.assertEqual(signal["matched_terms"], ["sn22 bittensor"])
        self.assertEqual(signal["matched_accounts"], [])
        self.assertEqual(signal["route_hints"], ["x-engage/brand"])
        self.assertGreaterEqual(signal["score"], 90)
        self.assertIn("Matched keyword:sn22 bittensor", signal["why_now"])
        self.assertEqual(signal["risk_flags"], [])
        self.assertEqual(signal["observed_at"], "2026-05-12T00:02:00+00:00")
        self.assertEqual(signal["created_at"], "2026-05-12T00:01:00+00:00")

    def test_build_normalized_signal_marks_negative_filter_risk(self):
        tweet = monitor.normalize_tweet(
            {"id": "1999", "text": "desearch scam", "created_at": "2026-05-12T00:01:00+00:00"},
            "keyword:desearch",
            "desearch",
            "high",
            "Brand mention",
            ["brand"],
            ["x-engage/brand"],
            config={"account_filters": {"desearch_ai": {"positive": ["desearch"], "negative": ["scam"]}}},
        )

        signal = monitor.build_normalized_signal(tweet, observed_at="2026-05-12T00:02:00+00:00")

        self.assertEqual(signal["matched_terms"], ["desearch"])
        self.assertIn("negative_filter_match", signal["risk_flags"])
        self.assertIn("unqualified", signal["risk_flags"])
        self.assertEqual(signal["qualification"], "unqualified")


class WatchlistPersistenceTests(unittest.TestCase):
    def make_config(self):
        return {
            "lanes": [
                {"id": "brand", "name": "Brand", "buckets": ["desearch"], "route_hint": "x-engage/brand"},
                {"id": "founder", "name": "Founder", "buckets": ["builder"], "route_hint": "x-engage/founder"},
            ],
            "accounts": [
                {"username": "desearch_ai", "bucket": "desearch", "lanes": ["brand"], "importance": "high", "context": "Brand account", "include_retweets": False}
            ],
            "keywords": [
                {"query": "sn22 bittensor", "bucket": "desearch", "lanes": ["brand"], "importance": "high", "context": "Subnet mentions"}
            ],
            "lists": [],
            "filters": {},
            "discord": {"alerts_channel": "1498287725223215185"},
        }

    def write_config(self, path: Path):
        path.write_text(json.dumps(self.make_config()), encoding="utf-8")

    def test_watchlist_add_update_remove_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            self.write_config(config_path)

            created = x_monitor_api.add_watchlist_item(
                config_path,
                {
                    "kind": "mention",
                    "value": "@openclaw",
                    "bucket": "builder",
                    "lanes": ["founder"],
                    "importance": "normal",
                    "context": "OpenClaw mentions",
                },
            )
            self.assertEqual(created["kind"], "mention")
            self.assertEqual(created["value"], "@openclaw")

            watchlist = x_monitor_api.load_watchlist(config_path)
            self.assertEqual(watchlist["counts"], {"accounts": 1, "keywords": 1, "mentions": 1, "lists": 0})
            self.assertIn("x-engage/founder", watchlist["route_hints"])
            self.assertEqual(watchlist["agent_setup"]["service"], "x-monitor")

            updated = x_monitor_api.update_watchlist_item(
                config_path,
                created["id"],
                {"bucket": "desearch", "lanes": ["brand"], "context": "Brand mention"},
            )
            self.assertEqual(updated["bucket"], "desearch")
            self.assertEqual(updated["lanes"], ["brand"])

            removed = x_monitor_api.remove_watchlist_item(config_path, created["id"])
            self.assertEqual(removed["id"], created["id"])
            self.assertEqual(x_monitor_api.load_watchlist(config_path)["counts"]["mentions"], 0)

    def test_watchlist_rejects_duplicate_and_publishing_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            self.write_config(config_path)

            with self.assertRaises(x_monitor_api.ApiError) as duplicate:
                x_monitor_api.add_watchlist_item(config_path, {"kind": "account", "value": "@desearch_ai"})
            self.assertEqual(duplicate.exception.status, 409)

            with self.assertRaises(x_monitor_api.ApiError) as unsafe:
                x_monitor_api.add_watchlist_item(config_path, {"kind": "keyword", "value": "desearch", "approval_required": True})
            self.assertEqual(unsafe.exception.status, 400)
            self.assertIn("Publishing/account-auth fields", unsafe.exception.message)


if __name__ == "__main__":
    unittest.main()
