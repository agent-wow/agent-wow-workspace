from __future__ import annotations

import unittest

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.adapter import AdapterError, WorldAdapter


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None, *, timeout=10.0):
        self.calls.append((method, params or {}))
        value = self.responses.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(method, params or {})
        return value or {}


class AdapterNormalizationTests(unittest.TestCase):
    def test_ready_is_never_defaulted_when_the_server_omits_it(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {"unit": {"guid": 1, "name": "Growth1"}},
        }))
        snapshot = adapter.get_self()
        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["guid"], 1)

    def test_explicit_ready_and_camel_case_fields_are_normalized(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "ready": True,
                "mapId": 0,
                "sessionId": "epoch-7",
                "unit": {
                    "guid": "42",
                    "name": "Growth1",
                    "hasPosition": True,
                    "health": 70,
                    "maxHealth": 100,
                    "nextLevelXp": 900,
                    "playerFlags": 0x10,
                    "corpseReclaimable": True,
                    "position": {"x": 1, "y": 2, "z": 3},
                },
            },
        }))
        snapshot = adapter.get_self()
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["guid"], 42)
        self.assertEqual(snapshot["max_health"], 100)
        self.assertEqual(snapshot["next_level_xp"], 900)
        self.assertEqual(snapshot["epoch"], "epoch-7")
        self.assertTrue(snapshot["ghost"])
        self.assertTrue(snapshot["corpse_reclaimable"])
        self.assertEqual(
            {key: snapshot["position"][key] for key in ("x", "y", "z")},
            {"x": 1.0, "y": 2.0, "z": 3.0})

    def test_in_world_does_not_substitute_for_explicit_ready(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "inWorld": True,
                "map_id": 0,
                "unit": {"guid": 1, "name": "Growth1", "has_position": True},
            },
        }))
        self.assertFalse(adapter.get_self()["ready"])

    def test_resource_power_prefers_generic_then_legacy_rage(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "ready": True, "unit": {"guid": 1, "name": "Growth1",
                                        "hasPosition": True},
            },
            "move.get_position": {"position": {"x": 0, "y": 0, "z": 0}},
            "sense.get_nearby": {"units": []},
            "combat.get_state": {"selfRage": 150, "selfPower": 40},
        }))
        view = adapter.read_view()
        self.assertEqual(view.power("rage"), 150)
        self.assertEqual(view.power("mana"), 40)

    def test_legacy_power1_is_the_primary_resource_for_mana_and_energy(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "ready": True, "unit": {"guid": 1, "name": "Growth1",
                                        "hasPosition": True},
            },
            "move.get_position": {"position": {"x": 0, "y": 0, "z": 0}},
            "sense.get_nearby": {"units": []},
            "combat.get_state": {"selfRage": 150},
        }))
        view = adapter.read_view()
        self.assertEqual(view.power("mana"), 150)
        self.assertEqual(view.power("energy"), 150)

    def test_stale_combat_facts_do_not_clobber_ready_sense_snapshot(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "ready": True,
                "epoch": 3,
                "unit": {
                    "guid": 1, "name": "Growth1", "hasPosition": True,
                    "health": 80, "maxHealth": 100, "level": 5,
                    "xp": 50, "nextLevelXp": 100, "inCombat": False,
                },
            },
            "move.get_position": {"position": {"x": 0, "y": 0, "z": 0}},
            "sense.get_nearby": {"units": []},
            "combat.get_state": {
                "selfHealth": 0, "selfMaxHealth": 0, "selfLevel": 0,
                "selfXp": 0, "selfNextLevelXp": 0, "selfInCombat": True,
            },
        }))
        view = adapter.read_view()
        self.assertEqual(view.health, 80)
        self.assertEqual(view.max_health, 100)
        self.assertEqual(view.level, 5)
        self.assertEqual(view.xp, 50)
        self.assertFalse(view.in_combat)

    def test_not_ready_view_skips_other_rpc_calls(self):
        transport = FakeTransport({
            "sense.get_self": {"ready": False, "unit": {"guid": 1}},
        })
        view = WorldAdapter(transport).read_view()
        self.assertFalse(view.ready)
        self.assertEqual([call[0] for call in transport.calls], ["sense.get_self"])

    def test_inventory_protojson_is_normalized_without_inventing_templates(self):
        adapter = WorldAdapter(FakeTransport({
            "inventory.get_state": {
                "items": [{
                    "guid": "42", "itemId": 25, "equipmentSlot": -1,
                    "requiredClassMask": 1, "stats": {"strength": 3},
                    "count": 2, "buyPrice": "100", "templateKnown": True,
                }, {"guid": "43", "itemId": 26}],
                "vendorItems": [], "freeSlots": 7, "money": 99,
                "vendorGuid": "50", "vendorLoaded": True, "revision": "4",
            },
        }))
        state = adapter.get_inventory()
        self.assertEqual(state.items[0].guid, 42)
        self.assertIsNone(state.items[0].equipment_slot)
        self.assertEqual(state.items[0].stats["strength"], 3)
        self.assertEqual(state.items[0].buy_price, 100)
        self.assertFalse(state.items[1].template_known)
        self.assertEqual(state.free_slots, 7)
        self.assertTrue(state.vendor_loaded)
        self.assertEqual(state.revision, 4)

    def test_self_skill_ranks_are_normalized(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": {
                "ready": True, "unit": {"guid": 1},
                "skills": [{"id": 44, "value": 5}],
            },
        }))
        self.assertEqual(adapter.get_self()["skills"], {"44": 5})

    def test_transport_failure_becomes_adapter_error(self):
        adapter = WorldAdapter(FakeTransport({
            "sense.get_self": AdapterError("worldrpc down"),
        }))
        with self.assertRaises(AdapterError):
            adapter.get_self()


if __name__ == "__main__":
    unittest.main()
