import json
import os
import sys
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
sys.modules["monitor"] = monitor
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
    def setUp(self):
        empty_live_env = {
            name: ""
            for name in (
                *monitor.SUPABASE_URL_ENV_VARS,
                *monitor.SUPABASE_ANON_KEY_ENV_VARS,
            )
        }
        self.live_env_patcher = mock.patch.dict(os.environ, empty_live_env, clear=False)
        self.live_env_patcher.start()

    def tearDown(self):
        self.live_env_patcher.stop()

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

    def make_live_runtime_row(self):
        return {
            "id": "runtime-row-1",
            "label": "default",
            "watchlist_terms": ["desearch", "SN22"],
            "watchlist_accounts": ["desearch_ai", "cosmicquantum"],
            "lane_routing": {
                "research": ["desearch_ai"],
                "brand": ["cosmicquantum"],
            },
            "is_active": True,
        }

    def mocked_supabase_response(self, payload):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def test_load_config_prefers_live_supabase_runtime_before_managed_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            runtime_path = tmp_path / "managed-runtime.json"
            fallback_config_path = tmp_path / "config.json"
            self.write_json(runtime_path, {"config": self.make_runtime_contract()})
            self.write_json(
                fallback_config_path,
                {"accounts": [], "keywords": [], "lanes": []},
            )

            original_config_file = monitor.CONFIG_FILE
            monitor.CONFIG_FILE = fallback_config_path
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "SOCIAL_OS_SUPABASE_URL": "https://example.supabase.co/rest/v1",
                        "SOCIAL_OS_SUPABASE_ANON_KEY": "anon-key",
                        "X_MONITOR_RUNTIME_PATH": str(runtime_path),
                    },
                    clear=False,
                ), mock.patch(
                    "monitor.urllib.request.urlopen",
                    return_value=self.mocked_supabase_response([self.make_live_runtime_row()]),
                ) as urlopen:
                    config = monitor.load_config()
            finally:
                monitor.CONFIG_FILE = original_config_file

            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.full_url,
                "https://example.supabase.co/rest/v1/social_runtime_configs?select=%2A&is_active=eq.true&order=updated_at.desc&limit=1",
            )
            self.assertEqual(request.get_header("Apikey"), "anon-key")
            self.assertEqual(config["accounts"][0]["username"], "desearch_ai")
            self.assertEqual(config["accounts"][0]["lanes"], ["research"])
            self.assertEqual(config["accounts"][1]["username"], "cosmicquantum")
            self.assertEqual(config["accounts"][1]["lanes"], ["brand"])
            self.assertEqual(
                [item["query"] for item in config["keywords"]],
                ["desearch", "SN22"],
            )
            self.assertEqual([lane["id"] for lane in config["lanes"]], ["research", "brand"])
            self.assertEqual(config["discord"], {"alerts_channel": "1498287725223215185"})
            self.assertEqual(
                config["filters"],
                {
                    "normal_importance_min_likes": 0,
                    "skip_replies": False,
                    "skip_retweets_for_normal": False,
                },
            )

    def test_load_config_falls_back_to_managed_file_when_live_supabase_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_path = Path(tmpdir) / "social-runtime.json"
            self.write_json(runtime_path, {"config": self.make_runtime_contract()})

            with mock.patch.dict(
                os.environ,
                {
                    "SOCIAL_OS_SUPABASE_URL": "https://example.supabase.co",
                    "SOCIAL_OS_SUPABASE_ANON_KEY": "anon-key",
                    "X_MONITOR_RUNTIME_PATH": str(runtime_path),
                },
                clear=False,
            ), mock.patch(
                "monitor.urllib.request.urlopen",
                side_effect=OSError("network down"),
            ):
                config = monitor.load_config()

            self.assertEqual(config["accounts"][0]["username"], "const")
            self.assertEqual(config["keywords"][0]["query"], "@desearch_ai")

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


