from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)
from _growth_support import WARRIOR, make_view, unit

from scripts.growth.adapter import AdapterError
from scripts.growth.inventory_runtime import InventoryRuntime, NPC_FLAG_VENDOR
from scripts.growth.items import InventoryPolicy, InventoryState, Item
from scripts.growth.store import CheckpointStore


class FakeInventoryAdapter:
    def __init__(self, state: InventoryState):
        self.state = state
        self.calls = []
        self.lose_response = False

    def get_inventory(self):
        return self.state

    def list_inventory(self, vendor_guid):
        self.calls.append(("list", vendor_guid))
        self.state = replace(self.state, vendor_guid=vendor_guid, vendor_loaded=True)
        return self.state

    def move_to(self, x, y, z):
        self.calls.append(("move", x, y, z))
        return {}

    def equip_item(self, item_guid, destination_slot):
        self.calls.append(("equip", item_guid, destination_slot))
        return self.state

    def buy_item(self, vendor_guid, item_id, slot, count):
        self.calls.append(("buy", vendor_guid, item_id, slot, count))
        vendor_item = next(row for row in self.state.vendor_items if row.item_id == item_id)
        bought = replace(
            vendor_item, guid=500, equipped=True, bag=255, slot=19, stock=-1)
        self.state = replace(
            self.state,
            items=self.state.items + (bought,),
            money=self.state.money - vendor_item.buy_price * count,
            revision=self.state.revision + 1,
        )
        if self.lose_response:
            raise AdapterError("response lost")
        return self.state

    def sell_item(self, vendor_guid, item_guid, count):
        self.calls.append(("sell", vendor_guid, item_guid, count))
        item = self.state.by_guid(item_guid)
        self.state = replace(
            self.state,
            items=tuple(row for row in self.state.items if row.guid != item_guid),
            money=self.state.money + item.sell_price * item.count,
            free_slots=self.state.free_slots + 1,
            revision=self.state.revision + 1,
        )
        if self.lose_response:
            raise AdapterError("response lost")
        return self.state

    def repair_item(self, vendor_guid, item_guid=0):
        self.calls.append(("repair", vendor_guid, item_guid))
        self.state = replace(
            self.state,
            items=tuple(replace(row, durability=row.max_durability)
                        for row in self.state.items),
            money=self.state.money - 3,
            revision=self.state.revision + 1,
        )
        if self.lose_response:
            raise AdapterError("response lost")
        return self.state

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


class InventoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CheckpointStore(Path(self.temp.name) / "growth.db").open()
        self.now = time.time()
        vendor = unit(50, name="Vendor", faction=0)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        self.view = make_view(units=[vendor])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def runtime(self, adapter):
        return InventoryRuntime(
            InventoryPolicy(WARRIOR.inventory), adapter, self.store,
            clock=lambda: self.now)

    def test_lost_bag_purchase_is_confirmed_without_second_charge(self):
        bag = Item(
            guid=0, item_id=99, class_id=1, container_slots=6,
            buy_price=50, slot=1, template_known=True)
        adapter = FakeInventoryAdapter(InventoryState(
            vendor_items=(bag,), money=100, free_slots=4,
            vendor_guid=50, vendor_loaded=True, revision=1))
        adapter.lose_response = True
        runtime = self.runtime(adapter)

        first = runtime.step(self.now, self.view, WARRIOR.class_mask)
        self.assertIn("unknown", first.detail)
        second = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)
        self.assertIn("confirmed", second.detail)
        self.assertEqual(len(adapter.named("buy")), 1)
        self.assertEqual(adapter.state.money, 50)

    def test_lost_sale_is_read_back_without_selling_twice(self):
        junk = Item(guid=42, item_id=7, sell_price=5)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(junk,), money=10, free_slots=0,
            vendor_guid=50, vendor_loaded=True, revision=1))
        adapter.lose_response = True
        runtime = self.runtime(adapter)

        runtime.step(self.now, self.view, WARRIOR.class_mask)
        result = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)
        self.assertIn("confirmed", result.detail)
        self.assertEqual(len(adapter.named("sell")), 1)
        self.assertIsNone(adapter.state.by_guid(42))

    def test_lost_repair_is_confirmed_from_durability(self):
        damaged = Item(
            guid=42, item_id=7, equipped=True, equipment_slot=15,
            durability=4, max_durability=10)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), money=10, free_slots=4,
            vendor_guid=50, vendor_loaded=True, revision=1))
        adapter.lose_response = True
        runtime = self.runtime(adapter)

        runtime.step(self.now, self.view, WARRIOR.class_mask)
        result = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)
        self.assertIn("confirmed", result.detail)
        self.assertEqual(len(adapter.named("repair")), 1)
        self.assertEqual(adapter.state.items[0].durability, 10)

    def test_equip_rejection_is_recorded_and_not_retried(self):
        weapon = Item(
            guid=42, item_id=7, equipment_slot=15, inventory_type=13,
            stats={"strength": 5})
        adapter = FakeInventoryAdapter(InventoryState(
            items=(weapon,), money=0, free_slots=4, revision=1))
        runtime = self.runtime(adapter)
        runtime.step(self.now, self.view, WARRIOR.class_mask)
        adapter.state = replace(
            adapter.state, revision=2, failure_count=1,
            last_failure={"operation": "equip", "item_guid": 42,
                          "detail": "equip_failed result 12"})

        rejected = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)
        self.assertIn("failed", rejected.detail)
        suppressed = runtime.step(self.now + 2, self.view, WARRIOR.class_mask)
        self.assertEqual(suppressed.action, "list_inventory")
        self.assertEqual(len(adapter.named("equip")), 1)


if __name__ == "__main__":
    unittest.main()
