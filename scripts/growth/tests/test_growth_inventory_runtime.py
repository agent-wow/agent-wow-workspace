from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)
from _growth_support import WARRIOR, make_view, unit

from scripts.growth.adapter import AdapterError
from scripts.growth.inventory_runtime import (
    NPC_FLAG_REPAIR, NPC_FLAG_VENDOR, InventoryRuntime,
)
from scripts.growth.items import InventoryPolicy, InventoryState, Item
from scripts.growth.profiles import PurchaseRule
from scripts.growth.store import CheckpointStore


class FakeInventoryAdapter:
    def __init__(self, state: InventoryState):
        self.state = state
        self.calls = []
        self.lose_response = False
        self.move_error = False

    def get_inventory(self):
        return self.state

    def list_inventory(self, vendor_guid):
        self.calls.append(("list", vendor_guid))
        self.state = replace(self.state, vendor_guid=vendor_guid, vendor_loaded=True)
        return self.state

    def move_to(self, x, y, z):
        if self.move_error:
            raise AdapterError("move failed")
        self.calls.append(("move", x, y, z))
        return {}

    def equip_item(self, item_guid, destination_slot):
        self.calls.append(("equip", item_guid, destination_slot))
        return self.state

    def buy_item(self, vendor_guid, item_id, slot, count):
        self.calls.append(("buy", vendor_guid, item_id, slot, count))
        vendor_item = next(row for row in self.state.vendor_items if row.item_id == item_id)
        bought = replace(
            vendor_item, guid=500, equipped=True, bag=255, slot=19, stock=-1,
            count=count * max(1, vendor_item.count))
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

    def test_reward_reservation_sells_junk_then_releases_turn_in(self):
        junk = Item(guid=42, item_id=7, sell_price=5)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(junk,), money=10, free_slots=0,
            vendor_guid=50, vendor_loaded=True, revision=1))
        runtime = self.runtime(adapter)

        selling = runtime.step(
            self.now, self.view, WARRIOR.class_mask, reserve_slots=1)
        ready = runtime.step(
            self.now + 1, self.view, WARRIOR.class_mask, reserve_slots=1)

        self.assertEqual(selling.action, "inventory.sell")
        self.assertEqual(adapter.state.free_slots, 1)
        self.assertIsNone(ready)

    def test_reward_reservation_approaches_vendor_beyond_old_search_range(self):
        vendor = unit(50, name="Duokna", faction=0, x=65)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        adapter = FakeInventoryAdapter(InventoryState(
            items=(Item(guid=42, item_id=7, sell_price=5),), free_slots=0,
            vendor_guid=50, vendor_loaded=True, revision=1))
        runtime = self.runtime(adapter)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask, reserve_slots=1)

        self.assertEqual(result.action, "move_to")
        self.assertEqual(adapter.named("move")[0][1:], (20.0, 0.0, 0.0))

    def test_reward_reservation_returns_to_turn_in_when_vendor_is_not_visible(self):
        view = make_view()
        adapter = FakeInventoryAdapter(InventoryState(free_slots=0))
        runtime = self.runtime(adapter)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask, reserve_slots=1,
            fallback_position={"x": 100, "y": 0, "z": 0})

        self.assertEqual(result.action, "move_to")
        self.assertIn("quest turn-in", result.detail)
        self.assertEqual(adapter.named("move")[0][1:], (20.0, 0.0, 0.0))

    def test_reward_reservation_blocks_turn_in_when_vendor_move_fails(self):
        vendor = unit(50, name="Duokna", faction=0, x=50)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        adapter = FakeInventoryAdapter(InventoryState(free_slots=0))
        adapter.move_error = True
        runtime = self.runtime(adapter)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask, reserve_slots=1)

        self.assertIsNotNone(result)
        self.assertIn("vendor approach failed", result.detail)

    def test_reward_vendor_failure_switches_to_its_measured_route(self):
        class TerrainAdapter(FakeInventoryAdapter):
            def move_to(self, x, y, z):
                self.calls.append(("move", x, y, z))
                if self.move_error:
                    raise AdapterError(
                        "terrain height 64.85 differs from estimated 45.89")
                return {}

        vendor = unit(50, name="Zlagk", faction=0, x=50)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        adapter = TerrainAdapter(InventoryState(free_slots=0))
        adapter.move_error = True
        runtime = self.runtime(adapter)
        route = ({"x": 0.0, "y": 10.0, "z": 0.0},
                 {"x": 50.0, "y": 10.0, "z": 0.0})
        runtime.step(
            self.now, view, WARRIOR.class_mask, reserve_slots=1,
            vendor_position={"x": 50.0, "y": 0.0, "z": 0.0},
            vendor_waypoints=route)
        adapter.move_error = False
        adapter.calls.clear()

        result = runtime.step(
            self.now + 1, view, WARRIOR.class_mask, reserve_slots=1,
            vendor_position={"x": 50.0, "y": 0.0, "z": 0.0},
            vendor_waypoints=route)

        self.assertEqual(result.action, "move_to")
        self.assertGreater(adapter.named("move")[0][2], 0,
                           adapter.named("move"))

    def test_depleted_supplies_return_along_measured_fallback_waypoints(self):
        view = make_view(position={"x": 0.0, "y": 0.0, "z": 5.0})
        adapter = FakeInventoryAdapter(InventoryState(free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(
                item_id=2512, target_count=600,
                vendor_position={"x": -200, "y": 0, "z": 5},
                vendor_waypoints=(
                    {"x": -50, "y": 0, "z": 5},
                    {"x": -100, "y": 0, "z": 5},
                    {"x": -150, "y": 0, "z": 5},
                )),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask,
            fallback_position={"x": 999, "y": 0, "z": 5})

        self.assertEqual(result.action, "move_to")
        self.assertIn("depleted supplies", result.detail)
        self.assertEqual(adapter.named("move")[0][1:], (-20.0, 0.0, 5.0))

    def test_depleted_supply_movement_uses_progressively_shorter_segments(self):
        class TerrainAdapter(FakeInventoryAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                if len(self.named("move")) < 3:
                    raise AdapterError(
                        "terrain height 64.57 differs from estimated 72.68 "
                        "by more than 8 yards")
                return {}

        view = make_view(position={"x": 0.0, "y": 0.0, "z": 72.68})
        adapter = TerrainAdapter(InventoryState(free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask,
            fallback_position={"x": -100, "y": 0, "z": 40})

        self.assertEqual(result.action, "move_to")
        self.assertEqual([call[1] for call in adapter.named("move")],
                         [-20.0, -12.0, -8.0])
        self.assertTrue(all(call[3] == 72.68 for call in adapter.named("move")))

    def test_blocked_depleted_supply_route_does_not_release_the_quest(self):
        view = make_view(position={"x": 0.0, "y": 0.0, "z": 72.68})
        adapter = FakeInventoryAdapter(InventoryState(free_slots=8))
        adapter.move_error = True
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(
            self.now, view, WARRIOR.class_mask,
            fallback_position={"x": -100, "y": 0, "z": 40})

        self.assertIsNotNone(result)
        self.assertIsNone(result.action)
        self.assertIn("depleted supply route is blocked", result.detail)

    def test_direct_supply_failure_switches_to_measured_waypoints(self):
        class TerrainAdapter(FakeInventoryAdapter):
            def move_to(self, x, y, z):
                if self.move_error:
                    raise AdapterError(
                        "terrain height 64.85 differs from estimated 45.89")
                return super().move_to(x, y, z)

        view = make_view()
        adapter = TerrainAdapter(InventoryState(free_slots=8))
        adapter.move_error = True
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(
                item_id=2512, target_count=600,
                vendor_position={"x": -20, "y": 0, "z": 0},
                vendor_waypoints=({"x": -5, "y": 10, "z": 0},)),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        runtime.step(self.now, view, WARRIOR.class_mask)
        adapter.move_error = False
        adapter.calls.clear()
        result = runtime.step(self.now + 1, view, WARRIOR.class_mask)

        self.assertEqual(result.action, "move_to")
        self.assertGreater(adapter.named("move")[0][2], 0)

    def test_configured_supply_route_is_not_diverted_by_a_food_vendor(self):
        food_vendor = unit(50, name="Zlagk", faction=0, x=-30, y=40)
        food_vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[food_vendor])
        adapter = FakeInventoryAdapter(InventoryState(free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(
                item_id=2512, target_count=600,
                vendor_position={"x": -60, "y": 0, "z": 0}),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertEqual(result.action, "move_to")
        self.assertIn("depleted supplies", result.detail)
        self.assertEqual(adapter.named("list"), [])
        self.assertLess(adapter.named("move")[0][1], 0)

    def test_full_supplies_do_not_pull_the_character_back_to_a_vendor(self):
        vendor = unit(50, name="Zlagk", faction=0, x=20)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        arrows = Item(guid=42, item_id=2512, class_id=6, count=600)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(arrows,), free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertIsNone(result)
        self.assertEqual(adapter.named("move"), [])
        self.assertEqual(adapter.named("list"), [])

    def test_one_consumed_arrow_does_not_trigger_a_vendor_detour(self):
        vendor = unit(50, name="Grimtak", faction=0, x=20)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        arrows = Item(guid=42, item_id=2512, class_id=6, count=599)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(arrows,), free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600, restock_below=100),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertIsNone(result)
        self.assertEqual(adapter.named("move"), [])
        self.assertEqual(adapter.named("list"), [])

    def test_low_supplies_use_the_configured_restock_route(self):
        view = make_view(position={"x": 0.0, "y": 0.0, "z": 5.0})
        arrows = Item(guid=42, item_id=2512, class_id=6, count=99)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(arrows,), free_slots=8))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(
                item_id=2512, target_count=600, restock_below=100,
                vendor_position={"x": -200, "y": 0, "z": 5},
                vendor_waypoints=({"x": -50, "y": 0, "z": 5},)),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertEqual(result.action, "move_to")
        self.assertLess(adapter.named("move")[0][1], 0)

    def test_abandoned_repair_does_not_pull_the_character_back_to_a_vendor(self):
        vendor = unit(50, name="Grimtak", faction=0, x=20)
        vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[vendor])
        damaged = Item(
            guid=42, item_id=7, equipped=True, equipment_slot=15,
            durability=4, max_durability=10)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), free_slots=8))
        runtime = self.runtime(adapter)
        runtime._suppressed.add("inventory.repair:50:0:42:4")

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertIsNone(result)
        self.assertEqual(adapter.named("move"), [])
        self.assertEqual(adapter.named("repair"), [])

    def test_broken_weapon_uses_the_configured_repair_route_without_a_visible_npc(self):
        view = make_view()
        broken = Item(
            guid=42, item_id=2504, equipped=True, equipment_slot=17,
            durability=0, max_durability=20)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(broken,), free_slots=8))
        rules = replace(WARRIOR.inventory, repair_position={
            "x": 100.0, "y": 0.0, "z": 0.0})
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertEqual(result.action, "move_to")
        self.assertIn("repair broken equipment", result.detail)
        self.assertEqual(adapter.named("move")[0][1:], (20.0, 0.0, 0.0))

    def test_minor_durability_loss_does_not_trigger_a_remote_repair_detour(self):
        view = make_view()
        damaged = Item(
            guid=42, item_id=2504, equipped=True, equipment_slot=17,
            durability=19, max_durability=20)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), free_slots=8))
        rules = replace(WARRIOR.inventory, repair_position={
            "x": 100.0, "y": 0.0, "z": 0.0})
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)

        self.assertIsNone(result)
        self.assertEqual(adapter.named("move"), [])

    def test_vendor_without_ammunition_is_skipped_for_a_real_stockist(self):
        food_vendor = unit(50, name="Zlagk", faction=0, x=2)
        food_vendor["npc_flags"] = NPC_FLAG_VENDOR
        arrow_vendor = unit(60, name="Duokna", faction=0, x=6)
        arrow_vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[food_vendor, arrow_vendor])
        arrows = Item(guid=0, item_id=2512, class_id=6, count=200,
                      buy_price=10, slot=3, template_known=True)
        adapter = FakeInventoryAdapter(InventoryState(
            vendor_items=(), money=27, free_slots=8,
            vendor_guid=50, vendor_loaded=True, revision=1))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        unavailable = runtime.step(self.now, view, WARRIOR.class_mask)
        self.assertIn("lacks depleted supplies", unavailable.detail)
        approaching = runtime.step(self.now + 1, view, WARRIOR.class_mask)
        self.assertEqual(approaching.action, "list_inventory")
        self.assertEqual(adapter.named("list")[-1][1], 60)

        adapter.state = replace(
            adapter.state, vendor_items=(arrows,), vendor_guid=60,
            vendor_loaded=True)
        purchased = runtime.step(self.now + 2, replace(
            view, position={"x": 6.0, "y": 0.0, "z": 0.0}),
            WARRIOR.class_mask)
        self.assertIn("inventory.buy", purchased.action)
        self.assertEqual(adapter.named("buy")[-1][1:], (60, 2512, 3, 2))
        self.assertEqual(adapter.state.count_of(2512), 400)
        self.assertEqual(adapter.state.money, 7)

    def test_lost_repair_is_confirmed_from_durability(self):
        damaged = Item(
            guid=42, item_id=7, equipped=True, equipment_slot=15,
            durability=4, max_durability=10)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), money=10, free_slots=4,
            vendor_guid=50, vendor_loaded=True, revision=1))
        adapter.lose_response = True
        runtime = self.runtime(adapter)
        self.view.nearby[0]["npc_flags"] |= NPC_FLAG_REPAIR

        runtime.step(self.now, self.view, WARRIOR.class_mask)
        result = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)
        self.assertIn("confirmed", result.detail)
        self.assertEqual(len(adapter.named("repair")), 1)
        self.assertEqual(adapter.state.items[0].durability, 10)

    def test_broken_equipment_sells_junk_to_fund_a_new_repair_attempt(self):
        broken = Item(
            guid=42, item_id=2504, equipped=True, equipment_slot=17,
            durability=0, max_durability=20)
        junk = Item(guid=43, item_id=7098, sell_price=5, count=5)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(broken, junk), money=5, free_slots=8,
            vendor_guid=50, vendor_loaded=True, revision=1))
        runtime = self.runtime(adapter)
        self.view.nearby[0]["npc_flags"] |= NPC_FLAG_REPAIR

        sold = runtime.step(self.now, self.view, WARRIOR.class_mask)
        repaired = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)

        self.assertEqual(sold.action, "inventory.sell")
        self.assertEqual(adapter.named("sell"), [("sell", 50, 43, 0)])
        self.assertIn("inventory.repair:50:30:42:0", repaired.detail)
        self.assertIn("confirmed", repaired.detail)
        self.assertEqual(adapter.named("repair"), [("repair", 50, 0)])
        self.assertEqual(adapter.state.by_guid(42).durability, 20)

    def test_normal_vendor_sells_junk_for_training_without_repairing(self):
        broken = Item(
            guid=42, item_id=2504, equipped=True, equipment_slot=17,
            durability=0, max_durability=20)
        junk = Item(guid=43, item_id=7098, sell_price=5, count=5)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(broken, junk), money=5, free_slots=8,
            vendor_guid=50, vendor_loaded=True, revision=1))
        runtime = self.runtime(adapter)

        sold = runtime.step(self.now, self.view, WARRIOR.class_mask)
        finished = runtime.step(self.now + 1, self.view, WARRIOR.class_mask)

        self.assertEqual(sold.action, "inventory.sell")
        self.assertEqual(adapter.named("sell"), [("sell", 50, 43, 0)])
        self.assertEqual(adapter.state.money, 30)
        self.assertIsNone(finished)
        self.assertEqual(adapter.named("repair"), [])

    def test_repair_skips_a_vendor_without_repair_capability(self):
        food_vendor = unit(50, name="Grimtak", faction=0, x=2)
        food_vendor["npc_flags"] = NPC_FLAG_VENDOR
        armorer = unit(60, name="Wuark", faction=0, x=6)
        armorer["npc_flags"] = NPC_FLAG_VENDOR | NPC_FLAG_REPAIR
        view = make_view(units=[food_vendor, armorer])
        damaged = Item(
            guid=42, item_id=7, equipped=True, equipment_slot=15,
            durability=0, max_durability=20)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), money=49, free_slots=8,
            vendor_guid=50, vendor_loaded=True, revision=1))
        runtime = self.runtime(adapter)

        opened = runtime.step(self.now, view, WARRIOR.class_mask)
        repaired = runtime.step(self.now + 1, view, WARRIOR.class_mask)

        self.assertEqual(opened.action, "list_inventory")
        self.assertEqual(adapter.named("list"), [("list", 60)])
        self.assertIn("confirmed", repaired.detail)
        self.assertEqual(adapter.named("repair"), [("repair", 60, 0)])
        self.assertEqual(adapter.state.items[0].durability, 20)

    def test_depleted_ammo_still_uses_its_vendor_when_equipment_is_damaged(self):
        arrow_vendor = unit(60, name="Duokna", faction=0, x=6)
        arrow_vendor["npc_flags"] = NPC_FLAG_VENDOR
        view = make_view(units=[arrow_vendor])
        arrows = Item(guid=0, item_id=2512, class_id=6, count=200,
                      buy_price=10, slot=3, template_known=True)
        damaged = Item(
            guid=42, item_id=7, equipped=True, equipment_slot=15,
            durability=0, max_durability=20)
        adapter = FakeInventoryAdapter(InventoryState(
            items=(damaged,), vendor_items=(arrows,), money=30, free_slots=8,
            vendor_guid=60, vendor_loaded=True, revision=1))
        rules = replace(WARRIOR.inventory, supplies=(
            PurchaseRule(item_id=2512, target_count=600),))
        runtime = InventoryRuntime(
            InventoryPolicy(rules), adapter, self.store, clock=lambda: self.now)

        result = runtime.step(self.now, view, WARRIOR.class_mask)
        runtime.step(self.now + 1, view, WARRIOR.class_mask)

        self.assertEqual(result.action, "inventory.buy")
        self.assertEqual(adapter.named("buy")[-1][1:], (60, 2512, 3, 3))
        self.assertEqual(adapter.named("repair"), [])

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
        self.assertIsNone(suppressed)
        self.assertEqual(len(adapter.named("equip")), 1)


if __name__ == "__main__":
    unittest.main()