class SocialRuntimeTelemetryTests(unittest.TestCase):
    def sample_config(self):
        return {
            "lanes": [
                {"id": "founder", "name": "Founder", "buckets": ["bittensor"], "route_hint": "x-engage/founder"},
                {"id": "brand", "name": "Brand", "buckets": ["desearch"], "route_hint": "x-engage/brand"},
            ],
            "accounts": [
                {"username": "const", "bucket": "bittensor", "lanes": ["founder"], "importance": "high", "context": "Founder account"},
            ],
            "keywords": [
                {"query": "#desearch", "bucket": "desearch", "lanes": ["brand"], "importance": "high", "context": "Brand hashtag"},
            ],
        }

    def test_input_snapshot_has_watchlists_route_summary_and_stable_hash(self):
        config = self.sample_config()

        snapshot = monitor.build_telemetry_input_snapshot(config)
        fingerprint = monitor.config_fingerprint(snapshot)

        self.assertEqual(snapshot["accounts"], ["const"])
        self.assertEqual(snapshot["keywords"], ["#desearch"])
        self.assertEqual(snapshot["counts"], {"accounts": 1, "keywords": 1, "lanes": 2})
        self.assertEqual(snapshot["lane_summary"]["founder"], {"buckets": ["bittensor"], "route_hint": "x-engage/founder"})
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(fingerprint, monitor.config_fingerprint(json.loads(json.dumps(snapshot))))

    def test_signal_metadata_is_representative_not_full_payload(self):
        tweet = {
            "id": "123",
            "id_str": "123",
            "text": "keep this out of telemetry",
            "full_text": "also keep this out",
            "url": "https://x.com/const/status/123",
            "created_at": "2026-04-30T09:00:00+00:00",
            "_monitor_source": "account:const",
            "_monitor_bucket": "bittensor",
            "_monitor_lanes": ["founder"],
            "_monitor_route_hints": ["x-engage/founder"],
            "_monitor_importance": "high",
            "_monitor_context": "Founder account",
        }

        signals = monitor.build_representative_signal_metadata([tweet], limit=5)

        self.assertEqual(signals, [{
            "source": "account:const",
            "bucket": "bittensor",
            "lanes": ["founder"],
            "route_hints": ["x-engage/founder"],
            "importance": "high",
            "context": "Founder account",
            "tweet_id": "123",
            "url": "https://x.com/const/status/123",
            "timestamp": "2026-04-30T09:00:00+00:00",
        }])
        self.assertNotIn("text", signals[0])
        self.assertNotIn("full_text", signals[0])

    def test_runtime_event_payload_includes_input_output_and_signal_metadata(self):
        config = self.sample_config()
        started_at = "2026-04-30T09:00:00+00:00"
        finished_at = "2026-04-30T09:00:05+00:00"
        stats = {
            "accounts_checked": 1,
            "keywords_checked": 1,
            "total_fetched": 3,
            "new_count": 2,
            "deduped_count": 1,
            "emitted_count": 1,
            "queued_count": 0,
            "lanes_routed": {"founder": 1},
        }
        signal = monitor.normalize_tweet(
            {"id": "123", "url": "https://x.com/const/status/123", "created_at": started_at},
            "account:const",
            "bittensor",
            "high",
            "Founder account",
            ["founder"],
            ["x-engage/founder"],
        )

        event = monitor.build_social_runtime_event(
            lifecycle="finished",
            status="success",
            mode="dry-run",
            run_id="run-1",
            started_at=started_at,
            finished_at=finished_at,
            config=config,
            stats=stats,
            emitted_signals=[signal],
            errors=[],
            pending_alerts_count=None,
        )

        self.assertEqual(event["service"], "x-monitor")
        self.assertEqual(event["event_type"], "info")
        self.assertIn("finished", event["message"])
        self.assertEqual(event["metadata"]["input"]["accounts"], ["const"])
        self.assertEqual(event["metadata"]["output"]["accounts_checked"], 1)
        self.assertEqual(event["metadata"]["output"]["duration_seconds"], 5.0)
        self.assertEqual(event["metadata"]["output"]["representative_signals"][0]["source"], "account:const")

    def test_emit_social_runtime_events_skips_without_credentials_and_does_not_call_network(self):
        empty_env = {
            name: ""
            for name in (
                *monitor.SUPABASE_URL_ENV_VARS,
                *monitor.SUPABASE_ANON_KEY_ENV_VARS,
            )
        }
        with mock.patch.dict(os.environ, empty_env, clear=False), mock.patch(
            "monitor.urllib.request.urlopen"
        ) as urlopen, mock.patch("sys.stderr") as stderr:
            result = monitor.emit_social_runtime_events([{"service": "x-monitor", "event_type": "info", "message": "test", "metadata": {}}])

        self.assertFalse(result)
        urlopen.assert_not_called()
        self.assertIn("Social OS telemetry skipped: missing Supabase URL/key", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_emit_social_runtime_events_posts_batch_and_fails_soft(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        response.status = 201
        response.read.return_value = b""

        with mock.patch.dict(
            os.environ,
            {"SOCIAL_OS_SUPABASE_URL": "https://example.supabase.co", "SOCIAL_OS_SUPABASE_ANON_KEY": "anon-key"},
            clear=False,
        ), mock.patch("monitor.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(monitor.emit_social_runtime_events([{"service": "x-monitor", "event_type": "info", "message": "test", "metadata": {}}]))

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.supabase.co/rest/v1/social_runtime_events")
        self.assertEqual(request.get_header("Apikey"), "anon-key")
        self.assertEqual(request.get_header("Prefer"), "return=minimal")
        self.assertEqual(json.loads(request.data.decode("utf-8"))[0]["service"], "x-monitor")

        with mock.patch.dict(
            os.environ,
            {"SOCIAL_OS_SUPABASE_URL": "https://example.supabase.co", "SOCIAL_OS_SUPABASE_ANON_KEY": "anon-key"},
            clear=False,
        ), mock.patch("monitor.urllib.request.urlopen", side_effect=OSError("network down")), mock.patch("sys.stderr") as stderr:
            self.assertFalse(monitor.emit_social_runtime_events([{"service": "x-monitor", "event_type": "info", "message": "test", "metadata": {}}]))

        self.assertIn("Social OS telemetry failed", "".join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
