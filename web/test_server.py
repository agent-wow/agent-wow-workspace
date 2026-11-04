"""Tests for the console's played-time cache.

The web server is a thin shell over the workspace scripts, so the only logic
worth pinning here is the cache that keeps offline character rows honest.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).with_name("server.py")
spec = importlib.util.spec_from_file_location("wow_console_server", SERVER)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class PlaytimeCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = console.PLAYTIME
        console.PLAYTIME = Path(self.tmp.name) / "playtime.json"
        self.addCleanup(self._restore)

    def _restore(self):
        console.PLAYTIME = self.original

    def test_roundtrip_is_case_insensitive(self):
        console.remember_playtime("DoumoBoy1", "Reachbot", {
            "hasData": True, "totalSeconds": 7200, "levelSeconds": 600,
            "sessionSeconds": 300, "sessionStartedUnix": 1762000000,
        })
        entry = console.cached_playtime("doumoboy1", "reachbot")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["totalSeconds"], 7200)
        self.assertEqual(entry["sessionSeconds"], 300)
        self.assertEqual(entry["account"], "DoumoBoy1")
        self.assertGreater(entry["lastSeen"], 0)

        stored = json.loads(console.PLAYTIME.read_text())
        self.assertEqual(stored["version"], 1)
        self.assertIn("doumoboy1/reachbot", stored["characters"])

    def test_missing_or_failed_sample_is_not_cached(self):
        console.remember_playtime("acct", "Char", {"hasData": False})
        console.remember_playtime("acct", "Char", {"error": "rpc failed"})
        console.remember_playtime("acct", "", {"hasData": True})
        self.assertIsNone(console.cached_playtime("acct", "Char"))
        self.assertFalse(console.PLAYTIME.exists())

    def test_corrupt_store_reads_as_empty(self):
        console.PLAYTIME.parent.mkdir(parents=True, exist_ok=True)
        console.PLAYTIME.write_text("{not json")
        self.assertIsNone(console.cached_playtime("acct", "Char"))
        # A later write must recover, not append to the broken file.
        console.remember_playtime("acct", "Char", {
            "hasData": True, "totalSeconds": 60,
        })
        self.assertEqual(console.cached_playtime("acct", "Char")["totalSeconds"], 60)


class SelfSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_playtime = console.PLAYTIME
        self.original_rpc = console.rpc
        console.PLAYTIME = Path(self.tmp.name) / "playtime.json"
        self.addCleanup(self._restore)

    def _restore(self):
        console.PLAYTIME = self.original_playtime
        console.rpc = self.original_rpc

    def test_merges_played_time_and_caches_it(self):
        replies = {
            "sense.get_self": {"unit": {"name": "Reachbot"}, "mapId": 1},
            "sense.get_played_time": {
                "hasData": True, "totalSeconds": 9000, "levelSeconds": 600,
                "sessionSeconds": 120, "sessionStartedUnix": "1762000000",
            },
        }
        console.rpc = lambda account, method, params=None: replies[method]
        payload = console.get_self("doumoboy1")
        self.assertEqual(payload["self"]["unit"]["name"], "Reachbot")
        self.assertEqual(payload["playedTime"]["totalSeconds"], 9000)
        cached = console.cached_playtime("doumoboy1", "Reachbot")
        self.assertEqual(cached["totalSeconds"], 9000)
        self.assertEqual(cached["sessionStartedUnix"], 1762000000)

    def test_old_module_degrades_to_error_field(self):
        replies = {
            "sense.get_self": {"unit": {"name": "Reachbot"}},
            "sense.get_played_time": {"error": "unknown method"},
        }
        console.rpc = lambda account, method, params=None: replies[method]
        payload = console.get_self("doumoboy1")
        self.assertIn("playedError", payload)
        self.assertNotIn("playedTime", payload)
        self.assertIsNone(console.cached_playtime("doumoboy1", "Reachbot"))

    def test_offline_self_error_is_returned_as_is(self):
        console.rpc = lambda account, method, params=None: {"error": "not in world"}
        self.assertEqual(console.get_self("doumoboy1"), {"error": "not in world"})


if __name__ == "__main__":
    unittest.main()
