from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import ROOT

from scripts.growth.backup import create_backup, restore_backup
from scripts.growth.permissions import (
    audit_accounts,
    audit_tree,
    enforce_tree,
    ensure_private_dir,
    ensure_private_file,
)
from scripts.growth.roster import load_roster
from scripts.growth.stop import default_stop_path
from scripts.growth.store import CheckpointStore


class BackupTests(unittest.TestCase):
    def test_backup_restore_roundtrip_while_database_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runtime"
            (source / "state").mkdir(parents=True)
            store = CheckpointStore(source / "state" / "growth1.db")
            store.open()
            self.addCleanup(store.close)
            store.begin_intent("buy:1", "buy", {"item": 1})
            store.commit_poll(
                epoch=3, events=[{
                    "id": "e1", "domain": "sense", "seq": 1, "payload": {}}],
                cursors={"sense": 1})

            dest = root / "backup"
            manifest = create_backup(source, dest)
            self.assertTrue((dest / "state" / "growth1.db").is_file())
            self.assertFalse(list(dest.rglob("*-wal")), "backup copied a WAL file")
            self.assertFalse(list(dest.rglob("*-shm")))
            self.assertEqual(manifest["databases"][0]["relative"], "state/growth1.db")
            self.assertEqual(
                (dest / "state" / "growth1.db").stat().st_mode & 0o077, 0)

            store.close()
            target = root / "restored"
            restore_backup(dest, target)
            restored = CheckpointStore(target / "state" / "growth1.db")
            restored.open()
            self.addCleanup(restored.close)
            self.assertEqual(restored.intent("buy:1")["state"], "planned")
            self.assertEqual(restored.cursor("sense")["cursor"], 1)
            self.assertEqual(restored.cursor("sense")["epoch"], "3")

    def test_backup_skips_stop_control_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runtime"
            ensure_private_dir(source / "control")
            (source / "control" / "STOP").write_text("stop", encoding="utf-8")
            dest = root / "backup"
            create_backup(source, dest)
            self.assertFalse((dest / "control" / "STOP").exists())


class PermissionTests(unittest.TestCase):
    def test_default_stop_path_matches_operator_control_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                default_stop_path(tmp), Path(tmp) / "control" / "STOP")

    def test_private_tree_passes_and_permissive_tree_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            ensure_private_dir(root)
            nested = root / "coordinator"
            ensure_private_dir(nested)
            ensure_private_file(root / "char.db")
            self.assertEqual(audit_tree(root), [])

            permissive = root / "char.db"
            os.chmod(permissive, 0o644)
            issues = audit_tree(root)
            self.assertTrue(any("char.db" in issue for issue in issues))
            enforce_tree(root)
            self.assertEqual(audit_tree(root), [])

    def test_account_permission_audit_uses_temp_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / "accounts"
            account = accounts / "doumoboy1"
            ensure_private_dir(account)
            ensure_private_dir(account / "config")
            ensure_private_dir(account / "data")
            ensure_private_file(account / "config" / "auth.json")
            ensure_private_file(account / "config" / "realm.json")
            ensure_private_file(account / "session.json")
            self.assertEqual(audit_accounts(accounts), [])

            os.chmod(account / "config" / "auth.json", 0o640)
            issues = audit_accounts(accounts)
            self.assertTrue(any("auth.json" in issue for issue in issues))

    def test_stop_directory_is_not_writable_by_growth_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control"
            ensure_private_dir(control)
            stop = control / "STOP"
            ensure_private_file(stop)
            os.chmod(control, 0o500)
            try:
                with self.assertRaises(PermissionError):
                    os.unlink(stop)
                self.assertTrue(stop.exists())
                with self.assertRaises(PermissionError):
                    (control / "STOP2").write_text("x", encoding="utf-8")
                self.assertFalse((control / "STOP2").exists())
            finally:
                os.chmod(control, 0o700)

    @unittest.skipUnless((ROOT / ".git").is_dir(), "requires a git worktree")
    def test_versioned_data_is_tracked_and_runtime_state_is_ignored(self):
        def ignored(relative: str) -> bool:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--no-index", relative],
                cwd=ROOT, capture_output=True, text=True)
            return result.returncode == 0

        for tracked in (
            "config/roster.yaml",
            "data/profiles/example.json",
            "data/routes/example.json",
            "data/quests/example.json",
            "data/professions/example.json",
            "data/dungeons/example.json",
        ):
            with self.subTest(tracked=tracked):
                self.assertFalse(ignored(tracked), f"{tracked} must be trackable")
        for ignored_path in (
            "data/growth/state/growth1.db",
            "accounts/doumoboy1/config/auth.json",
        ):
            with self.subTest(ignored=ignored_path):
                self.assertTrue(ignored(ignored_path))

    def test_versioned_roster_describes_the_five_live_characters(self):
        path = ROOT / "config" / "roster.yaml"
        self.assertTrue(path.is_file(), "config/roster.yaml must exist")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(data.get("configured"))
        self.assertEqual(len(data.get("characters") or []), 5)
        roster = load_roster(path)
        self.assertEqual(roster.faction, "horde")
        self.assertEqual(roster.start_zone, "durotar")
        self.assertEqual(
            {spec.name for spec in roster.characters},
            {"腰果战士", "腰果萨满", "腰果猎人", "腰果牧师", "腰果术士"})


if __name__ == "__main__":
    unittest.main()
