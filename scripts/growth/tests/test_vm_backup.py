from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401

from scripts.growth.store import CheckpointStore


class VMBackupTests(unittest.TestCase):
    def test_vm_backup_and_rebuild_restore_use_sealed_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / "host"
            vm_home = root / "vm-home"
            vm = vm_home / "aw-growth"
            for base in (host, vm):
                (base / "scripts").mkdir(parents=True)
                shutil.copytree(
                    ROOT / "scripts" / "growth", base / "scripts" / "growth")
            source = ROOT / "scripts" / "growth-backup.sh"
            script = host / "scripts" / source.name
            script.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)

            store = CheckpointStore(vm / "data" / "growth" / "state" / "growth1.db")
            store.open()
            store.begin_intent("buy:bag:1", "buy", {"item": 1})
            store.commit_poll(epoch=9, events=[{
                "id": "e1", "domain": "sense", "seq": 1, "payload": {}}],
                cursors={"sense": 1})

            fake_bin = root / "bin"
            fake_bin.mkdir()
            colima = fake_bin / "colima"
            colima.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = ssh ]; then shift 2; exec \"$@\"; fi\n"
                "exit 2\n", encoding="utf-8")
            colima.chmod(0o755)
            env = dict(os.environ)
            env.update({"PATH": f"{fake_bin}:{env['PATH']}", "HOME": str(vm_home)})
            backup = host / "backups" / "snapshot"
            result = subprocess.run(
                [str(script), "--vm", "--dest", str(backup)],
                cwd=host, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((backup / "state" / "growth1.db").is_file())
            self.assertFalse(list(backup.rglob("*-wal")))
            self.assertFalse(list(backup.rglob("*-shm")))
            store.close()

            # Simulate deleting and rebuilding aw-growth, retaining only code.
            shutil.rmtree(vm / "data" / "growth")
            result = subprocess.run(
                [str(script), "--vm", "--restore", str(backup)],
                cwd=host, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            restored = CheckpointStore(vm / "data" / "growth" / "state" / "growth1.db")
            restored.open()
            self.addCleanup(restored.close)
            self.assertEqual(restored.intent("buy:bag:1")["state"], "planned")
            self.assertEqual(restored.cursor("sense")["epoch"], "9")
            self.assertEqual(restored.cursor("sense")["cursor"], 1)


if __name__ == "__main__":
    unittest.main()
