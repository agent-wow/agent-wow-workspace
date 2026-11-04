from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import roster_data  # noqa: F401  (sets sys.path too)

from scripts.growth.coordinator import (
    CoordinatorClient,
    CoordinatorStore,
    CoordinatorUnavailable,
)
from scripts.growth.identity import EpochIdentityGate, IdentityError
from scripts.growth.roster import parse_roster
from scripts.growth.stop import StopClearDenied, StopFlag, clear_stop
from scripts.growth.store import CheckpointStore, StoreError
from scripts.growth.worker import GrowthWorker, StopRequested, WorkerLocked


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "coordinator.db"
        self.store = CoordinatorStore(self.path, role="coordinator")
        self.store.open()
        self.addCleanup(self.store.close)

    def test_worker_role_cannot_open_coordinator_store(self):
        with self.assertRaises(PermissionError):
            CoordinatorStore(self.path, role="worker")

    def test_agreement_roster_and_promoted_strategy(self):
        self.store.register_worker("doumoboy1", "Growth1", "w1", epoch=4)
        self.assertEqual(self.store.workers()[0]["character"], "Growth1")
        self.store.upsert_agreement(
            "agreement-1", scope="dungeon", participants=["Growth1", "Growth2"],
            state="proposed", nonce="n1", expires_at=100, attempt_id="a1")
        self.assertEqual(self.store.agreement("agreement-1")["state"], "proposed")
        self.store.promote_strategy("strategy-1", "general", 3, {"x": 1})
        self.assertEqual(self.store.promoted("general")[0]["version"], 3)

    def test_lease_uses_fencing_token_and_expiry(self):
        self.assertTrue(self.store.acquire_lease(
            "dungeon-1", "Growth1", ttl=10, fencing_token=5, now=100))
        self.assertFalse(self.store.acquire_lease(
            "dungeon-1", "Growth2", ttl=10, fencing_token=6, now=105))
        self.assertTrue(self.store.acquire_lease(
            "dungeon-1", "Growth2", ttl=10, fencing_token=7, now=111))
        self.assertEqual(self.store.active_lease("dungeon-1", now=112)["owner"],
                         "Growth2")

    def test_client_reports_unavailable(self):
        client = CoordinatorClient(handler=None)
        with self.assertRaises(CoordinatorUnavailable):
            client.submit({"op": "heartbeat"})


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = parse_roster(roster_data())
        self.spec = self.roster.characters[0]
        self.stop = StopFlag(self.root / "STOP")

    def make_store(self, name: str) -> CheckpointStore:
        store = CheckpointStore(self.root / f"{name}.db")
        store.open()
        self.addCleanup(store.close)
        return store

    def make_worker(self, store, *, lock_name="worker.lock", spec=None,
                    gate=None, coordinator=None, epoch=1):
        return GrowthWorker(
            spec or self.spec,
            store,
            lock_path=self.root / lock_name,
            stop_flag=self.stop,
            gate=gate,
            coordinator=coordinator,
            epoch=epoch,
        )

    def test_duplicate_worker_lock_is_isolated_per_character(self):
        first = self.make_worker(self.make_store("a"))
        first.start()
        self.addCleanup(first.stop)

        second = self.make_worker(
            self.make_store("b"), lock_name="worker.lock")
        with self.assertRaises(WorkerLocked):
            second.start()

        # A different character keeps its own lock and is unaffected.
        other = self.roster.characters[1]
        third = self.make_worker(
            self.make_store("c"), lock_name="other.lock", spec=other)
        third.start()
        third.stop()

        first.stop()
        second.start()
        second.stop()

    def test_stop_file_blocks_start_and_tick_and_growth_cannot_clear_it(self):
        stop_file = self.root / "STOP"
        stop_file.write_text("operator stop", encoding="utf-8")
        worker = self.make_worker(self.make_store("a"))
        with self.assertRaises(StopRequested):
            worker.start()
        self.assertTrue(stop_file.exists(), "growth code must never clear STOP")

        with self.assertRaises(StopClearDenied):
            clear_stop(stop_file, actor="growth")
        self.assertTrue(stop_file.exists())

        # An operator may clear it; then the worker proceeds.
        clear_stop(stop_file, actor="operator")
        worker.start()
        worker.tick()
        stop_file.write_text("stop again", encoding="utf-8")
        with self.assertRaises(StopRequested):
            worker.tick()
        self.assertTrue(stop_file.exists())
        worker.stop()

    def test_identity_gate_runs_before_the_first_write_in_an_epoch(self):
        gate = EpochIdentityGate(
            self.spec,
            lambda: {"ready": True, "guid": 999, "name": self.spec.name},
        )
        worker = self.make_worker(self.make_store("a"), gate=gate)
        worker.start()
        self.addCleanup(worker.stop)
        with self.assertRaises(IdentityError):
            worker.tick()
        self.assertIsNone(worker.store.get_meta("last_tick"))

    def test_start_recovers_sending_and_sent_to_unknown(self):
        store = self.make_store("a")
        store.begin_intent("a", "buy")
        store.begin_send("a")
        worker = self.make_worker(store)
        worker.start()
        self.addCleanup(worker.stop)
        self.assertEqual(store.intent("a")["state"], "unknown")

    def test_coordinator_failure_does_not_stop_solo_runtime(self):
        def broken(request):
            raise CoordinatorUnavailable("coordinator down")

        worker = self.make_worker(
            self.make_store("a"), coordinator=CoordinatorClient(broken))
        worker.start()
        self.addCleanup(worker.stop)
        result = worker.tick()
        self.assertIn("down", result.coordinator_error)
        self.assertIsNotNone(worker.store.get_meta("last_tick"),
                             "local checkpoint must keep advancing")

    def test_checkpoint_failure_is_isolated_between_characters(self):
        broken_store = self.make_store("broken")
        broken = self.make_worker(broken_store, lock_name="broken.lock")
        broken.start()
        self.addCleanup(broken.stop)
        broken_store.close()
        with self.assertRaises(StoreError):
            broken.tick()

        healthy = self.make_worker(
            self.make_store("healthy"), lock_name="healthy.lock",
            spec=self.roster.characters[1])
        healthy.start()
        self.addCleanup(healthy.stop)
        healthy.tick()
        self.assertIsNotNone(healthy.store.get_meta("last_tick"))

    def test_local_candidates_are_not_in_the_coordinator_store(self):
        store = self.make_store("a")
        store.set_local_strategy(candidate="candidate-1", applied="v1")
        coord = CoordinatorStore(self.root / "coord.db", role="coordinator")
        coord.open()
        self.addCleanup(coord.close)
        coord.promote_strategy("promoted-1", "general", 2, {})
        self.assertEqual(store.local_strategy()["candidate"], "candidate-1")
        self.assertEqual(coord.promoted("general")[0]["strategy_id"],
                         "promoted-1")


if __name__ == "__main__":
    unittest.main()
