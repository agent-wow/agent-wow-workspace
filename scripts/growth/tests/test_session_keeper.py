from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ROOT, FakeClients, FakeStopFlag, load_session_keeper, write_json


class Recorder:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.accounts = Path(self.tmp.name) / "accounts"
        self.account = "doumoboy1"
        self.character = "Growth1"
        self.clock = 1000.0
        self.keeper_module = load_session_keeper()

    def intent(self, desired="online", character=None):
        write_json(self.accounts / self.account / "session.json", {
            "desired": desired,
            "character": character if character is not None else self.character,
            "updated": self.clock,
        })

    def make_keeper(self, *, clients=None, start=None, stop=None,
                    rpc=None, module=None, policy=None, char_policy=None,
                    log_tail=None, session_valid=None, stop_flag=None,
                    backup=None, backup_enabled=None):
        self.start = start or Recorder(True)
        self.stop = stop or Recorder(True)
        self.rpc = rpc or Recorder((True, ""))
        self.module = module or Recorder((True, ""))
        return self.keeper_module.Keeper(
            accounts_dir=self.accounts,
            policy_allows=policy or (lambda account: True),
            character_policy_allows=char_policy or (lambda account, character: True),
            list_clients_fn=clients or FakeClients({}),
            start_fn=self.start,
            stop_fn=self.stop,
            rpc_probe=self.rpc,
            module_probe=self.module,
            log_tail_fn=log_tail or (lambda account: ""),
            session_valid_fn=session_valid or (lambda account: False),
            stop_flag=stop_flag or FakeStopFlag(False),
            clock=lambda: self.clock,
            logger=lambda message: None,
            sleep_fn=lambda seconds: None,
            backup_fn=backup or Recorder(True),
            backup_enabled=backup_enabled or (lambda: True),
        )

    def test_alive_client_with_dead_rpc_restarts_only_after_consecutive_failures(self):
        self.intent()
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": f"/home/u/aw-{self.account}",
             "char": self.character},
        ]})
        keeper = self.make_keeper(
            clients=clients, rpc=Recorder((False, "rpc timeout")))

        keeper.reconcile_once()
        keeper.reconcile_once()
        self.assertEqual(self.stop.calls, [])
        self.assertEqual(self.start.calls, [])

        keeper.reconcile_once()
        self.assertEqual(len(self.stop.calls), 1)
        self.assertEqual(len(self.start.calls), 1)
        self.assertEqual(self.start.calls[0], (self.account, self.character))

    def test_module_health_failure_counts_toward_restart(self):
        self.intent()
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(
            clients=clients, rpc=Recorder((True, "")),
            module=Recorder((False, "missing module")))
        for _ in range(3):
            keeper.reconcile_once()
        self.assertEqual(len(self.stop.calls), 1)
        self.assertEqual(len(self.start.calls), 1)

    def test_healthy_client_is_not_restarted(self):
        self.intent()
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(clients=clients)
        for _ in range(5):
            keeper.reconcile_once()
        self.assertEqual(self.stop.calls, [])
        self.assertEqual(self.start.calls, [])

    def test_invalid_session_key_degrades_and_never_logs_in(self):
        self.intent()
        keeper = self.make_keeper(
            start=Recorder(False),
            log_tail=lambda account: (
                "V] saved session is invalid; please authenticate again"),
            session_valid=lambda account: False,
        )
        keeper.reconcile_once()
        self.assertEqual(len(self.start.calls), 1)
        self.assertIn(self.account, keeper.degraded)

        self.clock += 1
        keeper.reconcile_once()
        self.assertEqual(
            len(self.start.calls), 1,
            "degraded account must wait for a human instead of retrying")
        self.assertNotIn("auth login", ROOT.joinpath(
            "scripts", "session-keeper.py").read_text(encoding="utf-8").lower())
        self.assertFalse(hasattr(keeper, "login"))

    def test_stop_file_blocks_health_restart(self):
        self.intent()
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(
            clients=clients, rpc=Recorder((False, "rpc timeout")),
            stop_flag=FakeStopFlag(True))
        for _ in range(5):
            keeper.reconcile_once()
        self.assertEqual(self.stop.calls, [])
        self.assertEqual(self.start.calls, [])

    def test_offline_intent_still_stops_when_stop_is_set(self):
        self.intent(desired="offline")
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(
            clients=clients, stop_flag=FakeStopFlag(True))
        keeper.reconcile_once()
        self.assertEqual(len(self.stop.calls), 1)
        self.assertEqual(self.start.calls, [])

    def test_online_intent_starts_without_direct_web_action(self):
        self.intent()
        keeper = self.make_keeper()
        keeper.reconcile_once()
        self.assertEqual(self.start.calls, [(self.account, self.character)])

    def test_denied_character_never_stops_the_current_client(self):
        self.intent(character="NotAllowed")
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(
            clients=clients,
            char_policy=lambda account, character: False)
        keeper.reconcile_once()
        self.assertEqual(self.stop.calls, [])
        self.assertEqual(self.start.calls, [])

    def test_failed_stop_prevents_start(self):
        self.intent(character="Replacement")
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(clients=clients, stop=Recorder(False))
        keeper.reconcile_once()
        self.assertEqual(len(self.stop.calls), 1)
        self.assertEqual(self.start.calls, [])

    def test_failed_health_stop_prevents_restart(self):
        self.intent()
        clients = FakeClients({self.account: [
            {"pid": 1, "cwd": "/x", "char": self.character}]})
        keeper = self.make_keeper(
            clients=clients, stop=Recorder(False),
            rpc=Recorder((False, "rpc timeout")))
        for _ in range(3):
            keeper.reconcile_once()
        self.assertEqual(len(self.stop.calls), 1)
        self.assertEqual(self.start.calls, [])

    def test_module_probe_is_scoped_to_the_account(self):
        original_run = self.keeper_module.run
        self.addCleanup(setattr, self.keeper_module, "run", original_run)
        self.keeper_module.run = lambda *args, **kwargs: (
            0,
            "aw-doumoboy2-sense-1\n"
            "aw-doumoboy1-sense-1\naw-doumoboy1-move-1\n"
            "aw-doumoboy1-combat-1\naw-doumoboy1-loot-1\n"
            "aw-doumoboy1-quest-1\naw-doumoboy1-party-1\n",
            "")

        ok, _ = self.keeper_module.probe_modules("doumoboy1")
        self.assertTrue(ok)
        ok, _ = self.keeper_module.probe_modules("doumoboy2")
        self.assertFalse(ok)
        ok, _ = self.keeper_module.probe_modules("doumoboy3")
        self.assertFalse(ok)

    def test_module_probe_reads_compose_labels_for_hashed_names(self):
        original_run = self.keeper_module.run
        self.addCleanup(setattr, self.keeper_module, "run", original_run)
        modules = ("sense", "move", "combat", "loot", "quest", "party")
        rows = [
            f"aw-1176777713-{index}-module-1\t"
            f"/home/user/aw-doumoboy2/modules/{module}"
            for index, module in enumerate(modules)
        ]
        rows.append("aw-other-sense-1\t/home/user/aw-doumoboy1/modules/sense")
        self.keeper_module.run = lambda *args, **kwargs: (
            0, "\n".join(rows) + "\n", "")

        ok, reason = self.keeper_module.probe_modules("doumoboy2")
        self.assertTrue(ok, reason)
        ok, _ = self.keeper_module.probe_modules("doumoboy1")
        self.assertFalse(ok)

    def test_deleted_runtime_cwd_still_belongs_to_the_same_account(self):
        original_run = self.keeper_module.run
        self.addCleanup(setattr, self.keeper_module, "run", original_run)
        self.keeper_module.run = lambda *args, **kwargs: (
            0,
            "42\t/home/u/aw-doumoboy1 (deleted)\t"
            "agent-wow char play Growth1 --module-timeout 5m\n",
            "")

        clients = self.keeper_module.list_clients()

        self.assertEqual(list(clients), ["doumoboy1"])
        self.assertTrue(self.keeper_module.matching(
            clients["doumoboy1"], "Growth1"))
        self.assertNotIn("doumoboy1 (deleted)", clients)

    def test_host_backup_runs_at_most_once_per_rpo_interval(self):
        backup = Recorder(True)
        keeper = self.make_keeper(backup=backup)
        keeper.maybe_backup()
        keeper.maybe_backup()
        self.assertEqual(len(backup.calls), 1)
        self.clock += 59
        keeper.maybe_backup()
        self.assertEqual(len(backup.calls), 1)
        self.clock += 1
        keeper.maybe_backup()
        self.assertEqual(len(backup.calls), 2)

    def test_host_backup_is_disabled_until_roster_is_configured(self):
        backup = Recorder(True)
        keeper = self.make_keeper(
            backup=backup, backup_enabled=lambda: False)
        keeper.maybe_backup()
        self.assertEqual(backup.calls, [])


if __name__ == "__main__":
    unittest.main()
