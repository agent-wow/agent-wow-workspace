from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.store import (
    CheckpointStore,
    InvalidTransition,
    StoreError,
    StoreLocked,
)


def event(event_id: str, domain: str = "sense", seq: int = 1, payload=None):
    return {
        "id": event_id,
        "domain": domain,
        "seq": seq,
        "payload": payload if payload is not None else {"id": event_id},
    }


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "char.db"
        self.store = CheckpointStore(self.path)
        self.store.open()
        self.addCleanup(self.store.close)

    def test_wal_enabled_and_private_modes(self):
        mode = self.store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_stable_intent_key_is_unique(self):
        self.store.begin_intent("buy:item:1", "buy", {"item": 1})
        self.store.begin_intent("buy:item:1", "buy", {"item": 1})
        self.assertEqual(len(self.store.intents()), 1)
        self.assertEqual(self.store.intent("buy:item:1")["state"], "planned")

    def test_stable_intent_key_rejects_a_different_payload(self):
        self.store.begin_intent("buy:item:1", "buy", {"item": 1})
        with self.assertRaises(StoreError):
            self.store.begin_intent("buy:item:1", "buy", {"item": 2})
        with self.assertRaises(StoreError):
            self.store.begin_intent("buy:item:1", "sell", {"item": 1})

    def test_sending_is_committed_before_dispatch(self):
        self.store.begin_intent("buy:item:1", "buy")
        self.store.begin_send("buy:item:1")
        # A crash happens now: the durable row must already be "sending".
        self.store.close()
        self.store = CheckpointStore(self.path)
        self.store.open()
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.intent("buy:item:1")["state"], "sending")

    def test_startup_marks_sending_and_sent_unknown(self):
        self.store.begin_intent("a", "buy")
        self.store.begin_send("a")
        self.store.begin_intent("b", "sell")
        self.store.begin_send("b")
        self.store.mark_sent("b")
        self.assertEqual(self.store.recover_inflight(), 2)
        self.assertEqual(self.store.intent("a")["state"], "unknown")
        self.assertEqual(self.store.intent("b")["state"], "unknown")

    def test_unknown_requires_readback_before_redispatch(self):
        self.store.begin_intent("a", "buy")
        self.store.begin_send("a")
        self.store.mark_unknown("a", "restart")
        with self.assertRaises(InvalidTransition):
            self.store.begin_send("a")
        self.store.resolve_unknown("a", "confirmed")
        self.assertEqual(self.store.intent("a")["state"], "confirmed")

    def test_abandoned_only_before_dispatch_or_after_readback(self):
        self.store.begin_intent("a", "buy")
        self.store.mark_abandoned("a")
        self.assertEqual(self.store.intent("a")["state"], "abandoned")
        with self.assertRaises(InvalidTransition):
            self.store.begin_send("a")

    def test_event_cursor_and_intent_commit_atomically(self):
        self.store.begin_intent("a", "buy")
        applied = self.store.commit_poll(
            epoch=7,
            events=[event("e1"), event("e2", seq=2)],
            cursors={"sense": 2},
            intents=[("a", "sending", None)],
        )
        self.assertEqual(applied, 2)
        self.assertEqual(self.store.cursor("sense")["cursor"], 2)
        self.assertEqual(self.store.cursor("sense")["epoch"], "7")
        self.assertEqual(self.store.intent("a")["state"], "sending")
        self.assertEqual(self.store.applied_event_ids(), {"e1", "e2"})

    def test_late_commit_failure_rolls_back_events_and_cursor(self):
        self.store.begin_intent("a", "buy")
        with self.assertRaises(StoreError):
            self.store.commit_poll(
                epoch=1,
                events=[event("e1")],
                cursors={"sense": 1},
                intents=[("missing-intent", "sending", None)],
            )
        self.assertEqual(self.store.event_count(), 0)
        self.assertEqual(self.store.applied_event_ids(), set())
        self.assertEqual(self.store.cursor("sense")["cursor"], 0)

    def test_replay_is_idempotent(self):
        self.store.commit_poll(
            epoch=1, events=[event("e1")], cursors={"sense": 1})
        applied = self.store.commit_poll(
            epoch=1, events=[event("e1")], cursors={"sense": 1})
        self.assertEqual(applied, 0)
        self.assertEqual(self.store.event_count(), 1)

    def test_same_event_id_in_a_new_epoch_is_not_suppressed(self):
        self.store.commit_poll(
            epoch=1, events=[event("boss-dead")], cursors={"sense": 1})
        applied = self.store.commit_poll(
            epoch=2, events=[event("boss-dead")], cursors={"sense": 1})
        self.assertEqual(applied, 1)
        self.assertEqual(self.store.event_count(), 2)

    def test_epoch_change_marks_inflight_intents_unknown(self):
        self.store.begin_intent("a", "buy")
        self.store.begin_send("a")
        self.store.begin_intent("b", "sell")
        self.store.begin_send("b")
        self.store.mark_sent("b")
        self.store.commit_poll(epoch=2)
        self.assertEqual(self.store.intent("a")["state"], "unknown")
        self.assertEqual(self.store.intent("b")["state"], "unknown")

    def test_epoch_change_resets_all_cursors(self):
        self.store.commit_poll(
            epoch=1,
            events=[event("e1", "sense", 1), event("e2", "move", 1)],
            cursors={"sense": 5, "move": 3},
        )
        self.store.commit_poll(
            epoch=2, events=[], cursors={"sense": 0}, dropped={"sense": 0})
        self.assertEqual(self.store.cursor("sense")["epoch"], "2")
        self.assertEqual(self.store.cursor("sense")["cursor"], 0)
        self.assertEqual(self.store.cursor("move")["cursor"], 0)

    def test_dropped_counters_persist(self):
        self.store.commit_poll(
            epoch=1, events=[], cursors={"sense": 4}, dropped={"sense": 3})
        self.assertEqual(self.store.cursor("sense")["dropped"], 3)

    def test_fencing_token_is_monotonic_and_stale_tokens_rejected(self):
        first = self.store.bump_fencing_token("worker-a")
        second = self.store.bump_fencing_token("worker-b")
        self.assertGreater(second, first)
        with self.assertRaises(StoreError):
            self.store.require_fencing(first)
        self.store.require_fencing(second)

    def test_duplicate_writer_is_rejected(self):
        with self.assertRaises(StoreLocked):
            CheckpointStore(self.path).open()

    def test_local_strategy_pointers_are_per_character(self):
        self.store.set_local_strategy(candidate="candidate-2", applied="v1")
        state = self.store.local_strategy()
        self.assertEqual(state["candidate"], "candidate-2")
        self.assertEqual(state["applied"], "v1")


if __name__ == "__main__":
    unittest.main()
