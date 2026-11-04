from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import roster_data  # noqa: F401  (sets sys.path)

from _growth_support import ROUTE, WARRIOR, FakeAdapter, make_view, unit
from scripts.growth.adapter import AdapterError
from scripts.growth.machine import GrowthState, SoloGrowthMachine
from scripts.growth.roster import parse_roster
from scripts.growth.stop import StopFlag
from scripts.growth.store import CheckpointStore
from scripts.growth.worker import GrowthWorker


class RecordingGate:
    def __init__(self):
        self.epochs = []

    def ensure(self, epoch):
        self.epochs.append(epoch)
        return True


class WorkerGrowthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = parse_roster(roster_data())
        self.stop = StopFlag(self.root / "STOP")

    def make_worker(self, spec, name: str, adapter, gate=None) -> GrowthWorker:
        store = CheckpointStore(self.root / f"{name}.db")
        store.open()
        self.addCleanup(store.close)
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter, store=store)
        worker = GrowthWorker(
            spec, store, lock_path=self.root / f"{name}.lock",
            stop_flag=self.stop, growth=machine, gate=gate)
        worker.start()
        self.addCleanup(worker.stop)
        return worker

    def test_a_broken_character_does_not_stop_a_healthy_character(self):
        broken_adapter = FakeAdapter(make_view())
        broken_adapter.error = AdapterError("worldrpc down")
        broken = self.make_worker(
            self.roster.characters[0], "broken", broken_adapter)

        healthy_adapter = FakeAdapter(make_view(units=[]))
        healthy = self.make_worker(
            self.roster.characters[1], "healthy", healthy_adapter)

        broken_result = broken.tick()
        self.assertEqual(broken_result.state, GrowthState.OFFLINE.value)
        self.assertIsNotNone(
            broken.store.get_meta("last_tick"),
            "the checkpoint keeps advancing even while offline")

        healthy_result = healthy.tick()
        self.assertIsNotNone(healthy_result.state)
        self.assertIsNotNone(healthy.store.get_meta("last_tick"))

    def test_stop_is_honoured_before_the_growth_decision(self):
        adapter = FakeAdapter(make_view(units=[]))
        worker = self.make_worker(self.roster.characters[0], "stopped", adapter)
        worker.stop_flag.path.write_text("operator stop", encoding="utf-8")

        with self.assertRaises(Exception):
            worker.tick()
        self.assertEqual(adapter.calls, [])

    def test_live_epoch_is_gated_before_the_first_action(self):
        adapter = FakeAdapter(make_view(epoch="7", units=[unit(9)]))
        gate = RecordingGate()
        worker = self.make_worker(
            self.roster.characters[0], "dynamic-epoch", adapter, gate=gate)

        result = worker.tick()
        self.assertEqual(result.epoch, "7")
        self.assertEqual(gate.epochs, ["7"])
        self.assertTrue(adapter.named("attack"))

        adapter.view = make_view(epoch="8", units=[unit(10)])
        worker.tick()
        self.assertEqual(gate.epochs, ["7", "8"])

    def test_missing_live_epoch_cannot_bypass_the_identity_gate(self):
        adapter = FakeAdapter(make_view(epoch=None, units=[unit(9)]))
        gate = RecordingGate()
        worker = self.make_worker(
            self.roster.characters[0], "missing-epoch", adapter, gate=gate)

        result = worker.tick()
        self.assertEqual(result.state, GrowthState.RECONCILE.value)
        self.assertEqual(gate.epochs, [])
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
