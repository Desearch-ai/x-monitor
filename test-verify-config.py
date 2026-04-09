#!/usr/bin/env python3
"""
Verify config key consistency and summarize.py / post-to-discord.cjs correctness.

Root-cause context
------------------
Before fix: summarize.py read from a stale top-level key
  (discord_channel_id) that no longer existed in config.json, so the
  Discord channel resolved to an empty string and no message was ever sent.
After fix: all scripts read config["discord"]["alerts_channel"].

This script checks:
  1. config.json has the correct nested path  (discord.alerts_channel)
  2. No stale top-level discord_channel_id key remains in config.json
  3. summarize.py source reads config["discord"]["alerts_channel"]
  4. summarize.py does NOT reference the old stale key
  5. post-to-discord.cjs reads cfg.discord?.alerts_channel (JS equivalent)

Usage:  python3 test-verify-config.py
Exit 0 = all tests pass, Exit 1 = at least one failure.
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
SUMMARIZE_FILE = SCRIPT_DIR / "summarize.py"
POST_DISCORD_FILE = SCRIPT_DIR / "post-to-discord.cjs"

passed = 0
failed = 0


def check(condition: bool, msg: str, detail: str = ""):
    global passed, failed
    if condition:
        print(f"  ✓ {msg}")
        passed += 1
    else:
        full = f"  ✗ FAIL: {msg}"
        if detail:
            full += f"\n      {detail}"
        print(full)
        failed += 1


# ── Test 1: config.json has discord.alerts_channel ────────────────────────
print("\nTest 1: config.json structure")
config = json.loads(CONFIG_FILE.read_text())
check("discord" in config, "config.json has top-level 'discord' key")
check(
    "alerts_channel" in config.get("discord", {}),
    "config.discord.alerts_channel exists",
)
channel_val = str(config.get("discord", {}).get("alerts_channel", ""))
check(len(channel_val) > 0, f"discord.alerts_channel is non-empty (value: {channel_val!r})")

# ── Test 2: no stale top-level key ────────────────────────────────────────
print("\nTest 2: no stale top-level key in config.json")
check(
    "discord_channel" not in config,
    "No stale top-level 'discord_channel' key",
    "Remove it — it will shadow the nested discord.alerts_channel read"
)
check(
    "discord_channel_id" not in config,
    "No stale top-level 'discord_channel_id' key",
)

# ── Test 3: summarize.py reads the correct path ───────────────────────────
print("\nTest 3: summarize.py reads correct config path")
summarize_src = SUMMARIZE_FILE.read_text()
check(
    'config["discord"]["alerts_channel"]' in summarize_src,
    'summarize.py reads config["discord"]["alerts_channel"]',
    'Expected: discord_channel = str(config["discord"]["alerts_channel"])'
)

# ── Test 4: summarize.py does NOT reference the old stale key ─────────────
print("\nTest 4: summarize.py does not reference stale key")
bad_keys = [
    'config["discord_channel_id"]',
    "config['discord_channel_id']",
    'config.get("discord_channel_id")',
    "config.get('discord_channel_id')",
    'config["discord_channel"]',
    "config['discord_channel']",
]
for bk in bad_keys:
    check(
        bk not in summarize_src,
        f"summarize.py does not contain stale key: {bk!r}",
    )

# ── Test 5: post-to-discord.cjs reads correct path ────────────────────────
print("\nTest 5: post-to-discord.cjs reads correct config path")
post_src = POST_DISCORD_FILE.read_text()
check(
    "cfg.discord?.alerts_channel" in post_src,
    "post-to-discord.cjs reads cfg.discord?.alerts_channel",
)

# ── Test 6: pending_alerts clear-only-on-success pattern ──────────────────
print("\nTest 6: pending_alerts preservation semantics in post-to-discord.cjs")
check(
    "pending_alerts.json preserved (not cleared)" in post_src,
    "post-to-discord.cjs logs preservation message on failure",
)
check(
    "pending_alerts.json cleared" in post_src,
    "post-to-discord.cjs logs clear message only on success",
)

# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n{'─' * 50}")
print(f"Tests: {passed + failed} | Passed: {passed} | Failed: {failed}")
if failed > 0:
    print("RESULT: FAIL")
    sys.exit(1)
else:
    print("RESULT: PASS")
    sys.exit(0)
