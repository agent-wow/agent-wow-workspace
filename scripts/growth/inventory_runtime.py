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
NPC_FLAG_REPAIR = 0x00001000
VENDOR_INTERACT_RANGE = 8.0
VENDOR_SEARCH_RANGE = 60.0  # Gornek to Duokna is about 44.6 yards.


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
        self._supply_excluded_vendors: set[int] = set()
        self._fallback_key: tuple[float, float] | None = None
        self._fallback_waypoint: int | None = None
        self._fallback_detour = False

    def step(self, now: float, view: WorldView, class_mask: int, *,
             reserve_slots: int = 0,
             fallback_position: dict | None = None,
             fallback_waypoints: tuple[dict, ...] = (),
             vendor_position: dict | None = None,
             vendor_waypoints: tuple[dict, ...] = ()) -> InventoryStep | None:
        if self.executor is None:
            return None
        resumed = self._resume_one()
        if resumed is not None:
            return resumed
        try:
            state = self.adapter.get_inventory()
        except AdapterError as exc:
            self.log(f"inventory read failed: {exc}")
            if reserve_slots > 0:
                return InventoryStep(None, "quest reward bag space is unavailable")
            return None
        if reserve_slots > 0 and state.free_slots >= reserve_slots:
            # A full inventory makes the server re-offer an unclaimable reward.
            # Routine maintenance must not buy or equip anything in this mode.
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

        depleted = {rule.item_id for rule in self.policy.rules.supplies
                    if state.count_of(rule.item_id) == 0}
        low_supplies = {rule.item_id for rule in self.policy.rules.supplies
                        if rule.is_low(state.count_of(rule.item_id))}
        supply_rule = next((rule for rule in self.policy.rules.supplies
                            if rule.item_id in depleted | low_supplies
                            and rule.vendor_position is not None), None)
        if supply_rule is not None:
            # Supply content owns its measured town route. The current quest's
            # turn-in may already have advanced to another zone and is not a
            # safe substitute for an ammunition vendor.
            vendor_position = supply_rule.vendor_position
            vendor_waypoints = supply_rule.vendor_waypoints
        if vendor_position is not None:
            fallback_position = vendor_position
            fallback_waypoints = vendor_waypoints
        damaged = {str(item.guid): item.durability for item in state.items
                   if item.equipped and item.max_durability > item.durability}
        broken = any(item.equipped and item.max_durability > 0
                     and item.durability <= 0 for item in state.items)
        repair_route = None
        if broken and not depleted and not low_supplies and reserve_slots <= 0:
            repair_route = self.policy.rules.repair_position
            if repair_route is not None:
                fallback_position = repair_route
                fallback_waypoints = self.policy.rules.repair_waypoints
        sell_items = self.policy.items_to_sell(
            state, level=view.level, class_mask=class_mask, skills=skills,
            force=broken)
        search_range = (120.0 if depleted or low_supplies or reserve_slots > 0
                        else VENDOR_SEARCH_RANGE)
        needs_repair_vendor = (
            damaged and not depleted and not low_supplies and reserve_slots <= 0)
        vendor = (self._nearest_vendor(
            view, search_range=search_range,
            required_flags=NPC_FLAG_VENDOR | NPC_FLAG_REPAIR)
            if needs_repair_vendor else None)
        if vendor is None and (not needs_repair_vendor or sell_items):
            # Prefer an armorer for combined repair/sale maintenance. A normal
            # merchant can still fund training by selling junk; repair_key below
            # remains gated on the server's repair flag.
            vendor = self._nearest_vendor(
                view, search_range=search_range, required_flags=NPC_FLAG_VENDOR)
        if (self._fallback_detour and vendor_position is not None
                and view.position
                and _distance(view.position, vendor_position)
                > VENDOR_INTERACT_RANGE):
            vendor = None
        if (supply_rule is not None and view.position
                and _distance(view.position, supply_rule.vendor_position)
                > VENDOR_INTERACT_RANGE):
            # A configured supply route is authoritative all the way to its
            # vendor. Do not let a nearer food/drink NPC divert an empty-ammo
            # character before it can verify the intended stockist's inventory.
            vendor = None
        if vendor is None:
            if (fallback_position and view.position
                    and (reserve_slots > 0 or depleted or low_supplies
                         or repair_route is not None)):
                destination = self._fallback_destination(
                    view, fallback_position, fallback_waypoints)
                if _distance(view.position, destination) > 4.0:
                    try:
                        _move_toward(self.adapter, view, destination)
                        detail = ("seeking a vendor near the quest turn-in"
                                  if reserve_slots > 0 else
                                  "returning to repair broken equipment"
                                  if repair_route is not None else
                                  "returning for depleted supplies")
                        return InventoryStep("move_to", detail)
                    except AdapterError as exc:
                        self.log(f"vendor fallback approach failed: {exc}")
                        if (depleted and fallback_waypoints
                                and "terrain height" in str(exc)):
                            self._fallback_detour = True
                        if depleted or low_supplies:
                            return InventoryStep(
                                None, f"depleted supply route is blocked: {exc}")
                        if repair_route is not None:
                            return InventoryStep(
                                None, f"broken equipment repair route is blocked: {exc}")
            if reserve_slots > 0:
                return InventoryStep(
                    None, "quest reward needs a free bag slot and a nearby vendor")
            if depleted or low_supplies:
                return InventoryStep(
                    None, "depleted supplies have no reachable vendor route")
            if repair_route is not None:
                return InventoryStep(None, "waiting for the repair vendor")
            return None
        vendor_guid = int(vendor["guid"])
        distance = _distance(view.position, vendor.get("position") or {})
        repair_key = None
        if (damaged and int(vendor.get("npc_flags") or 0) & NPC_FLAG_REPAIR
                == NPC_FLAG_REPAIR):
            signature = ",".join(
                f"{guid}:{value}" for guid, value in sorted(damaged.items()))
            repair_key = (
                f"inventory.repair:{vendor_guid}:{state.money}:{signature}")
        needs_repair = repair_key is not None and repair_key not in self._suppressed
        if (not depleted and not low_supplies and reserve_slots <= 0
                and not sell_items and not needs_repair
                and (distance > VENDOR_INTERACT_RANGE
                     or not state.vendor_loaded
                     or state.vendor_guid != vendor_guid)):
            # Do not oscillate between a quest route and a vendor merely to
            # discover optional purchases. Detours need a concrete maintenance
            # reason; an already-open vendor may still offer a useful bag.
            return None
        if distance > VENDOR_INTERACT_RANGE:
            destination = vendor.get("position") or {}
            try:
                _move_toward(self.adapter, view, destination)
                return InventoryStep("move_to", f"approaching vendor {vendor_guid}")
            except AdapterError as exc:
                self.log(f"vendor approach failed: {exc}")
                if (fallback_waypoints and (depleted or low_supplies
                                            or reserve_slots > 0)
                        and "terrain height" in str(exc)):
                    if fallback_position is not None:
                        self._fallback_key = (
                            float(fallback_position.get("x", 0.0)),
                            float(fallback_position.get("y", 0.0)))
                        self._fallback_waypoint = None
                    self._fallback_detour = True
                if reserve_slots > 0:
                    return InventoryStep(
                        None, f"quest reward vendor approach failed: {vendor_guid}")
                if depleted or low_supplies:
                    return InventoryStep(
                        None, f"depleted supply vendor is unreachable: {vendor_guid}")
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

        if (depleted or low_supplies) and any(not any(
                row.item_id == item_id and self.policy._valid_vendor_row(row)
                for row in state.vendor_items) for item_id in depleted | low_supplies):
            # itemTemplates describe known items, not this vendor's stock. Move
            # to another real vendor instead of concluding ammunition is sold
            # everywhere merely because its template has been observed.
            self._supply_excluded_vendors.add(vendor_guid)
            self.log(f"vendor {vendor_guid} lacks depleted supplies; trying another")
            return InventoryStep(None, f"vendor {vendor_guid} lacks depleted supplies")

        for item in sell_items:
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
                "received_count": purchase.count * max(1, purchase.item.count),
                "before_revision": state.revision,
                "before_failure_count": state.failure_count,
                "total_price": purchase.total_price,
            })
            if result is not None:
                return result

        if repair_key is not None:
            result = self._start(repair_key, "inventory.repair", {
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
                int(payload["before_count"])
                + int(payload.get("received_count", payload["count"])))
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

    def _nearest_vendor(self, view: WorldView, *,
                        search_range: float = VENDOR_SEARCH_RANGE,
                        required_flags: int = NPC_FLAG_VENDOR) -> dict | None:
        best = None
        best_distance = search_range
        for candidate in view.nearby:
            flags = int(candidate.get("npc_flags") or 0)
            if flags & required_flags != required_flags:
                continue
            if (candidate.get("guid") is None
                    or int(candidate["guid"]) in self._supply_excluded_vendors
                    or not candidate.get("has_position")):
                continue
            distance = _distance(view.position, candidate.get("position") or {})
            if distance < best_distance:
                best, best_distance = candidate, distance
        return best

    def _fallback_destination(self, view: WorldView, position: dict,
                              waypoints: tuple[dict, ...]) -> dict:
        key = (float(position.get("x", 0.0)), float(position.get("y", 0.0)))
        if key != self._fallback_key:
            self._fallback_key = key
            self._fallback_waypoint = None
            self._fallback_detour = False
        if not waypoints or (_distance(view.position, position) <= 60.0
                             and not self._fallback_detour):
            self._fallback_waypoint = None
            return position
        if self._fallback_waypoint is None:
            self._fallback_waypoint = min(
                range(len(waypoints)),
                key=lambda index: _distance(view.position, waypoints[index]))
        while self._fallback_waypoint < len(waypoints):
            waypoint = waypoints[self._fallback_waypoint]
            if _distance(view.position, waypoint) > 4.0:
                return waypoint
            self._fallback_waypoint += 1
        return position


def _move_toward(adapter, view: WorldView, destination: dict) -> None:
    distance = _distance(view.position, destination)
    if distance <= 0.01:
        return
    dx = float(destination.get("x", 0.0)) - float(view.position.get("x", 0.0))
    dy = float(destination.get("y", 0.0)) - float(view.position.get("y", 0.0))
    z = float(view.position.get("z", destination.get("z", 0.0)))
    last_error = None
    for candidate in (20.0, 12.0, 8.0, 4.0):
        step = min(candidate, distance)
        try:
            adapter.move_to(
                float(view.position.get("x", 0.0)) + dx / distance * step,
                float(view.position.get("y", 0.0)) + dy / distance * step,
                z)
            return
        except AdapterError as exc:
            last_error = exc
            error = str(exc)
            if not ("terrain height" in error
                    and "differs from estimated" in error):
                raise
    if last_error is not None:
        raise last_error


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
