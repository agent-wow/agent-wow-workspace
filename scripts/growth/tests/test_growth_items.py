"""Tests for role-based item scoring and bounded inventory decisions."""
from __future__ import annotations

import unittest
from dataclasses import replace

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.items import (
    EquipmentPolicy,
    InventoryPolicy,
    InventoryState,
    Item,
)
from scripts.growth.tests._growth_support import WARRIOR


def item(item_id: int, **overrides) -> Item:
    values = dict(
        guid=item_id + 1000,
        item_id=item_id,
        name=f"item-{item_id}",
        quality=1,
        class_id=2,
        subclass=0,
        inventory_type=17,
        equipment_slot=15,
        required_level=1,
        required_class_mask=0,
        required_skill=0,
        required_skill_rank=0,
        stats={"strength": 2},
        armor=0,
        dps=0.0,
        sell_price=5,
        buy_price=20,
        count=1,
        bag=0,
        slot=1,
        container_slots=0,
        equipped=False,
        quest_item=False,
        stock=-1,
    )
    values.update(overrides)
    return Item(**values)


class EquipmentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = EquipmentPolicy(WARRIOR.inventory)

    def test_higher_quality_lower_role_score_is_not_an_upgrade(self):
        equipped = item(1, quality=1, stats={"strength": 8}, equipped=True)
        candidate = item(2, quality=4, stats={"intellect": 100}, bag=1, slot=2)
        state = InventoryState(items=(equipped, candidate), free_slots=3)
        self.assertIsNone(self.policy.best_upgrade(state, level=10, class_mask=1))

    def test_lower_quality_higher_score_replaces_equipped_item(self):
        equipped = item(1, quality=4, stats={"strength": 1}, equipped=True)
        candidate = item(2, quality=0, stats={"strength": 9}, bag=1, slot=2)
        state = InventoryState(items=(equipped, candidate), free_slots=3)
        decision = self.policy.best_upgrade(state, level=10, class_mask=1)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.item.guid, candidate.guid)
        self.assertEqual(decision.replaced_guid, equipped.guid)
        self.assertEqual(decision.equipment_slot, 15)

    def test_level_class_and_weapon_skill_are_checked(self):
        candidate = item(
            2, required_level=20, required_class_mask=16,
            required_skill=44, required_skill_rank=5, bag=1, slot=2)
        state = InventoryState(items=(candidate,), free_slots=3)
        reason = self.policy.ineligible_reason(
            candidate, level=5, class_mask=1, skills={})
        self.assertIn("level", reason)
        reason = self.policy.ineligible_reason(
            replace(candidate, required_level=1, required_class_mask=1),
            level=5, class_mask=1, skills={})
        self.assertIn("skill", reason)

    def test_two_hander_is_compared_with_main_plus_offhand(self):
        main = item(1, stats={"strength": 4}, equipped=True)
        offhand = item(
            2, inventory_type=14, equipment_slot=16,
            stats={"strength": 3}, equipped=True, slot=2)
        candidate = item(
            3, inventory_type=17, equipment_slot=15,
            stats={"strength": 6}, bag=1, slot=3)
        state = InventoryState(items=(main, offhand, candidate), free_slots=2)
        self.assertIsNone(self.policy.best_upgrade(state, level=10, class_mask=1))
        stronger = replace(candidate, stats={"strength": 8})
        state = InventoryState(items=(main, offhand, stronger), free_slots=2)
        decision = self.policy.best_upgrade(state, level=10, class_mask=1)
        self.assertEqual(decision.item.guid, stronger.guid)
        self.assertEqual(decision.replaced_guids, (main.guid, offhand.guid))


class InventoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = InventoryPolicy(WARRIOR.inventory)

    def test_bag_purchase_precedes_supplies_and_checks_slot_stock_money(self):
        # Use explicit tiny purchase rules without relying on game item IDs.
        from scripts.growth.profiles import PurchaseRule
        policy = InventoryPolicy(replace(
            WARRIOR.inventory, supplies=(PurchaseRule(item_id=99, target_count=2),)))
        bag = item(10, class_id=1, inventory_type=0, equipment_slot=None,
                   container_slots=6, slot=1, buy_price=30)
        supply = item(99, class_id=0, inventory_type=0, equipment_slot=None,
                      slot=2, buy_price=10)
        state = InventoryState(items=(), vendor_items=(supply, bag),
                               money=100, free_slots=0)
        decision = policy.best_purchase(state)
        self.assertEqual(decision.item.item_id, 10)
        self.assertEqual(decision.vendor_slot, 1)
        self.assertEqual(decision.count, 1)
        self.assertEqual(decision.total_price, 30)
        self.assertIsNone(policy.best_purchase(replace(state, money=5)))
        self.assertEqual(
            policy.purchase_block_reason(replace(state, money=5)),
            "saving_for_bag")
        self.assertIsNone(policy.best_purchase(
            replace(state, vendor_items=(replace(bag, stock=0),))))

    def test_depleted_projectiles_precede_bag_capacity(self):
        from scripts.growth.profiles import PurchaseRule
        policy = InventoryPolicy(replace(
            WARRIOR.inventory,
            supplies=(PurchaseRule(item_id=2512, target_count=600),)))
        bag = item(10, class_id=1, inventory_type=0, equipment_slot=None,
                   container_slots=6, slot=1, buy_price=30)
        arrows = item(2512, class_id=6, inventory_type=0, equipment_slot=None,
                      slot=2, buy_price=10, count=200)
        state = InventoryState(items=(), vendor_items=(bag, arrows),
                               money=1000, free_slots=8)

        decision = policy.best_purchase(state)
        self.assertEqual(decision.item.item_id, 2512)
        self.assertEqual(decision.count, 3)
        self.assertEqual(decision.total_price, 30)
        self.assertEqual(decision.reason, "critical_projectile")
        affordable = policy.best_purchase(replace(state, money=20))
        self.assertEqual(affordable.count, 2,
                         "two 200-arrow groups are better than starving at zero")
        self.assertEqual(affordable.total_price, 20)
        self.assertIsNone(policy.best_purchase(replace(state, money=0)),
                          "cash is reserved for ammunition instead of a bag")

        owned = replace(arrows, guid=2000, count=200, bag=1, slot=3)
        decision = policy.best_purchase(replace(state, items=(owned,)))
        # Once any ammunition exists, the pre-existing bag-first rule applies.
        self.assertEqual(decision.item.item_id, 10)
        self.assertEqual(decision.reason, "bag_capacity")

    def test_owned_bag_capacity_prevents_repeated_purchase(self):
        bag = item(10, class_id=1, inventory_type=0, equipment_slot=None,
                   container_slots=6, slot=1, buy_price=30)
        owned = replace(bag, guid=100, slot=3, bag=19)
        state = InventoryState(
            items=(owned,), vendor_items=(bag,), money=100, free_slots=0)
        self.assertIsNone(self.policy.best_purchase(state))
        self.assertIsNone(self.policy.purchase_block_reason(state))
        larger = replace(bag, item_id=11, guid=1011, container_slots=8)
        decision = self.policy.best_purchase(
            replace(state, vendor_items=(bag, larger)))
        self.assertEqual(decision.item.item_id, 11)

    def test_unknown_templates_are_not_equipped_or_sold(self):
        equipped = item(1, stats={"strength": 1}, equipped=True)
        unknown = item(2, stats={"strength": 100}, bag=1, slot=2,
                       template_known=False)
        state = InventoryState(items=(equipped, unknown), free_slots=0)
        self.assertIsNone(self.policy.equipment.best_upgrade(
            state, level=10, class_mask=1))
        self.assertEqual(self.policy.items_to_sell(
            state, level=10, class_mask=1), ())

    def test_full_bag_protects_quest_upgrade_material_and_containers(self):
        equipped = item(1, stats={"strength": 1}, equipped=True)
        upgrade = item(2, stats={"strength": 8}, bag=1, slot=2)
        quest = item(3, quest_item=True, bag=1, slot=3)
        material = item(4, bag=1, slot=4)
        bag = item(5, class_id=1, container_slots=6, bag=1, slot=5)
        junk = item(6, quality=0, inventory_type=0, equipment_slot=None,
                    stats={}, bag=1, slot=6)
        rules = replace(WARRIOR.inventory, material_item_ids=(4,))
        policy = InventoryPolicy(rules)
        state = InventoryState(
            items=(equipped, upgrade, quest, material, bag, junk), free_slots=0)
        selected = policy.items_to_sell(state, level=10, class_mask=1)
        self.assertEqual([row.guid for row in selected], [junk.guid])


if __name__ == "__main__":
    unittest.main()
