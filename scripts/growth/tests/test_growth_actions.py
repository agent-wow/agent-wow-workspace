from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.actions import DurableActionExecutor
from scripts.growth.store import CheckpointStore


class DurableActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CheckpointStore(Path(self.temp.name) / "growth.db").open()
        self.now = time.time()
        self.executor = DurableActionExecutor(
            self.store, clock=lambda: self.now, unknown_timeout=20)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_lost_response_reconciles_before_any_retry(self):
        calls = []

        def dispatch():
            calls.append("dispatch")
            raise TimeoutError("response lost")

        first = self.executor.run(
            "buy:1", "buy", {"item_id": 10}, dispatch, lambda: None)
        self.assertEqual(first.state, "unknown")
        second = self.executor.run(
            "buy:1", "buy", {"item_id": 10},
            lambda: self.fail("ambiguous buy was retried"), lambda: True)
        self.assertEqual(second.state, "confirmed")
        self.assertEqual(calls, ["dispatch"])

    def test_definitive_no_effect_is_not_retried_with_same_intent(self):
        self.executor.run(
            "sell:1", "sell", {"guid": 22}, lambda: None, lambda: None)
        result = self.executor.run(
            "sell:1", "sell", {"guid": 22},
            lambda: self.fail("failed sale was retried"), lambda: False)
        self.assertEqual(result.state, "failed")

    def test_restarted_inflight_intent_requires_readback(self):
        self.store.begin_intent("equip:1", "equip", {"guid": 4})
        self.store.begin_send("equip:1")
        self.store.recover_inflight()
        result = self.executor.run(
            "equip:1", "equip", {"guid": 4},
            lambda: self.fail("equip retried after restart"), lambda: True)
        self.assertEqual(result.state, "confirmed")
        self.assertEqual(self.store.intent("equip:1")["attempts"], 1)

    def test_unresolvable_intent_is_abandoned_after_deadline(self):
        self.executor.run(
            "repair:1", "repair", {}, lambda: None, lambda: None)
        self.now += 21
        result = self.executor.run(
            "repair:1", "repair", {}, lambda: None, lambda: None)
        self.assertEqual(result.state, "abandoned")


if __name__ == "__main__":
    unittest.main()
