import unittest
import json
from pathlib import Path
import sys
import importlib.util

# Load the monitor module
spec = importlib.util.spec_from_file_location("monitor", Path(__file__).parent / "x-monitor.py")
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
        self.assertEqual(result["_monitor_category"], "bittensor")
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
        config_path = Path(__file__).parent / "config.json"
        with open(config_path) as f:
            config = json.load(f)
        
        self.assertIn("lanes", config)
        self.assertEqual(len(config["lanes"]), 2)
        
        lane_ids = [l["id"] for l in config["lanes"]]
        self.assertIn("founder", lane_ids)
        self.assertIn("brand", lane_ids)

    def test_accounts_have_bucket_and_lanes(self):
        config_path = Path(__file__).parent / "config.json"
        with open(config_path) as f:
            config = json.load(f)
        
        for account in config["accounts"]:
            self.assertIn("bucket", account)
            self.assertIn("lanes", account)
            self.assertIsInstance(account["lanes"], list)
            self.assertTrue(len(account["lanes"]) > 0)

    def test_keywords_have_bucket_and_lanes(self):
        config_path = Path(__file__).parent / "config.json"
        with open(config_path) as f:
            config = json.load(f)
        
        for kw in config["keywords"]:
            self.assertIn("bucket", kw)
            self.assertIn("lanes", kw)
            self.assertIsInstance(kw["lanes"], list)

    def test_lanes_have_route_hints(self):
        config_path = Path(__file__).parent / "config.json"
        with open(config_path) as f:
            config = json.load(f)
        
        for lane in config["lanes"]:
            self.assertIn("route_hint", lane)


if __name__ == '__main__':
    unittest.main()
