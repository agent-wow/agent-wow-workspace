from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import roster_data  # noqa: F401  (sets sys.path)

from scripts.growth.roster import parse_roster
from scripts.growth.stop import StopFlag
from scripts.growth.supervisor import GrowthSupervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roster = parse_roster(roster_data())
        self.stop = StopFlag(Path(self.tmp.name) / "STOP")
        self.spawned = []

    def supervisor(self, keeper_alive=False, worker_alive=None):
        worker_alive = worker_alive or (lambda spec: False)

        def spawn(kind, spec=None):
            self.spawned.append((kind, spec))
            return True

        return GrowthSupervisor(
            self.roster,
            stop_flag=self.stop,
            is_keeper_alive=lambda: keeper_alive,
            is_worker_alive=worker_alive,
            spawn=spawn,
        )

    def test_plan_keeps_keeper_and_every_worker_alive(self):
        plan = self.supervisor().plan()
        self.assertFalse(plan.blocked)
        self.assertTrue(plan.start_keeper)
        self.assertEqual(len(plan.start_workers), 5)

    def test_only_missing_processes_are_started(self):
        alive = self.roster.characters[0]

        plan = self.supervisor(
            keeper_alive=True,
            worker_alive=lambda spec: spec.account == alive.account,
        ).plan()
        self.assertFalse(plan.start_keeper)
        self.assertEqual(len(plan.start_workers), 4)
        self.assertNotIn(alive, plan.start_workers)

    def test_stop_blocks_all_restarts(self):
        (Path(self.tmp.name) / "STOP").write_text("stop", encoding="utf-8")
        sup = self.supervisor()
        plan = sup.plan()
        self.assertTrue(plan.blocked)
        self.assertFalse(plan.start_keeper)
        self.assertEqual(plan.start_workers, [])
        sup.run_once()
        self.assertEqual(self.spawned, [])

    def test_supervisor_never_starts_sessions_directly(self):
        sup = self.supervisor()
        sup.run_once()
        kinds = {kind for kind, _ in self.spawned}
        self.assertEqual(kinds, {"keeper", "worker"})
        self.assertNotIn("session", kinds)


if __name__ == "__main__":
    unittest.main()
