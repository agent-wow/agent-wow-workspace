from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import ACCOUNT_PORTS, ROOT, roster_data, write_json

from scripts.growth.identity import (
    IdentityError,
    PolicyError,
    EpochIdentityGate,
    export_account_ports,
    load_exported_account_ports,
    verify_live_identity,
    verify_roster,
)
from scripts.growth.roster import RosterError, load_roster, parse_roster


class RosterTests(unittest.TestCase):
    def test_missing_roster_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RosterError):
                load_roster(Path(tmp) / "roster.yaml")

    def test_empty_unconfigured_roster_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "roster.yaml", {
                "version": 1,
                "configured": False,
                "faction": None,
                "start_zone": None,
                "characters": [],
            })
            with self.assertRaises(RosterError):
                load_roster(path)

    def test_valid_five_character_roster(self):
        roster = parse_roster(roster_data())
        self.assertEqual(len(roster.characters), 5)
        self.assertEqual(roster.faction, "alliance")
        self.assertEqual(roster.characters[0].role, "tank")

    def test_mixed_faction_rejected(self):
        data = roster_data()
        data["characters"][2]["faction"] = "horde"
        with self.assertRaises(RosterError):
            parse_roster(data)

    def test_start_zone_mismatch_rejected(self):
        data = roster_data()
        data["characters"][3]["start_zone"] = "durotar"
        with self.assertRaises(RosterError):
            parse_roster(data)

    def test_missing_role_coverage_rejected(self):
        data = roster_data()
        for char in data["characters"]:
            if char["role"] == "healer":
                char["role"] = "dps"
        with self.assertRaises(RosterError):
            parse_roster(data)

    def test_duplicate_port_account_and_guid_rejected(self):
        for corrupt in ("port", "account", "guid"):
            data = roster_data()
            data["characters"][1][corrupt] = data["characters"][0][corrupt]
            with self.subTest(corrupt=corrupt), self.assertRaises(RosterError):
                parse_roster(data)

    def test_roster_must_have_five_characters(self):
        data = roster_data()
        data["characters"].pop()
        with self.assertRaises(RosterError):
            parse_roster(data)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.roster = parse_roster(roster_data())

    def test_policy_and_port_rejection(self):
        def allowed(account):
            return account != "doumoboy5"

        with self.assertRaises(PolicyError):
            verify_roster(
                self.roster,
                policy_account=allowed,
                policy_character=lambda account, name: True,
                account_ports=ACCOUNT_PORTS,
            )

        def char_denied(account, name):
            return name != "Growth3"

        with self.assertRaises(PolicyError):
            verify_roster(
                self.roster,
                policy_account=lambda account: True,
                policy_character=char_denied,
                account_ports=ACCOUNT_PORTS,
            )

    def test_port_must_match_the_account_config(self):
        ports = dict(ACCOUNT_PORTS)
        ports["doumoboy2"] = 9000
        with self.assertRaises(IdentityError):
            verify_roster(
                self.roster,
                policy_account=lambda account: True,
                policy_character=lambda account, name: True,
                account_ports=ports,
            )

    def test_guid_and_name_must_match_live_snapshot(self):
        spec = self.roster.characters[0]
        verify_live_identity(
            spec, {"ready": True, "guid": spec.guid, "name": spec.name})
        for snapshot in (
            {"ready": True, "guid": spec.guid + 1, "name": spec.name},
            {"ready": True, "guid": spec.guid, "name": "SomeoneElse"},
            {"ready": False, "guid": spec.guid, "name": spec.name},
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(IdentityError):
                verify_live_identity(spec, snapshot)

    def test_epoch_gate_reverifies_only_on_new_epoch(self):
        spec = self.roster.characters[0]
        calls = []

        def fetch():
            calls.append(1)
            return {"ready": True, "guid": spec.guid, "name": spec.name}

        gate = EpochIdentityGate(spec, fetch)
        gate.ensure(10)
        gate.ensure(10)
        self.assertEqual(len(calls), 1)
        gate.ensure(11)
        self.assertEqual(len(calls), 2)

    def test_failed_epoch_verification_does_not_advance(self):
        spec = self.roster.characters[0]
        snapshots = [
            {"ready": True, "guid": spec.guid, "name": spec.name},
            {"ready": True, "guid": 999, "name": spec.name},
        ]
        gate = EpochIdentityGate(spec, lambda: snapshots.pop(0))
        gate.ensure(1)
        with self.assertRaises(IdentityError):
            gate.ensure(2)
        # Epoch 2 was not accepted, so a later successful call must still verify.
        gate._fetch_snapshot = lambda: {
            "ready": True, "guid": spec.guid, "name": spec.name}
        gate.ensure(2)

    def test_account_port_read_from_agent_wow_yaml(self):
        from scripts.growth.identity import read_account_port
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "agent-wow.yaml"
            conf.write_text(
                "worldrpc:\n  host: localhost\n  port: 8123\n", encoding="utf-8")
            self.assertEqual(read_account_port(conf), 8123)

    def test_port_export_contains_only_allowed_accounts_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for account, port in (("doumoboy1", 8093), ("doumoman", 8086)):
                account_dir = root / "accounts" / account
                account_dir.mkdir(parents=True)
                (account_dir / "agent-wow.yaml").write_text(
                    f"worldrpc:\n  port: {port}\n", encoding="utf-8")
                (account_dir / "auth.json").write_text("do-not-export")
            policy = write_json(root / "agent-policy.json", {
                "accounts": {"doumoboy1": {"characters": ["*"]}}})
            destination = root / "config" / "account-ports.json"
            export_account_ports(
                root / "accounts", destination, policy_path=policy)
            self.assertEqual(
                load_exported_account_ports(destination), {"doumoboy1": 8093})
            self.assertNotIn("do-not-export", destination.read_text())


if __name__ == "__main__":
    unittest.main()
