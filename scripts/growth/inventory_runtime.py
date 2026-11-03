"""Durable inventory decisions for the solo growth machine.

Every mutating action has a stable checkpoint intent. A lost RPC response is
resolved only from fresh server facts; this runtime never interprets a timeout
as permission to buy, sell, repair or equip a second time.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from .actions import DurableActionExecutor
from .adapter import AdapterError, WorldAdapter, WorldView
from .items import InventoryPolicy, InventoryState

NPC_FLAG_VENDOR = 0x00000080
VENDOR_INTERACT_RANGE = 8.0
VENDOR_SEARCH_RANGE = 40.0


@dataclass(frozen=True)
class InventoryStep:
    action: str | None
    detail: str


class InventoryRuntime:
    def __init__(
        self,
        policy: InventoryPolicy,
        adapter: WorldAdapter,
        store=None,
        *,
        clock: Callable[[], float] = time.time,
        logger: Callable[[str], None] | None = None,
    ):
        self.policy = policy
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.log = logger or (lambda message: None)
        self.executor = (DurableActionExecutor(store, clock=clock)
                         if store is not None else None)
        self._pending: dict[str, tuple[str, dict]] = {}
        self._suppressed: set[str] = set()
        self._recovered = False
        self._list_vendor: int | None = None
        self._list_deadline = 0.0

    def step(self, now: float, view: WorldView, class_mask: int) -> InventoryStep | None:
        if self.executor is None:
            return None
        resumed = self._resume_one()
        if resumed is not None:
            return resumed
        try:
            state = self.adapter.get_inventory()
        except AdapterError as exc:
            self.log(f"inventory read failed: {exc}")
            return None

        skills = view.self_state.get("skills") or {}
        upgrade = self.policy.equipment.best_upgrade(
            state, level=view.level, class_mask=class_mask, skills=skills)
        if upgrade is not None:
            item = upgrade.item
            rank = _skill_rank(skills, item.required_skill)
            key = (f"inventory.equip:{item.guid}:{upgrade.equipment_slot}:"
                   f"l{view.level}:s{rank}")
            result = self._start(key, "inventory.equip", {
                "item_guid": item.guid,
                "destination_slot": upgrade.equipment_slot,
                "before_revision": state.revision,
                "before_failure_count": state.failure_count,
                "level": view.level,
                "skill_rank": rank,
            })
            if result is not None:
                return result

        vendor = self._nearest_vendor(view)
        if vendor is None:
            return None
        vendor_guid = int(vendor["guid"])
        distance = _distance(view.position, vendor.get("position") or {})
        if distance > VENDOR_INTERACT_RANGE:
            destination = vendor.get("position") or {}
            try:
                self.adapter.move_to(
                    float(destination.get("x", 0.0)),
                    float(destination.get("y", 0.0)),
                    float(destination.get("z", view.position.get("z", 0.0))),
                )
                return InventoryStep("move_to", f"approaching vendor {vendor_guid}")
            except AdapterError as exc:
                self.log(f"vendor approach failed: {exc}")
                return None

        if not state.vendor_loaded or state.vendor_guid != vendor_guid:
            if self._list_vendor == vendor_guid and now < self._list_deadline:
                return InventoryStep(None, f"waiting for vendor {vendor_guid} inventory")
            try:
                self.adapter.list_inventory(vendor_guid)
            except AdapterError as exc:
                self.log(f"vendor list failed: {exc}")
                self._list_vendor = None
                return None
            self._list_vendor = vendor_guid
            self._list_deadline = now + 10.0
            return InventoryStep("list_inventory", f"opened vendor {vendor_guid}")
        self._list_vendor = None

        for item in self.policy.items_to_sell(
                state, level=view.level, class_mask=class_mask, skills=skills):
            count = item.count
            key = f"inventory.sell:{vendor_guid}:{item.guid}:{count}:{state.money}"
            result = self._start(key, "inventory.sell", {
                "vendor_guid": vendor_guid, "item_guid": item.guid,
                "count": 0, "before_stack": count,
                "before_money": state.money, "before_revision": state.revision,
                "before_failure_count": state.failure_count,
                "expected_copper": item.sell_price * count,
            })
            if result is not None:
                return result

        purchase = self.policy.best_purchase(state)
        if purchase is not None:
            item = purchase.item
            before_count = state.count_of(item.item_id)
            key = (f"inventory.buy:{vendor_guid}:{item.item_id}:{purchase.vendor_slot}:"
                   f"{purchase.count}:{state.money}:{before_count}")
            result = self._start(key, "inventory.buy", {
                "vendor_guid": vendor_guid, "item_id": item.item_id,
                "slot": purchase.vendor_slot, "count": purchase.count,
                "before_count": before_count, "before_money": state.money,
                "before_revision": state.revision,
                "before_failure_count": state.failure_count,
                "total_price": purchase.total_price,
            })
            if result is not None:
                return result

        damaged = {str(item.guid): item.durability for item in state.items
                   if item.max_durability > item.durability}
        if damaged:
            signature = ",".join(f"{guid}:{value}" for guid, value in sorted(damaged.items()))
            key = f"inventory.repair:{vendor_guid}:{signature}"
            result = self._start(key, "inventory.repair", {
                "vendor_guid": vendor_guid, "item_guid": 0,
                "before_durability": damaged, "before_money": state.money,
                "before_revision": state.revision,
                "before_failure_count": state.failure_count,
            })
            if result is not None:
                return result
        return None

    def _start(self, key: str, kind: str, payload: dict) -> InventoryStep | None:
        if key in self._suppressed:
            return None
        existing = self.store.intent(key)
        if existing is not None and existing.get("state") in (
                "confirmed", "failed", "abandoned"):
            self._suppressed.add(key)
            if existing.get("state") != "confirmed":
                self.log(f"{key} suppressed after {existing.get('state')}")
            return None
        self._pending[key] = (kind, payload)
        return self._drive(key)

    def _resume_one(self) -> InventoryStep | None:
        if not self._pending and not self._recovered:
            self._recovered = True
            for state in ("sending", "sent", "unknown"):
                for intent in self.store.intents(state=state):
                    kind = str(intent.get("kind") or "")
                    payload = intent.get("payload")
                    if kind.startswith("inventory.") and isinstance(payload, dict):
                        self._pending[str(intent["intent_key"])] = (kind, payload)
        if not self._pending:
            return None
        key = next(iter(self._pending))
        return self._drive(key)

    def _drive(self, key: str) -> InventoryStep:
        kind, payload = self._pending[key]
        result = self.executor.run(
            key, kind, payload,
            dispatch=lambda: self._dispatch(kind, payload),
            reconcile=lambda: self._reconcile(kind, payload),
        )
        if result.state in ("confirmed", "failed", "abandoned"):
            self._pending.pop(key, None)
            self._suppressed.add(key)
        return InventoryStep(kind, f"{key}: {result.state}")

    def _dispatch(self, kind: str, payload: dict) -> None:
        if kind == "inventory.equip":
            self.adapter.equip_item(payload["item_guid"], payload["destination_slot"])
        elif kind == "inventory.buy":
            self.adapter.buy_item(
                payload["vendor_guid"], payload["item_id"],
                payload["slot"], payload["count"])
        elif kind == "inventory.sell":
            self.adapter.sell_item(
                payload["vendor_guid"], payload["item_guid"], payload["count"])
        elif kind == "inventory.repair":
            self.adapter.repair_item(payload["vendor_guid"], payload["item_guid"])
        else:
            raise AdapterError(f"unknown inventory action {kind}")

    def _reconcile(self, kind: str, payload: dict) -> bool | None:
        state = self.adapter.get_inventory()
        if state.revision <= int(payload.get("before_revision", 0)):
            return None
        if (state.failure_count > int(payload.get("before_failure_count", 0))
                and _failure_matches(state, kind, payload)):
            return False
        if kind == "inventory.equip":
            item = state.by_guid(int(payload["item_guid"]))
            return True if (item is not None and item.equipped
                            and item.equipment_slot == int(payload["destination_slot"])) else None
        if kind == "inventory.buy":
            count_ok = state.count_of(int(payload["item_id"])) >= (
                int(payload["before_count"]) + int(payload["count"]))
            money_ok = state.money <= int(payload["before_money"]) - int(payload["total_price"])
            return True if count_ok and money_ok else None
        if kind == "inventory.sell":
            item = state.by_guid(int(payload["item_guid"]))
            before_stack = int(payload["before_stack"])
            requested = int(payload["count"])
            expected_stack = 0 if requested == 0 else max(0, before_stack - requested)
            current_stack = item.count if item is not None else 0
            item_ok = current_stack <= expected_stack
            money_ok = state.money >= (
                int(payload["before_money"]) + int(payload["expected_copper"]))
            return True if item_ok and money_ok else None
        if kind == "inventory.repair":
            improved = False
            for guid, old_value in payload["before_durability"].items():
                item = state.by_guid(int(guid))
                if item is not None and item.durability > int(old_value):
                    improved = True
                    break
            return True if improved and state.money <= int(payload["before_money"]) else None
        return None

    @staticmethod
    def _nearest_vendor(view: WorldView) -> dict | None:
        best = None
        best_distance = VENDOR_SEARCH_RANGE
        for candidate in view.nearby:
            if not int(candidate.get("npc_flags") or 0) & NPC_FLAG_VENDOR:
                continue
            if candidate.get("guid") is None or not candidate.get("has_position"):
                continue
            distance = _distance(view.position, candidate.get("position") or {})
            if distance < best_distance:
                best, best_distance = candidate, distance
        return best


def _failure_matches(state: InventoryState, kind: str, payload: dict) -> bool:
    failure = state.last_failure
    operation = kind.removeprefix("inventory.")
    if failure.get("operation") != operation:
        return False
    if kind == "inventory.buy":
        return int(failure.get("item_id") or 0) == int(payload["item_id"])
    return int(failure.get("item_guid") or 0) == int(payload.get("item_guid") or 0)


def _skill_rank(skills: dict, skill_id: int) -> int:
    value = skills.get(str(skill_id), skills.get(skill_id, 0))
    if isinstance(value, dict):
        value = value.get("rank", value.get("current", 0))
    return int(value or 0)


def _distance(a: dict, b: dict) -> float:
    return math.hypot(float(a.get("x", 0.0)) - float(b.get("x", 0.0)),
                      float(a.get("y", 0.0)) - float(b.get("y", 0.0)))
