"""Durable, roster-only trade planning and read-back reconciliation."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from .actions import DurableActionExecutor
from .adapter import AdapterError, WorldAdapter
from .items import InventoryState, Item
from .roster import CharacterSpec, Roster
from .social import RuntimeAction, TradeItem, TradeState

TRADE_PLAN_KEY = "cooperation.trade_plan"


def _reject_redispatch():
    raise AdapterError("an in-flight trade action cannot be redispatched")


@dataclass(frozen=True)
class TradeAssignment:
    item_guid: int
    item_id: int
    count: int
    bag: int
    slot: int
    trade_slot: int


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    peer: str
    peer_guid: int
    expires_at: float
    initiate: bool
    own_items: tuple[TradeAssignment, ...]
    own_money: int
    expected_items: tuple[TradeItem, ...]
    expected_money: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "TradePlan":
        return cls(
            plan_id=str(raw["plan_id"]), peer=str(raw["peer"]),
            peer_guid=int(raw["peer_guid"]), expires_at=float(raw["expires_at"]),
            initiate=bool(raw["initiate"]),
            own_items=tuple(TradeAssignment(**row) for row in raw["own_items"]),
            own_money=int(raw["own_money"]),
            expected_items=tuple(TradeItem(**row) for row in raw["expected_items"]),
            expected_money=int(raw["expected_money"]),
        )


class TradeRuntime:
    def __init__(
        self, roster: Roster, spec: CharacterSpec, adapter: WorldAdapter, store,
        *, clock: Callable[[], float] = time.time,
    ):
        self.roster = roster
        self.spec = spec
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.actions = DurableActionExecutor(
            store, clock=clock, unknown_timeout=45.0)
        self.plan = self._load_plan()

    def configure(
        self, peer: str, own_items: tuple[Item, ...] | list[Item], *,
        own_money: int = 0, expected_items: tuple[TradeItem, ...] | list[TradeItem] = (),
        expected_money: int = 0, initiate: bool = True, ttl: float = 60.0,
    ) -> TradePlan:
        spec = self.roster.by_character().get(peer)
        if spec is None or spec.guid == self.spec.guid:
            raise ValueError("trade peer must be another roster character")
        contacts = {row.guid for row in self.adapter.get_party().contacts}
        if any(peer.guid != self.spec.guid and peer.guid not in contacts
               for peer in self.roster.characters):
            raise ValueError("trade requires a complete roster contact list")
        if own_money < 0 or expected_money < 0 or len(own_items) > 6:
            raise ValueError("invalid trade money or item count")
        assignments = tuple(TradeAssignment(
            item_guid=item.guid, item_id=item.item_id, count=item.count,
            bag=item.bag, slot=item.slot, trade_slot=index,
        ) for index, item in enumerate(own_items))
        if len({row.item_guid for row in assignments}) != len(assignments):
            raise ValueError("duplicate item guid in trade plan")
        self.plan = TradePlan(
            plan_id=uuid.uuid4().hex, peer=peer, peer_guid=spec.guid,
            expires_at=self.clock() + float(ttl), initiate=bool(initiate),
            own_items=assignments, own_money=int(own_money),
            expected_items=tuple(sorted(expected_items, key=lambda row: row.slot)),
            expected_money=int(expected_money),
        )
        self.store.set_meta(TRADE_PLAN_KEY, json.dumps(self.plan.to_dict(), sort_keys=True))
        return self.plan

    def clear(self) -> None:
        self.plan = None
        self.store.set_meta(TRADE_PLAN_KEY, "")

    def step(self, now: float, inventory: InventoryState | None = None) -> RuntimeAction | None:
        try:
            state = inventory or self.adapter.get_inventory()
        except AdapterError:
            return None
        trade = state.trade
        plan = self.plan
        if trade.trader_guid and trade.trader_guid not in {
                row.guid for row in self.roster.characters if row.guid != self.spec.guid}:
            return self._cancel(trade, "non-roster trader")
        if plan is None:
            resumed = self._resume_unplanned()
            if resumed is not None:
                return resumed
            if trade.pending or trade.active:
                return self._cancel(trade, "no explicit trade plan")
            return None
        resumed = self._resume_intent(plan, state)
        if resumed is not None:
            return resumed
        if ((trade.complete or trade.cancelled) and not self._has_plan_intents(plan)):
            # Terminal packets belong to the previous plan until this plan has
            # dispatched an action or received a fresh server trade state.
            trade = TradeState()
        if now >= plan.expires_at and not trade.complete:
            if trade.pending or trade.active:
                return self._cancel(trade, "trade plan expired")
            self.clear()
            return RuntimeAction("trade_expired", plan.plan_id)
        if trade.trader_guid not in (0, plan.peer_guid):
            return self._cancel(trade, "trade peer changed")

        if trade.pending:
            return self._begin(trade, plan)
        if not trade.active:
            if trade.complete:
                self.clear()
                return RuntimeAction("trade_complete", plan.plan_id)
            if trade.cancelled:
                self.clear()
                return RuntimeAction("trade_cancelled", plan.plan_id)
            if plan.initiate:
                return self._initiate(trade, plan, state)
            return None

        if not self._own_offer_matches(trade, plan):
            assignment = self._missing_assignment(trade, plan)
            if assignment is not None:
                return self._set_item(trade, plan, assignment)
            if trade.own_money != plan.own_money:
                return self._set_gold(trade, plan)
            return self._cancel(trade, "own offer contains an unexpected row")
        if not self._expected_offer_matches(trade, plan):
            if self._expected_offer_incomplete(trade, plan):
                return RuntimeAction("trade_wait_peer_offer", plan.plan_id)
            return self._cancel(trade, "peer offer differs from the explicit plan")
        if not self._inventory_identity_valid(state, plan):
            return self._cancel(trade, "inventory identity changed before accept")
        return self._accept(trade, plan, state)

    def _resume_unplanned(self) -> RuntimeAction | None:
        for state in ("sending", "sent", "unknown"):
            for intent in self.store.intents(state=state):
                key = str(intent["intent_key"])
                kind = str(intent.get("kind") or "")
                payload = intent.get("payload")
                if not key.startswith("trade:unplanned:") or not isinstance(payload, dict):
                    continue
                result = self.actions.run(
                    key, kind, payload, _reject_redispatch,
                    lambda kind=kind, payload=payload: self._reconcile_intent(
                        None, kind, payload))
                return RuntimeAction(kind, result.state)
        return None

    def _resume_intent(self, plan, inventory) -> RuntimeAction | None:
        prefix = f"trade:{plan.plan_id}:"
        for state in ("sending", "sent", "unknown", "abandoned"):
            for intent in self.store.intents(state=state):
                key = str(intent["intent_key"])
                kind = str(intent.get("kind") or "")
                payload = intent.get("payload")
                if not key.startswith(prefix) or not isinstance(payload, dict):
                    continue
                if (kind == "trade_accept" and inventory.trade.active
                        and (not self._own_offer_matches(inventory.trade, plan)
                             or not self._expected_offer_matches(inventory.trade, plan))):
                    continue  # The normal path must cancel a changed offer.
                result = self.actions.run(
                    key, kind, payload,
                    _reject_redispatch,
                    lambda kind=kind, payload=payload: self._reconcile_intent(
                        plan, kind, payload))
                if result.state in {"unknown", "abandoned"}:
                    return RuntimeAction(kind, result.state)
                if kind == "trade_cancel" and result.state == "confirmed":
                    self.clear()
                    return RuntimeAction("trade_cancel", plan.plan_id)
                if (kind == "trade_accept" and result.state == "confirmed"
                        and not (inventory.trade.active or inventory.trade.pending)):
                    self.clear()
                    return RuntimeAction("trade_complete_reconciled", plan.plan_id)
                if result.state == "failed":
                    return RuntimeAction(kind, result.state)
        return None

    def _reconcile_intent(self, plan, kind, payload) -> bool | None:
        if kind == "trade_accept":
            return self._accept_readback(plan, payload)
        try:
            trade = self.adapter.get_inventory().trade
        except AdapterError:
            return None
        if trade.cancelled:
            return True if kind == "trade_cancel" else False
        if kind == "trade_initiate":
            return True if (trade.trader_guid == plan.peer_guid
                            and (trade.pending or trade.active)) else None
        if kind == "trade_begin":
            return True if trade.active else None
        if kind == "trade_set_item":
            return True if any(
                row.slot == int(payload["trade_slot"])
                and row.item_id == int(payload["item_id"])
                and row.count == int(payload["count"])
                for row in trade.own_items) else None
        if kind == "trade_set_gold":
            return True if trade.own_money == int(payload["copper"]) else None
        if kind == "trade_cancel":
            return True if not (trade.active or trade.pending) else None
        return None

    def _initiate(self, trade, plan, inventory) -> RuntimeAction:
        if inventory.money < plan.own_money or not self._inventory_identity_valid(inventory, plan):
            return RuntimeAction("trade_blocked", "planned items or money are unavailable")
        key = f"trade:{plan.plan_id}:initiate"
        result = self.actions.run(
            key, "trade_initiate", {"peer_guid": plan.peer_guid},
            lambda: self.adapter.trade("initiate", target_guid=plan.peer_guid),
            lambda: self._trade_readback(lambda current: (
                True if current.trader_guid == plan.peer_guid and (current.pending or current.active)
                else False if current.cancelled else None)))
        return RuntimeAction("trade_initiate", result.state)

    def _begin(self, trade, plan) -> RuntimeAction:
        result = self.actions.run(
            f"trade:{plan.plan_id}:begin", "trade_begin", {"trade_id": trade.trade_id},
            lambda: self.adapter.trade("begin", trade_id=trade.trade_id),
            lambda: self._trade_readback(lambda current: (
                True if current.active else False if current.cancelled else None)))
        return RuntimeAction("trade_begin", result.state)

    def _set_item(self, trade, plan, row: TradeAssignment) -> RuntimeAction:
        payload = asdict(row)
        result = self.actions.run(
            f"trade:{plan.plan_id}:set:{row.trade_slot}:{row.item_guid}",
            "trade_set_item", payload,
            lambda: self.adapter.trade(
                "set_item", trade_id=trade.trade_id, trade_slot=row.trade_slot,
                bag=row.bag, slot=row.slot, item_guid=row.item_guid),
            lambda: self._trade_readback(lambda current: (
                True if any(item.slot == row.trade_slot and item.item_id == row.item_id
                            and item.count == row.count for item in current.own_items)
                else False if current.cancelled else None)))
        return RuntimeAction("trade_set_item", result.state)

    def _set_gold(self, trade, plan) -> RuntimeAction:
        result = self.actions.run(
            f"trade:{plan.plan_id}:gold:{plan.own_money}", "trade_set_gold",
            {"copper": plan.own_money},
            lambda: self.adapter.trade(
                "set_gold", trade_id=trade.trade_id, copper=plan.own_money),
            lambda: self._trade_readback(lambda current: (
                True if current.own_money == plan.own_money
                else False if current.cancelled else None)))
        return RuntimeAction("trade_set_gold", result.state)

    def _accept(self, trade, plan, inventory) -> RuntimeAction:
        baseline_counts = {item_id: inventory.count_of(item_id) for item_id in
                           {row.item_id for row in plan.expected_items}}
        payload = {
            "trade_id": trade.trade_id, "revision": trade.revision,
            "money": inventory.money, "own_guids": [row.item_guid for row in plan.own_items],
            "baseline_counts": baseline_counts,
        }
        def dispatch():
            self.adapter.trade(
                "accept", trade_id=trade.trade_id, expected_revision=trade.revision)
        result = self.actions.run(
            f"trade:{plan.plan_id}:accept", "trade_accept", payload,
            dispatch, lambda: self._accept_readback(plan, payload))
        return RuntimeAction("trade_accept", result.state)

    def _cancel(self, trade, reason) -> RuntimeAction:
        plan_id = self.plan.plan_id if self.plan else "unplanned"
        key = f"trade:{plan_id}:cancel:{trade.trade_id}:{trade.revision}"
        result = self.actions.run(
            key, "trade_cancel", {"reason": reason},
            lambda: self.adapter.trade("cancel", trade_id=trade.trade_id),
            lambda: self._trade_readback(lambda current: (
                True if current.cancelled or not (current.active or current.pending)
                else None)))
        if result.state == "confirmed":
            self.clear()
        return RuntimeAction("trade_cancel", f"{reason}: {result.state}")

    def _accept_readback(self, plan, payload) -> bool | None:
        try:
            current = self.adapter.get_inventory()
        except AdapterError:
            return None
        trade = current.trade
        if trade.complete:
            return True
        if trade.active or trade.pending:
            return None
        # A cancel/disconnect notification alone does not prove that an accepted
        # trade was rolled back. Prefer a full inventory/money reconciliation.
        outgoing_present = [current.by_guid(guid) is not None
                            for guid in payload["own_guids"]]
        expected_counts: dict[int, int] = {}
        for row in plan.expected_items:
            expected_counts[row.item_id] = expected_counts.get(row.item_id, 0) + row.count
        incoming_ok = True
        unchanged = True
        for item_id, count in expected_counts.items():
            baseline = int(payload["baseline_counts"].get(str(item_id),
                              payload["baseline_counts"].get(item_id, 0)))
            current_count = current.count_of(item_id)
            incoming_ok = incoming_ok and current_count >= baseline + count
            unchanged = unchanged and current_count == baseline
        expected_money = int(payload["money"]) - plan.own_money + plan.expected_money
        if (not any(outgoing_present) and incoming_ok
                and current.money == expected_money):
            return True
        # Failure is definitive only when every outgoing item and the complete
        # pre-trade money/inventory counts are still present. Anything else is
        # ambiguous and must expire without another trade attempt.
        if (trade.cancelled and all(outgoing_present) and unchanged
                and current.money == int(payload["money"])):
            return False
        return None

    def _trade_readback(self, predicate) -> bool | None:
        try:
            return predicate(self.adapter.get_inventory().trade)
        except AdapterError:
            return None

    @staticmethod
    def _inventory_identity_valid(inventory, plan) -> bool:
        for row in plan.own_items:
            item = inventory.by_guid(row.item_guid)
            if (item is None or item.equipped or item.bag != row.bag
                    or item.slot != row.slot or item.item_id != row.item_id
                    or item.count < row.count):
                return False
        return inventory.money >= plan.own_money

    @staticmethod
    def _own_offer_matches(trade: TradeState, plan: TradePlan) -> bool:
        expected = tuple(sorted(
            (row.trade_slot, row.item_id, row.count) for row in plan.own_items))
        actual = tuple(sorted(
            (row.slot, row.item_id, row.count) for row in trade.own_items))
        return actual == expected and trade.own_money == plan.own_money

    @staticmethod
    def _missing_assignment(trade: TradeState, plan: TradePlan):
        actual = {(row.slot, row.item_id, row.count) for row in trade.own_items}
        return next((row for row in plan.own_items
                     if (row.trade_slot, row.item_id, row.count) not in actual), None)

    @staticmethod
    def _expected_offer_matches(trade: TradeState, plan: TradePlan) -> bool:
        expected = tuple(sorted(
            (row.slot, row.item_id, row.count, row.random_property)
            for row in plan.expected_items))
        actual = tuple(sorted(
            (row.slot, row.item_id, row.count, row.random_property)
            for row in trade.trader_items))
        return actual == expected and trade.trader_money == plan.expected_money

    def _has_plan_intents(self, plan: TradePlan) -> bool:
        prefix = f"trade:{plan.plan_id}:"
        return any(str(row["intent_key"]).startswith(prefix)
                   for row in self.store.intents())

    @staticmethod
    def _expected_offer_incomplete(trade: TradeState, plan: TradePlan) -> bool:
        expected = {
            (row.slot, row.item_id, row.count, row.random_property)
            for row in plan.expected_items}
        actual = [
            (row.slot, row.item_id, row.count, row.random_property)
            for row in trade.trader_items]
        if len(actual) != len(set(actual)) or any(row not in expected for row in actual):
            return False
        if trade.trader_money not in (0, plan.expected_money):
            return False
        return len(actual) < len(expected) or trade.trader_money != plan.expected_money

    def _load_plan(self) -> TradePlan | None:
        raw = self.store.get_meta(TRADE_PLAN_KEY)
        if not raw:
            return None
        try:
            plan = TradePlan.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return plan
