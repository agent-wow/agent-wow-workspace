from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401

from scripts.growth.events import EventReconciler
from scripts.growth.store import CheckpointStore


def event(event_id: str, domain: str, seq: int):
    return {"id": event_id, "domain": domain, "seq": seq, "payload": {"n": seq}}


class FakePoller:
    """Non-destructive module poll: repeated calls return the same events."""

    def __init__(self, epoch=1, events=None, dropped=None):
        self.epoch = epoch
        self.events = events or []
        self.dropped = dropped or {}
        self.after_calls = []

    def __call__(self, epoch, after_cursor, max_events):
        self.after_calls.append(after_cursor)
        return {
            "epoch": self.epoch,
            "events": list(self.events),
            "next_cursor": max(
                [e.get("cursor", 0) for e in self.events] or [after_cursor]),
            "dropped": dict(self.dropped),
        }


class EventReconcilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CheckpointStore(Path(self.tmp.name) / "char.db")
        self.store.open()
        self.addCleanup(self.store.close)
        self.reconciler = EventReconciler(self.store)

    def test_poll_then_crash_replays_without_duplicate_application(self):
        poller = FakePoller(epoch=1, events=[
            {"cursor": 1, "id": "e1", "domain": "sense", "seq": 1,
             "payload": {"hp": 10}},
        ])
        plan1, applied1 = self.reconciler.reconcile(
            "sense", 1, poller, commit=False)
        plan2, applied2 = self.reconciler.reconcile(
            "sense", 1, poller, commit=False)
        self.assertEqual(applied1, 0)
        self.assertEqual(applied2, 0)
        self.assertEqual(
            self.store.cursor("sense")["cursor"], 0,
            "a plan that was never committed must not advance the cursor")

        applied = self.reconciler.commit(plan2)
        self.assertEqual(applied, 1)
        self.assertEqual(self.store.cursor("sense")["cursor"], 1)

        # The same non-destructive poll is returned again after the commit.
        _, applied_again = self.reconciler.reconcile("sense", 1, poller)
        self.assertEqual(applied_again, 0)
        self.assertEqual(self.store.event_count(), 1)

    def test_committed_cursor_is_used_for_the_next_poll(self):
        poller = FakePoller(epoch=1, events=[])
        self.reconciler.reconcile("sense", 1, poller)
        poller.after_calls.clear()
        self.reconciler.reconcile("sense", 1, poller)
        self.assertEqual(poller.after_calls, [0])

        self.store.commit_poll(
            epoch=1, events=[event("e1", "sense", 1)], cursors={"sense": 9})
        poller.after_calls.clear()
        self.reconciler.reconcile("sense", 1, poller)
        self.assertEqual(poller.after_calls, [9])

    def test_dropped_signal_forces_a_full_reread_once(self):
        poller = FakePoller(epoch=1, events=[], dropped={"sense": 2})
        plan, _ = self.reconciler.reconcile("sense", 1, poller)
        self.assertIn("sense", plan.full_reread)
        self.assertEqual(self.store.cursor("sense")["dropped"], 2)

        plan2, _ = self.reconciler.reconcile("sense", 1, poller)
        self.assertNotIn("sense", plan2.full_reread)

        poller.dropped = {"sense": 3}
        plan3, _ = self.reconciler.reconcile("sense", 1, poller)
        self.assertIn("sense", plan3.full_reread)

    def test_epoch_change_resets_cursor_and_requests_full_reread(self):
        self.store.commit_poll(
            epoch=1, events=[event("e1", "sense", 1)],
            cursors={"sense": 8, "move": 4})
        poller = FakePoller(epoch=2, events=[])
        plan, _ = self.reconciler.reconcile("sense", 2, poller)
        self.assertTrue(plan.epoch_changed)
        self.assertIn("sense", plan.full_reread)
        self.assertEqual(poller.after_calls, [0])
        self.assertEqual(self.store.cursor("sense")["epoch"], "2")
        self.assertEqual(self.store.cursor("move")["cursor"], 0)

    def test_event_and_intent_share_the_commit(self):
        self.store.begin_intent("buy:1", "buy")
        poller = FakePoller(epoch=1, events=[
            {"cursor": 1, "id": "e1", "domain": "sense", "seq": 1,
             "payload": {}},
        ])
        applied = self.reconciler.commit(
            self.reconciler.reconcile("sense", 1, poller, commit=False)[0],
            intents=[("buy:1", "sending", None)],
        )
        self.assertEqual(applied, 1)
        self.assertEqual(self.store.intent("buy:1")["state"], "sending")


if __name__ == "__main__":
    unittest.main()
