"""Role-based item decisions; live item facts remain in the inventory module.

Quality is deliberately absent from equipment score. An upgrade is a legal item
whose class-weighted stats improve the relevant equipment slot(s), including the
main-hand plus off-hand comparison for a two-handed weapon.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .profiles import InventoryRules, PurchaseRule
from .social import TradeState


@dataclass(frozen=True)
class Item:
    guid: int
    item_id: int
    name: str = ""
    quality: int = 0
    class_id: int = 0
    subclass: int = 0
    inventory_type: int = 0
    equipment_slot: int | None = None
    required_level: int = 0
    required_class_mask: int = 0
    required_skill: int = 0
    required_skill_rank: int = 0
    stats: dict[str, int] = field(default_factory=dict)
    armor: int = 0
    dps: float = 0.0
    sell_price: int = 0
    buy_price: int = 0
    count: int = 1
    bag: int = 0
    slot: int = 0
    container_slots: int = 0
    equipped: bool = False
    quest_item: bool = False
    stock: int = -1
    durability: int = 0
    max_durability: int = 0
    template_known: bool = True
    bonding: int = 0


@dataclass(frozen=True)
class InventoryState:
    items: tuple[Item, ...] = ()
    vendor_items: tuple[Item, ...] = ()
    buyback: tuple[Item, ...] = ()
    money: int = 0
    free_slots: int = 0
    vendor_guid: int | None = None
    vendor_loaded: bool = False
    revision: int = 0
    failure_count: int = 0
    last_failure: dict = field(default_factory=dict)
    item_templates: tuple[Item, ...] = ()
    trade: TradeState = field(default_factory=TradeState)

    def count_of(self, item_id: int) -> int:
        return sum(row.count for row in self.items if row.item_id == item_id)

    def by_guid(self, guid: int) -> Item | None:
        return next((row for row in self.items if row.guid == guid), None)


@dataclass(frozen=True)
class EquipmentDecision:
    item: Item
    equipment_slot: int
    score: float
    previous_score: float
    replaced_guids: tuple[int, ...] = ()

    @property
    def replaced_guid(self) -> int | None:
        return self.replaced_guids[0] if self.replaced_guids else None


@dataclass(frozen=True)
class PurchaseDecision:
    item: Item
    vendor_slot: int
    count: int
    total_price: int
    reason: str


class EquipmentPolicy:
    def __init__(self, rules: InventoryRules):
        self.rules = rules

    def score(self, item: Item | None) -> float:
        if item is None:
            return 0.0
        value = float(item.armor) * self.rules.armor_weight
        value += float(item.dps) * self.rules.weapon_dps_weight
        for name, amount in item.stats.items():
            value += (self.rules.stat_weights.get(str(name).lower(), 0.0)
                      * float(amount))
        return value

    def ineligible_reason(
        self, item: Item, *, level: int, class_mask: int,
        skills: dict | None = None,
    ) -> str | None:
        if item.required_level > level:
            return f"requires level {item.required_level}"
        if item.required_class_mask and not item.required_class_mask & class_mask:
            return "class cannot use item"
        if item.required_skill or item.required_skill_rank:
            ranks = skills or {}
            rank = ranks.get(str(item.required_skill),
                             ranks.get(item.required_skill, 0))
            if isinstance(rank, dict):
                rank = rank.get("rank", rank.get("current", 0))
            if int(rank or 0) < item.required_skill_rank:
                return f"requires skill {item.required_skill} rank {item.required_skill_rank}"
        return None

    def upgrades(
        self, state: InventoryState, *, level: int, class_mask: int,
        skills: dict | None = None,
    ) -> tuple[EquipmentDecision, ...]:
        equipped = {row.equipment_slot: row for row in state.items
                    if row.equipped and row.equipment_slot is not None}
        decisions = []
        for candidate in state.items:
            if (candidate.equipped or candidate.equipment_slot is None
                    or not candidate.template_known):
                continue
            if self.ineligible_reason(
                    candidate, level=level, class_mask=class_mask,
                    skills=skills) is not None:
                continue
            slots = self._comparison_slots(candidate, equipped)
            if not slots:
                continue
            slot = slots[0]
            previous = sum(self.score(equipped.get(index)) for index in slots)
            score = self.score(candidate)
            if score <= previous:
                continue
            replaced = tuple(equipped[index].guid for index in slots
                             if equipped.get(index) is not None)
            decisions.append(EquipmentDecision(
                item=candidate, equipment_slot=slot, score=score,
                previous_score=previous, replaced_guids=replaced))
        return tuple(sorted(
            decisions,
            key=lambda row: (-(row.score - row.previous_score), row.item.guid)))

    def best_upgrade(
        self, state: InventoryState, *, level: int, class_mask: int,
        skills: dict | None = None,
    ) -> EquipmentDecision | None:
        upgrades = self.upgrades(
            state, level=level, class_mask=class_mask, skills=skills)
        return upgrades[0] if upgrades else None

    def _comparison_slots(self, candidate: Item, equipped: dict) -> tuple[int, ...]:
        if candidate.inventory_type == 17:  # INVTYPE_2HWEAPON
            return (15, 16)
        if candidate.inventory_type == 11:  # finger: replace weaker ring
            return (min((10, 11), key=lambda slot: self.score(equipped.get(slot))),)
        if candidate.inventory_type == 12:  # trinket
            return (min((12, 13), key=lambda slot: self.score(equipped.get(slot))),)
        return (candidate.equipment_slot,)


class InventoryPolicy:
    def __init__(self, rules: InventoryRules):
        self.rules = rules
        self.equipment = EquipmentPolicy(rules)

    def best_purchase(self, state: InventoryState) -> PurchaseDecision | None:
        # A hunter with zero projectiles cannot use its ranged rotation. Buying
        # a bag first can consume all cash and then free enough slots to stop
        # junk sales, permanently starving ammunition. Save for and buy the
        # depleted projectile supply before considering capacity upgrades.
        for rule in self.rules.supplies:
            if state.count_of(rule.item_id) != 0:
                continue
            row = next((row for row in state.vendor_items
                        if row.item_id == rule.item_id and row.class_id == 6
                        and self._valid_vendor_row(row)), None)
            if row is None:
                continue
            count = self._vendor_count(rule.target_count, row)
            if count <= 0 or (rule.max_price is not None
                              and row.buy_price > rule.max_price):
                continue
            total = row.buy_price * count
            if total > state.money:
                # A depleted projectile is an emergency: buy as many complete
                # vendor groups as possible rather than starving the rotation
                # until the full target becomes affordable.
                count = state.money // row.buy_price if row.buy_price else count
                total = row.buy_price * count
            if count <= 0 or total > state.money:
                return None
            return PurchaseDecision(
                item=row, vendor_slot=row.slot, count=count,
                total_price=total, reason="critical_projectile")

        largest_bag = max((row.container_slots for row in state.items
                           if row.class_id == 1), default=0)
        bags = [row for row in state.vendor_items
                if self._valid_vendor_row(row) and row.class_id == 1
                and row.container_slots > largest_bag]
        bags.sort(key=lambda row: (-row.container_slots, row.buy_price, row.item_id))
        if bags:
            bag = bags[0]
            if state.money >= bag.buy_price:
                return PurchaseDecision(
                    item=bag, vendor_slot=bag.slot, count=1,
                    total_price=bag.buy_price, reason="bag_capacity")
            # Keep copper for capacity rather than consuming it on supplies.
            return None

        owned = {item_id: state.count_of(item_id) for item_id in
                 {rule.item_id for rule in self.rules.supplies}}
        decisions = []
        for rule in self.rules.supplies:
            needed = rule.target_count - owned.get(rule.item_id, 0)
            if needed <= 0:
                continue
            row = next((row for row in state.vendor_items
                        if row.item_id == rule.item_id
                        and self._valid_vendor_row(row)), None)
            if row is None:
                continue
            count = self._vendor_count(needed, row)
            if count <= 0 or (rule.max_price is not None
                              and row.buy_price > rule.max_price):
                continue
            total = row.buy_price * count
            if total > state.money:
                continue
            decisions.append(PurchaseDecision(
                item=row, vendor_slot=row.slot, count=count,
                total_price=total, reason="supply"))
        return decisions[0] if decisions else None

    def purchase_block_reason(self, state: InventoryState) -> str | None:
        largest_bag = max((row.container_slots for row in state.items
                           if row.class_id == 1), default=0)
        for row in state.vendor_items:
            if (self._valid_vendor_row(row) and row.class_id == 1
                    and row.container_slots > largest_bag
                    and state.money < row.buy_price):
                return "saving_for_bag"
        return None

    def items_to_sell(
        self, state: InventoryState, *, level: int, class_mask: int,
        skills: dict | None = None, force: bool = False,
    ) -> tuple[Item, ...]:
        if not force and state.free_slots >= self.rules.min_free_slots:
            return ()
        upgrades = self.equipment.upgrades(
            state, level=level, class_mask=class_mask, skills=skills)
        protected_guids = {row.item.guid for row in upgrades}
        protected_ids = set(self.rules.protected_item_ids)
        protected_ids.update(self.rules.material_item_ids)
        candidates = []
        for row in state.items:
            if row.equipped or row.guid in protected_guids or not row.template_known:
                continue
            if row.item_id in protected_ids or row.quest_item:
                continue
            if row.class_id == 1 or row.quality > self.rules.sell_quality_max:
                continue
            if row.sell_price <= 0:
                continue
            candidates.append(row)
        candidates.sort(key=lambda row: (
            row.sell_price * max(1, row.count), self.equipment.score(row),
            row.guid))
        return tuple(candidates)

    @staticmethod
    def _vendor_count(item_count: int, row: Item) -> int:
        # CMSG_BUY_ITEM uses vendor groups. Ammunition advertises BuyCount=200,
        # so three purchases produce 600 arrows and cost three vendor prices.
        group_size = max(1, row.count)
        count = math.ceil(item_count / group_size)
        return count if row.stock < 0 else min(count, row.stock)

    @staticmethod
    def _valid_vendor_row(row: Item) -> bool:
        return row.slot >= 1 and row.stock != 0 and row.buy_price >= 0
