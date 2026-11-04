from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from _support import ROOT


class SyncScriptTests(unittest.TestCase):
    def test_account_sync_preserves_accounts_path_and_excludes_other_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            source = ROOT / "scripts" / "sync-to-vm.sh"
            target = scripts / source.name
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
            (root / "config.yaml").write_text("worldrpc:\n  port: 8086\n")
            maps = root / "data" / "client-data" / "terrain-v20" / "maps"
            maps.mkdir(parents=True)
            (maps / "0003232.map").write_bytes(b"test-terrain")
            for account in ("doumoboy1", "doumoboy2"):
                config = root / "accounts" / account / "config"
                config.mkdir(parents=True)
                (config / "auth.json").write_text(
                    '{"username":"test-only","password":"not-a-real-secret"}')

            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture = root / "payload.tar.gz"
            colima = fake_bin / "colima"
            colima.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "if any(arg.startswith('SYNC_ACCOUNT=') for arg in sys.argv):\n"
                "    with open(os.environ['COLIMA_CAPTURE'], 'wb') as out:\n"
                "        out.write(sys.stdin.buffer.read())\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8")
            colima.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "PATH": f"{fake_bin}:{env['PATH']}",
                "ACCOUNT": "doumoboy1",
                "AGENT_WOW_VM_DIR": "aw-doumoboy1",
                "COLIMA_CAPTURE": str(capture),
            })
            result = subprocess.run(
                [str(target)], cwd=root, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(capture, "r:gz") as archive:
                names = archive.getnames()
            self.assertTrue(any(
                name.endswith("accounts/doumoboy1/config/auth.json")
                for name in names), names)
            self.assertFalse(any("doumoboy2" in name for name in names), names)
            self.assertTrue(any(
                name.endswith("data/client-data/terrain-v20/maps/0003232.map")
                for name in names), names)

    def test_growth_sync_has_no_credentials_and_exports_only_port_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            source = ROOT / "scripts" / "sync-growth-to-vm.sh"
            target = scripts / source.name
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
            shutil.copytree(ROOT / "scripts" / "growth", scripts / "growth")
            (root / "agent-policy.json").write_text(
                '{"accounts":{"doumoboy1":{"characters":["*"]}}}')
            for account, port in (("doumoboy1", 8093), ("doumoman", 8086)):
                account_dir = root / "accounts" / account
                account_dir.mkdir(parents=True)
                (account_dir / "agent-wow.yaml").write_text(
                    f"worldrpc:\n  port: {port}\n")
                (account_dir / "auth.json").write_text("do-not-export")

            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture = root / "payload.tar.gz"
            colima = fake_bin / "colima"
            colima.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "if any('tar xzf' in arg for arg in sys.argv):\n"
                "    with open(os.environ['COLIMA_CAPTURE'], 'wb') as out:\n"
                "        out.write(sys.stdin.buffer.read())\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8")
            colima.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "PATH": f"{fake_bin}:{env['PATH']}",
                "COLIMA_CAPTURE": str(capture),
            })
            result = subprocess.run(
                [str(target)], cwd=root, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(capture, "r:gz") as archive:
                names = archive.getnames()
                port_member = next(
                    name for name in names
                    if name.endswith("config/account-ports.json"))
                ports = archive.extractfile(port_member).read().decode()
            self.assertFalse(any("/accounts/" in name for name in names), names)
            self.assertIn("8093", ports)
            self.assertNotIn("8086", ports)
            self.assertNotIn("do-not-export", ports)


if __name__ == "__main__":
    unittest.main()
