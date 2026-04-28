import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import importlib.util

# monitor.py lives at repo root, tests/ is a subdirectory of the repo root
REPO_ROOT = Path(__file__).resolve().parent.parent  # = repo root
MONITOR_PATH = REPO_ROOT / "monitor.py"

spec = importlib.util.spec_from_file_location("monitor", MONITOR_PATH)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorPendingAlertTests(unittest.TestCase):
    def test_merge_pending_alerts_preserves_existing_unsent_items(self):
        existing = [
            {'id': '1', 'text': 'older unsent'},
            {'id': '2', 'text': 'still unsent'}
        ]
        new_items = [
            {'id': '2', 'text': 'duplicate'},
            {'id': '3', 'text': 'new alert'}
        ]

        merged = monitor.merge_pending_alerts(existing, new_items)
        self.assertEqual([item['id'] for item in merged], ['1', '2', '3'])


class ManagedRuntimeConfigTests(unittest.TestCase):
    def make_runtime_contract(self):
        return {
            "key": "social-os:x-runtime",
            "workspace": "social-os",
            "platform": "x",
            "version": 1,
            "lanes": [
                {
                    "id": "founder",
                    "name": "Founder Lane",
                    "route_hint": "x-engage/founder",
                    "buckets": ["bittensor", "builder"],
                    "default_account_id": "personal"
                },
                {
                    "id": "brand",
                    "name": "Brand Lane",
                    "route_hint": "x-engage/brand",
                    "buckets": ["bittensor", "desearch", "subnet"],
                    "default_account_id": "brand"
                }
            ],
            "watchlists": [
                {
                    "id": "acct-const",
                    "kind": "account",
                    "value": "const",
                    "bucket": "bittensor",
                    "lanes": ["founder", "brand"],
                    "importance": "high",
                    "context": "Founder account",
                    "include_retweets": False
                },
                {
                    "id": "kw-desearch",
                    "kind": "keyword",
                    "value": "@desearch_ai",
                    "bucket": "desearch",
                    "lanes": ["brand"],
                    "importance": "high",
                    "context": "Brand mentions"
                }
            ],
            "services": [
                {
                    "id": "x-monitor",
                    "label": "X Monitor",
                    "enabled": True,
                    "settings": {
                        "discord_channel_id": "1498287725223215185",
                        "filters": {
                            "normal_importance_min_likes": 3,
                            "skip_replies": True,
                            "skip_retweets_for_normal": False
                        }
                    }
                }
            ],
            "defaults": {
                "discord_channel_id": "1498287725223215185"
            }
        }

    def write_json(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_load_config_prefers_managed_social_os_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            runtime_path = tmp_path / "social-runtime.json"
            fallback_config_path = tmp_path / "config.json"

            self.write_json(runtime_path, {"config": self.make_runtime_contract()})
            self.write_json(
                fallback_config_path,
                {
                    "lanes": [{"id": "fallback", "route_hint": "x-engage/fallback", "buckets": ["fallback"]}],
                    "accounts": [{"username": "fallback_user", "bucket": "fallback", "lanes": ["fallback"]}],
                    "keywords": [{"query": "fallback", "bucket": "fallback", "lanes": ["fallback"]}],
                },
            )

            original_config_file = monitor.CONFIG_FILE
            monitor.CONFIG_FILE = fallback_config_path
            try:
                with mock.patch.dict(os.environ, {"X_MONITOR_RUNTIME_PATH": str(runtime_path)}, clear=False):
                    config = monitor.load_config()
            finally:
                monitor.CONFIG_FILE = original_config_file

            self.assertEqual(
                config["accounts"],
                [
                    {
                        "username": "const",
                        "bucket": "bittensor",
                        "importance": "high",
                        "lanes": ["founder", "brand"],
                        "context": "Founder account",
                        "include_retweets": False,
                    }
                ],
            )
            self.assertEqual(
                config["keywords"],
                [
                    {
                        "query": "@desearch_ai",
                        "bucket": "desearch",
                        "importance": "high",
                        "lanes": ["brand"],
                        "context": "Brand mentions",
                    }
                ],
            )
            self.assertEqual(
                config["lanes"],
                [
                    {
                        "id": "founder",
                        "name": "Founder Lane",
                        "route_hint": "x-engage/founder",
                        "buckets": ["bittensor", "builder"],
                    },
                    {
                        "id": "brand",
                        "name": "Brand Lane",
                        "route_hint": "x-engage/brand",
                        "buckets": ["bittensor", "desearch", "subnet"],
                    },
                ],
            )
            self.assertEqual(config["discord"], {"alerts_channel": "1498287725223215185"})
            self.assertEqual(
                config["filters"],
                {
                    "normal_importance_min_likes": 3,
                    "skip_replies": True,
                    "skip_retweets_for_normal": False,
                },
            )

    def test_load_config_accepts_xmonitor_projection_wrapper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_path = Path(tmpdir) / "x-monitor-projection.json"
            self.write_json(
                runtime_path,
                {
                    "xMonitor": {
                        "lanes": [
                            {
                                "id": "brand",
                                "name": "Brand Lane",
                                "route_hint": "x-engage/brand",
                                "buckets": ["desearch"]
                            }
                        ],
                        "accounts": [
                            {
                                "username": "desearch_ai",
                                "bucket": "desearch",
                                "importance": "high",
                                "lanes": ["brand"],
                                "context": "Brand account",
                                "include_retweets": True
                            }
                        ],
                        "keywords": [
                            {
                                "query": "#desearch",
                                "bucket": "desearch",
                                "importance": "high",
                                "lanes": ["brand"],
                                "context": "Brand hashtag"
                            }
                        ],
                        "filters": {
                            "normal_importance_min_likes": 1,
                            "skip_replies": False,
                            "skip_retweets_for_normal": False
                        },
                        "discord": {
                            "alerts_channel": "1498287725223215185"
                        }
                    },
                    "xEngage": {
                        "x_accounts": []
                    }
                },
            )

            with mock.patch.dict(os.environ, {"X_MONITOR_RUNTIME_PATH": str(runtime_path)}, clear=False):
                config = monitor.load_config()

            self.assertEqual(config["accounts"][0]["username"], "desearch_ai")
            self.assertEqual(config["keywords"][0]["query"], "#desearch")
            self.assertEqual(monitor.resolve_route_hints(["brand"], config), ["x-engage/brand"])

    def test_managed_contract_bucket_mapping_drives_lane_and_route_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_path = Path(tmpdir) / "social-runtime.json"
            contract = self.make_runtime_contract()
            contract["watchlists"] = [
                {
                    "id": "acct-builder",
                    "kind": "account",
                    "value": "buildernews",
                    "bucket": "bittensor",
                    "importance": "high",
                    "context": "No explicit lanes — should use managed bucket mapping",
                    "include_retweets": False,
                },
                {
                    "id": "kw-subnet",
                    "kind": "keyword",
                    "value": "subnet22",
                    "bucket": "subnet",
                    "importance": "high",
                    "context": "Keyword lane should come from lane buckets",
                },
            ]
            self.write_json(runtime_path, contract)

            with mock.patch.dict(os.environ, {"X_MONITOR_RUNTIME_PATH": str(runtime_path)}, clear=False):
                config = monitor.load_config()

            account_lanes = monitor.resolve_lanes(config["accounts"][0], config["accounts"][0]["bucket"], config)
            keyword_lanes = monitor.resolve_lanes(config["keywords"][0], config["keywords"][0]["bucket"], config)

            self.assertEqual(account_lanes, ["founder", "brand"])
            self.assertEqual(monitor.resolve_route_hints(account_lanes, config), ["x-engage/founder", "x-engage/brand"])
            self.assertEqual(keyword_lanes, ["brand"])
            self.assertEqual(monitor.resolve_route_hints(keyword_lanes, config), ["x-engage/brand"])


class DualLaneMetadataTests(unittest.TestCase):
    """Tests for v2 dual-lane signal ingestion."""

    def setUp(self):
        self.config = {
            "lanes": [
                {
                    "id": "founder",
                    "name": "Founder Lane",
                    "buckets": ["bittensor", "builder", "influencer"],
                    "route_hint": "x-engage/founder"
                },
                {
                    "id": "brand",
                    "name": "Brand Lane",
                    "buckets": ["desearch", "subnet", "competitor"],
                    "route_hint": "x-engage/brand"
                }
            ],
            "accounts": [
                {"username": "const", "bucket": "bittensor", "importance": "high", "lanes": ["founder", "brand"]},
                {"username": "desearch_ai", "bucket": "desearch", "importance": "high", "lanes": ["brand"]}
            ],
            "keywords": [
                {"query": "#desearch", "bucket": "desearch", "importance": "high", "lanes": ["brand"]}
            ]
        }

    def test_get_lane_for_bucket(self):
        self.assertIn("founder", monitor.get_lane_for_bucket("bittensor", self.config))
        self.assertIn("brand", monitor.get_lane_for_bucket("desearch", self.config))

    def test_get_route_hint_for_lane(self):
        self.assertEqual(monitor.get_route_hint_for_lane("founder", self.config), "x-engage/founder")
        self.assertEqual(monitor.get_route_hint_for_lane("brand", self.config), "x-engage/brand")

    def test_normalize_tweet_adds_v2_fields(self):
        tweet = {"id": "123", "text": "test tweet", "created_at": "2026-04-11T12:00:00Z"}
        lanes = ["founder", "brand"]
        route_hints = ["x-engage/founder", "x-engage/brand"]

        result = monitor.normalize_tweet(
            tweet,
            source="account:const",
            bucket="bittensor",
            importance="high",
            context="test context",
            lanes=lanes,
            route_hints=route_hints
        )

        self.assertEqual(result["_monitor_source"], "account:const")
        self.assertEqual(result["_monitor_bucket"], "bittensor")
        self.assertEqual(result["_monitor_category"], "bittensor")  # backward compat
        self.assertEqual(result["_monitor_lanes"], lanes)
        self.assertEqual(result["_monitor_route_hints"], route_hints)

    def test_normalize_tweet_converts_twitter_date(self):
        tweet = {"id": "123", "text": "test", "created_at": "Mon Apr 11 12:00:00 +0000 2026"}

        result = monitor.normalize_tweet(
            tweet, "account:test", "test", "high", "", [], []
        )

        self.assertIn("2026", result["created_at"])

    def test_resolve_lanes_explicit(self):
        account = {"username": "test", "bucket": "other", "lanes": ["founder"]}
        lanes = monitor.resolve_lanes(account, "other", self.config)
        self.assertEqual(lanes, ["founder"])

    def test_resolve_lanes_fallback(self):
        account = {"username": "test", "bucket": "bittensor"}
        lanes = monitor.resolve_lanes(account, "bittensor", self.config)
        self.assertIn("founder", lanes)


class ConfigStructureTests(unittest.TestCase):
    """Test that config.json has v2 structure."""

    def test_lanes_exist(self):
        config_path = REPO_ROOT / "config.json"
        with open(config_path) as f:
            config = json.load(f)

        self.assertIn("lanes", config)
        self.assertEqual(len(config["lanes"]), 2)

        lane_ids = [l["id"] for l in config["lanes"]]
        self.assertIn("founder", lane_ids)
        self.assertIn("brand", lane_ids)

    def test_accounts_have_bucket_and_lanes(self):
        config_path = REPO_ROOT / "config.json"
        with open(config_path) as f:
            config = json.load(f)

        for account in config["accounts"]:
            self.assertIn("bucket", account)
            self.assertIn("lanes", account)
            self.assertIsInstance(account["lanes"], list)
            self.assertTrue(len(account["lanes"]) > 0)

    def test_keywords_have_bucket_and_lanes(self):
        config_path = REPO_ROOT / "config.json"
        with open(config_path) as f:
            config = json.load(f)

        for kw in config["keywords"]:
            self.assertIn("bucket", kw)
            self.assertIn("lanes", kw)
            self.assertIsInstance(kw["lanes"], list)

    def test_lanes_have_route_hints(self):
        config_path = REPO_ROOT / "config.json"
        with open(config_path) as f:
            config = json.load(f)

        for lane in config["lanes"]:
            self.assertIn("route_hint", lane)


if __name__ == '__main__':
    unittest.main()
