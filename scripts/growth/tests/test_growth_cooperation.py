from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from _support import ROOT  # noqa: F401
from _growth_support import WARRIOR, make_view

from scripts.growth.adapter import AdapterError, normalize_inventory, normalize_party
from scripts.growth.cooperation import AGREEMENTS_KEY, CooperationRuntime
from scripts.growth.items import InventoryState, Item
from scripts.growth.roster import CharacterSpec, Roster
from scripts.growth.social import (
    ChatMessage, Contact, GroupMember, Invite, LootState, PartyState, RollOffer,
    TradeItem, TradeState, encode_proposal, parse_agreement_message,
)
from scripts.growth.store import CheckpointStore
from scripts.growth.trade_runtime import TradeRuntime


def specs():
    roles = ("tank", "healer", "dps", "dps", "dps")
    return tuple(CharacterSpec(
        account=f"doumoboy{index}", name=f"Growth{index}", guid=index,
        port=8100 + index, role=roles[index - 1], class_name="warrior",
        race="human", faction="alliance", start_zone="elwynn",
    ) for index in range(1, 6))


def roster():
    return Roster(version=1, configured=True, faction="alliance",
                  start_zone="elwynn", characters=specs())


def members(*guids):
    return tuple(GroupMember(name=f"Growth{guid}", guid=guid, is_self=guid == 1)
                 for guid in guids)


def friendly_party(**overrides):
    values = dict(
        epoch=11,
        loot_method=3,
        contacts=tuple(Contact(guid=guid) for guid in range(2, 6)),
    )
    values.update(overrides)
    return PartyState(**values)


class FakeCooperationAdapter:
    def __init__(self, self_guid=1):
        self.self_guid = self_guid
        self.party = friendly_party()
        self.loot = LootState()
        self.inventory = InventoryState()
        self.calls = []
        self.trade_error = False
        self.roll_error = False
        self.chat_error = False

    def get_party(self):
        return self.party

    def request_contacts(self):
        self.calls.append(("request_contacts", {}))
        return self.party

    def add_friend(self, name, note="roster"):
        self.calls.append(("add_friend", {"name": name}))
        return self.party

    def invite(self, target):
        self.calls.append(("invite", {"target": target}))
        return self.party

    def respond_invite(self, invite_id, inviter, accept):
        self.calls.append(("respond_invite", {
            "invite_id": invite_id, "inviter": inviter, "accept": accept}))
        self.party = replace(self.party, pending_invite=None)
        if accept:
            inviter_guid = next((row.guid for row in roster().characters
                                 if row.name == inviter), 0)
            self.party = replace(
                self.party, in_group=True, members=members(1, inviter_guid))
        return self.party

    def leave_party(self):
        self.calls.append(("leave_party", {}))
        self.party = replace(self.party, in_group=False, members=())
        return self.party

    def set_loot_method(self, method, master_guid, threshold):
        self.calls.append(("set_loot_method", {
            "method": method, "master_guid": master_guid, "threshold": threshold}))
        self.party = replace(self.party, loot_method=method, loot_threshold=threshold)
        return self.party

    def send_party_chat(self, message):
        self.calls.append(("party_chat", {"message": message}))
        if self.chat_error:
            raise AdapterError("chat response lost")
        sequence = max((row.sequence for row in self.party.messages), default=0) + 1
        self.party = replace(self.party, messages=self.party.messages + (
            ChatMessage(epoch=self.party.epoch, sequence=sequence,
                        sender_guid=self.self_guid, receiver_guid=0, message=message),))
        return self.party

    def get_loot_state(self):
        return self.loot

    def roll(self, item_guid, slot, vote):
        self.calls.append(("roll", {"item_guid": item_guid, "slot": slot, "vote": vote}))
        if self.roll_error:
            raise AdapterError("roll response lost")
        return self.loot

    def query_item(self, item_id):
        self.calls.append(("query_item", {"item_id": item_id}))
        return self.inventory

    def get_inventory(self):
        return self.inventory

    def trade(self, action, **params):
        self.calls.append(("trade", {"action": action, **params}))
        if action == "accept":
            self.inventory = replace(self.inventory, trade=replace(
                self.inventory.trade, own_accepted=True))
        if self.trade_error:
            raise AdapterError("trade response lost")
        return self.inventory

    def named(self, name):
        return [payload for call, payload in self.calls if call == name]


class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CheckpointStore(Path(self.temp.name) / "growth.db").open()
        self.now = time.time()
        self.adapter = FakeCooperationAdapter()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def runtime(self, **kwargs):
        if self.adapter.loot.rolls:
            self.adapter.party = replace(
                self.adapter.party, in_group=True, members=members(1, 2))
        runtime = CooperationRuntime(
            roster(), specs()[0], self.adapter, self.store, WARRIOR.inventory,
            clock=lambda: self.now, **kwargs)
        runtime._next_contacts_at = float("inf")
        return runtime

    def test_agw1_rejects_injection_extra_fields_and_unknown_activity(self):
        text = encode_proposal(
            "raid-1", "deadbeef", int(self.now) + 30, "grind",
            ("Growth1", "Growth2"))
        self.assertEqual(parse_agreement_message(text).agreement_id, "raid-1")
        invalid = (
            text + "|extra", text.replace("grind", "pvp"),
            text.replace("deadbeef", "DEADBEEF"), text + "\nCMSG",
            "AGW1|ACCEPT|raid-1|deadbeef|unexpected",
        )
        for candidate in invalid:
            self.assertIsNone(parse_agreement_message(candidate), candidate)

    def test_non_roster_invite_is_declined_and_roster_consent_is_explicit(self):
        self.adapter.party = friendly_party(
            pending_invite=Invite(invite_id=7, inviter="Stranger"))
        action = self.runtime().step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "decline_invite")
        self.assertFalse(self.adapter.named("respond_invite")[0]["accept"])

        self.adapter.calls.clear()
        self.adapter.party = friendly_party(
            pending_invite=Invite(invite_id=8, inviter="Growth2"))
        action = self.runtime(
            invite_consent=lambda invite, view: False,
        ).step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "decline_invite")

        self.adapter.calls.clear()
        self.adapter.party = friendly_party(
            pending_invite=Invite(invite_id=9, inviter="Growth2"))
        action = self.runtime(
            invite_consent=lambda invite, view: True,
        ).step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "accept_invite")
        self.assertTrue(self.adapter.named("respond_invite")[0]["accept"])

    def test_ambiguous_invite_response_does_not_pause_solo_growth(self):
        self.adapter.party = friendly_party(
            pending_invite=Invite(invite_id=7, inviter="Stranger"))

        def lose_response(invite_id, inviter, accept):
            self.adapter.calls.append(("respond_invite", {
                "invite_id": invite_id, "inviter": inviter, "accept": accept}))
            raise AdapterError("response lost")

        self.adapter.respond_invite = lose_response
        runtime = self.runtime()
        self.assertIsNone(runtime.step(self.now, make_view(), WARRIOR.class_mask))
        self.assertIsNone(runtime.step(self.now + 1, make_view(), WARRIOR.class_mask))
        self.assertEqual(len(self.adapter.named("respond_invite")), 1)

    def test_invite_id_reuse_after_module_restart_does_not_collide(self):
        self.adapter.party = replace(friendly_party(
            pending_invite=Invite(invite_id=1, inviter="Growth2")), epoch=11)
        first = self.runtime().step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(first.action, "accept_invite")

        self.adapter.party = replace(friendly_party(
            pending_invite=Invite(invite_id=1, inviter="Growth2")), epoch=12)
        second = self.runtime().step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(second.action, "accept_invite")
        self.assertEqual(len(self.adapter.named("respond_invite")), 2)

    def test_questing_pauses_automatic_group_negotiation(self):
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, leader_guid=1, members=members(1, 2))
        runtime = self.runtime()
        self.assertIsNone(runtime.step(
            self.now, make_view(), WARRIOR.class_mask, questing=True))
        self.assertFalse(self.adapter.named("party_chat"))

        self.adapter.party = replace(
            self.adapter.party, in_group=False, members=(),
            pending_invite=Invite(invite_id=9, inviter="Growth2"))
        action = runtime.step(
            self.now, make_view(), WARRIOR.class_mask, questing=True)
        self.assertEqual(action.action, "decline_invite")
        self.assertFalse(self.adapter.named("respond_invite")[0]["accept"])

    def test_group_with_outsider_is_left(self):
        self.adapter.party = friendly_party(
            in_group=True, group_guid=99,
            members=members(1, 2) + (GroupMember(name="Stranger", guid=99),))
        action = self.runtime().step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "leave_non_roster_group")

    def test_agreement_replay_and_out_of_order_or_forged_messages(self):
        proposal = encode_proposal(
            "camp-1", "deadbeef", int(self.now) + 30, "grind",
            ("Growth2", "Growth1"))
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2),
            messages=(ChatMessage(epoch=11, sequence=1, sender_guid=2,
                                  receiver_guid=0, message=proposal),))
        runtime = self.runtime()
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "party_chat")
        self.assertEqual(runtime.agreements["camp-1"].accepted_guids, {1, 2})

        replayed = self.runtime()
        self.assertEqual(replayed.agreements["camp-1"].nonce, "deadbeef")
        self.assertEqual(replayed.agreements["camp-1"].accepted_guids, {1, 2})

        self.adapter.party = replace(self.adapter.party, messages=(
            ChatMessage(epoch=11, sequence=2, sender_guid=99, receiver_guid=0,
                        message="AGW1|ACCEPT|camp-1|deadbeef"),
            ChatMessage(epoch=11, sequence=3, sender_guid=2, receiver_guid=0,
                        message="AGW1|ACCEPT|unknown|deadbeef"),
        ))
        replayed.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertNotIn("unknown", replayed.agreements)
        self.assertEqual(replayed.agreements["camp-1"].proposer_guid, 2)

    def test_chat_burst_is_bounded_per_sender(self):
        messages = []
        for index in range(9):
            messages.append(ChatMessage(
                epoch=11, sequence=index + 1, sender_guid=2, receiver_guid=0,
                message=encode_proposal(
                    f"camp-{index}", "deadbeef", int(self.now) + 30,
                    "quest", ("Growth2", "Growth1"))))
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2),
            messages=tuple(messages))
        runtime = self.runtime()
        runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(len(runtime.agreements), 8)

    def test_roll_need_uses_equipment_score_not_quality(self):
        equipped = Item(guid=10, item_id=100, equipment_slot=0, equipped=True,
                        stats={"strength": 1}, template_known=True)
        template = Item(guid=0, item_id=200, equipment_slot=0, quality=0,
                        stats={"strength": 10}, template_known=True)
        self.adapter.inventory = InventoryState(
            items=(equipped,), item_templates=(template,))
        self.adapter.loot = LootState(rolls=(RollOffer(
            item_guid=500, slot=2, item_id=200, vote_mask=7,
            deadline_at=int((self.now + 60) * 1000)),))
        action = self.runtime().step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "roll")
        self.assertEqual(self.adapter.named("roll")[0]["vote"], 1)

    def test_roll_non_upgrade_greeds_and_unknown_near_deadline_passes(self):
        equipped = Item(guid=10, item_id=100, equipment_slot=0, equipped=True,
                        stats={"strength": 10}, template_known=True)
        template = Item(guid=0, item_id=200, equipment_slot=0, quality=6,
                        stats={"strength": 1}, template_known=True)
        offer = RollOffer(item_guid=500, slot=2, item_id=200, vote_mask=7,
                          deadline_at=int((self.now + 60) * 1000))
        self.adapter.inventory = InventoryState(
            items=(equipped,), item_templates=(template,))
        self.adapter.loot = LootState(rolls=(offer,))
        self.runtime().step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(self.adapter.named("roll")[0]["vote"], 2)
        self.store.mark_confirmed("roll:500:2")

        self.adapter.calls.clear()
        unknown_offer = replace(offer, item_guid=501, item_id=201)
        self.adapter.inventory = InventoryState(items=(equipped,))
        self.adapter.loot = LootState(rolls=(unknown_offer,))
        runtime = self.runtime()
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "query_roll_item")
        self.adapter.loot = LootState(rolls=(replace(
            unknown_offer, deadline_at=int((self.now + 1) * 1000)),))
        runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(self.adapter.named("roll")[0]["vote"], 0)

    def test_lost_roll_response_is_never_voted_twice(self):
        template = Item(guid=0, item_id=200, equipment_slot=0,
                        stats={"strength": 10}, template_known=True)
        offer = RollOffer(item_guid=500, slot=2, item_id=200, vote_mask=7,
                          deadline_at=int((self.now + 60) * 1000))
        self.adapter.inventory = InventoryState(item_templates=(template,))
        self.adapter.loot = LootState(rolls=(offer,))
        self.adapter.roll_error = True
        runtime = self.runtime()
        first = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertIn("unknown", first.detail)
        self.adapter.roll_error = False
        self.adapter.loot = LootState(rolls=(replace(offer, own_vote=1),))
        runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(len(self.adapter.named("roll")), 1)
        self.adapter.loot = LootState(rolls=(replace(
            offer, own_vote=1, vote_confirmed=True),))
        runtime.step(self.now + 2, make_view(), WARRIOR.class_mask)
        key = "roll:500:2"
        self.assertEqual(self.store.intent(key)["state"], "confirmed")

    def test_trade_accepts_only_the_exact_planned_offer(self):
        own = Item(guid=300, item_id=55, count=1, bag=255, slot=23)
        incoming = TradeItem(slot=0, item_id=77, count=1, random_property=4)
        trade = TradeState(
            trade_id=8, revision=3, active=True, trader_guid=2,
            own_items=(TradeItem(slot=0, item_id=55, count=1),),
            trader_items=(incoming,))
        self.adapter.inventory = InventoryState(
            items=(own,), money=1000, trade=trade)
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [own], expected_items=[incoming])
        action = runtime.step(self.now)
        self.assertEqual(action.action, "trade_accept")
        self.assertEqual(self.adapter.named("trade")[0]["action"], "accept")

        self.adapter.calls.clear()
        self.adapter.inventory = replace(self.adapter.inventory, trade=replace(
            trade, revision=4, trader_items=(replace(incoming, count=2),)))
        action = runtime.step(self.now + 1)
        self.assertEqual(action.action, "trade_cancel")
        self.assertFalse(self.adapter.named("trade")[0]["action"] == "accept")

    def test_lost_trade_accept_reconciles_without_redispatch(self):
        own = Item(guid=300, item_id=55, count=1, bag=255, slot=23)
        incoming = TradeItem(slot=0, item_id=77, count=1)
        trade = TradeState(
            trade_id=8, revision=3, active=True, trader_guid=2,
            own_items=(TradeItem(slot=0, item_id=55, count=1),),
            trader_items=(incoming,))
        self.adapter.inventory = InventoryState(
            items=(own,), money=1000, trade=trade)
        self.adapter.trade_error = True
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [own], expected_items=[incoming])
        first = runtime.step(self.now)
        self.assertIn("unknown", first.detail)
        self.adapter.trade_error = False
        runtime.step(self.now + 1)
        self.assertEqual(len(self.adapter.named("trade")), 1)

        received = Item(guid=500, item_id=77, count=1, bag=255, slot=24)
        self.adapter.inventory = InventoryState(
            items=(received,), money=1000, trade=TradeState())
        action = runtime.step(self.now + 2)
        self.assertEqual(action.action, "trade_complete_reconciled")
        accept_key = next(key for key in (
            row["intent_key"] for row in self.store.intents())
            if key.endswith(":accept"))
        self.assertEqual(self.store.intent(accept_key)["state"], "confirmed")
        self.assertEqual(len(self.adapter.named("trade")), 1)

    def test_cancelled_trade_with_transferred_inventory_still_reconciles(self):
        own = Item(guid=300, item_id=55, count=1, bag=255, slot=23)
        incoming = TradeItem(slot=0, item_id=77, count=1)
        trade = TradeState(
            trade_id=8, revision=3, active=True, trader_guid=2,
            own_items=(TradeItem(slot=0, item_id=55, count=1),),
            trader_items=(incoming,))
        self.adapter.inventory = InventoryState(
            items=(own,), money=1000, trade=trade)
        self.adapter.trade_error = True
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [own], expected_items=[incoming])
        runtime.step(self.now)
        self.adapter.trade_error = False
        received = Item(guid=500, item_id=77, count=1, bag=255, slot=24)
        self.adapter.inventory = InventoryState(
            items=(received,), money=1000,
            trade=TradeState(cancelled=True, trader_guid=2))
        action = runtime.step(self.now + 1)
        self.assertEqual(action.action, "trade_complete_reconciled")
        self.assertEqual(len(self.adapter.named("trade")), 1)

    def test_abandoned_trade_accept_never_starts_another_trade(self):
        own = Item(guid=300, item_id=55, count=1, bag=255, slot=23)
        trade = TradeState(
            trade_id=8, revision=3, active=True, trader_guid=2,
            own_items=(TradeItem(slot=0, item_id=55, count=1),))
        self.adapter.inventory = InventoryState(items=(own,), money=1000, trade=trade)
        self.adapter.trade_error = True
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [own])
        runtime.step(self.now)
        self.adapter.trade_error = False
        self.adapter.inventory = InventoryState(items=(own,), money=1000)
        self.now += 47
        action = runtime.step(self.now)
        self.assertEqual(action.detail, "abandoned")
        self.assertEqual(len(self.adapter.named("trade")), 1)
        action = runtime.step(self.now + 1)
        self.assertEqual(action.detail, "abandoned")
        self.assertEqual(len(self.adapter.named("trade")), 1)

    def test_trade_waits_for_a_partial_peer_offer(self):
        incoming = TradeItem(slot=0, item_id=77, count=1)
        trade = TradeState(trade_id=8, revision=3, active=True, trader_guid=2)
        self.adapter.inventory = InventoryState(money=1000, trade=trade)
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [], expected_items=[incoming])
        action = runtime.step(self.now)
        self.assertEqual(action.action, "trade_wait_peer_offer")
        self.assertFalse(self.adapter.named("trade"))
        self.adapter.inventory = replace(self.adapter.inventory, trade=replace(
            trade, trader_items=(incoming,)))
        action = runtime.step(self.now + 1)
        self.assertEqual(action.action, "trade_accept")
        self.assertEqual(self.adapter.named("trade")[0]["action"], "accept")

    def test_new_trade_plan_ignores_previous_terminal_state(self):
        self.adapter.inventory = InventoryState(
            money=1000, trade=TradeState(complete=True, trader_guid=2))
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.configure("Growth2", [])
        action = runtime.step(self.now)
        self.assertEqual(action.action, "trade_initiate")
        self.assertIsNotNone(runtime.plan)
        self.assertEqual(self.adapter.named("trade")[0]["action"], "initiate")

    def test_unplanned_pending_trade_is_cancelled(self):
        self.adapter.inventory = InventoryState(
            trade=TradeState(pending=True, trader_guid=99))
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        action = runtime.step(self.now)
        self.assertEqual(action.action, "trade_cancel")
        self.assertEqual(self.adapter.named("trade")[0]["action"], "cancel")

    def test_inactive_roll_without_server_vote_is_not_confirmed(self):
        offer = RollOffer(
            item_guid=500, slot=2, item_id=200, vote_mask=7,
            deadline_at=int((self.now + 60) * 1000))
        template = Item(guid=0, item_id=200, equipment_slot=0,
                        stats={"strength": 10}, template_known=True)
        self.adapter.inventory = InventoryState(item_templates=(template,))
        self.adapter.loot = LootState(rolls=(offer,))
        self.adapter.roll_error = True
        runtime = self.runtime()
        runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.adapter.roll_error = False
        self.adapter.loot = LootState(rolls=(replace(
            offer, active=False, own_vote=1, vote_confirmed=False),))
        action = runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "roll_resume")
        self.assertNotEqual(self.store.intent("roll:500:2")["state"], "confirmed")

    def test_lost_agreement_accept_is_not_marked_delivered_or_resent(self):
        proposal = encode_proposal(
            "camp-loss", "deadbeef", int(self.now) + 30, "grind",
            ("Growth2", "Growth1"))
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2),
            messages=(ChatMessage(epoch=11, sequence=1, sender_guid=2,
                                  receiver_guid=0, message=proposal),))
        self.adapter.chat_error = True
        runtime = self.runtime()
        runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(runtime.agreements["camp-loss"].accepted_guids, {2})
        self.adapter.chat_error = False
        runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(len(self.adapter.named("party_chat")), 1)
        self.assertEqual(runtime.agreements["camp-loss"].accepted_guids, {2})
        echoed = self.adapter.named("party_chat")[0]["message"]
        self.adapter.party = replace(self.adapter.party, messages=(
            self.adapter.party.messages + (ChatMessage(
                epoch=11, sequence=2, sender_guid=1, receiver_guid=0,
                message=echoed),)))
        runtime.step(self.now + 2, make_view(), WARRIOR.class_mask)
        self.assertEqual(runtime.agreements["camp-loss"].accepted_guids, {1, 2})
        self.assertEqual(len(self.adapter.named("party_chat")), 1)

    def test_runtime_invites_only_compatible_online_roster_peers(self):
        self.adapter.party = friendly_party(contacts=(
            Contact(guid=2, status=1, level=5),
            Contact(guid=3, status=0, level=5),
            Contact(guid=4, status=1, level=20),
            Contact(guid=5, status=1, level=5),
        ))
        runtime = self.runtime()
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "party_invite")
        self.assertEqual(self.adapter.named("invite"), [{"target": "Growth2"}])
        action = runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertIsNone(action)
        self.assertEqual(len(self.adapter.named("invite")), 1)

    def test_group_leader_proposes_temporary_grind(self):
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, leader_guid=1, members=members(1, 2))
        runtime = self.runtime()
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "party_chat")
        self.assertEqual(len(runtime.agreements), 1)
        agreement = next(iter(runtime.agreements.values()))
        self.assertEqual(agreement.activity, "grind")
        self.assertEqual(agreement.participants, ("Growth1", "Growth2"))

    def test_lost_cancel_response_is_confirmed_by_cancelled_state(self):
        self.adapter.inventory = InventoryState(
            trade=TradeState(pending=True, trader_guid=2))
        self.adapter.trade_error = True
        runtime = TradeRuntime(
            roster(), specs()[0], self.adapter, self.store, clock=lambda: self.now)
        runtime.step(self.now)
        self.adapter.trade_error = False
        self.adapter.inventory = InventoryState(
            trade=TradeState(cancelled=True, trader_guid=2))
        runtime.step(self.now + 1)
        key = next(row["intent_key"] for row in self.store.intents()
                   if row["kind"] == "trade_cancel")
        self.assertEqual(self.store.intent(key)["state"], "confirmed")

    def test_chat_replay_tolerates_removed_roster_participants(self):
        self.store.set_meta(AGREEMENTS_KEY, json.dumps([{
            "agreement_id": "old", "nonce": "deadbeef",
            "expires_at": int(self.now) + 30, "activity": "grind",
            "participants": ["Growth2", "Removed"], "proposer_guid": 2,
            "accepted_guids": [2],
        }]))
        self.store.commit_poll(epoch="1", events=[{
            "id": "party:11:1", "domain": "party.chat", "seq": 1,
            "payload": {
                "message": {"kind": "ACCEPT", "agreement_id": "old",
                            "nonce": "deadbeef"},
                "sender_guid": 2, "group_guids": [1, 2], "received_at": 0,
            },
        }], cursors={})
        runtime = self.runtime()
        self.assertEqual(runtime.agreements["old"].accepted_guids, {2})

    def test_roster_invite_is_not_accepted_before_contacts_are_ready(self):
        self.adapter.party = friendly_party(
            contacts=(), pending_invite=Invite(invite_id=10, inviter="Growth2"))
        action = self.runtime(
            invite_consent=lambda invite, view: True,
        ).step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "decline_invite")
        self.assertFalse(self.adapter.named("respond_invite")[0]["accept"])

    def test_agreement_timeout_leaves_group_and_returns_to_solo(self):
        proposal = encode_proposal(
            "short-1", "deadbeef", int(self.now) + 1, "grind",
            ("Growth2", "Growth1"))
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2),
            messages=(ChatMessage(epoch=11, sequence=1, sender_guid=2,
                                  receiver_guid=0, message=proposal),))
        runtime = self.runtime()
        runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.now += 2
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "leave_agreement_finished_group")
        self.assertTrue(self.adapter.named("leave_party"))

    def test_leader_sets_and_reads_back_group_loot_before_rolling(self):
        template = Item(guid=0, item_id=200, equipment_slot=0,
                        stats={"strength": 10}, template_known=True)
        self.adapter.inventory = InventoryState(item_templates=(template,))
        self.adapter.loot = LootState(rolls=(RollOffer(
            item_guid=500, slot=2, item_id=200, vote_mask=7,
            deadline_at=int((self.now + 60) * 1000)),))
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, leader_guid=1, loot_method=0,
            members=members(1, 2))
        runtime = self.runtime()
        action = runtime.step(self.now, make_view(), WARRIOR.class_mask)
        self.assertEqual(action.action, "set_group_loot")
        self.assertFalse(self.adapter.named("roll"))
        runtime.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(self.adapter.named("set_loot_method")[0]["method"], 3)
        self.assertEqual(self.adapter.named("roll")[0]["vote"], 1)

    def test_two_characters_negotiate_through_server_sender_events(self):
        second_store = CheckpointStore(Path(self.temp.name) / "second.db").open()
        self.addCleanup(second_store.close)
        second_adapter = FakeCooperationAdapter(self_guid=2)
        self.adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2))
        second_adapter.party = friendly_party(
            in_group=True, group_guid=20, members=members(1, 2),
            contacts=tuple(Contact(guid=guid) for guid in (1, 3, 4, 5)))
        first = self.runtime()
        second = CooperationRuntime(
            roster(), specs()[1], second_adapter, second_store, WARRIOR.inventory,
            clock=lambda: self.now)
        second._next_contacts_at = float("inf")

        first.propose("grind", ("Growth1", "Growth2"))
        proposal = second_adapter.party.messages  # no implicit shared transport
        self.assertFalse(proposal)
        outbound = self.adapter.party.messages[-1]
        second_adapter.party = replace(
            second_adapter.party,
            messages=(replace(outbound, epoch=11, sequence=1),))
        second.step(self.now, make_view(), WARRIOR.class_mask)
        response = second_adapter.party.messages[-1]
        self.assertIn("|ACCEPT|", response.message)
        self.assertEqual(response.sender_guid, 2)

        self.adapter.party = replace(
            self.adapter.party,
            messages=self.adapter.party.messages + (
                replace(response, epoch=11, sequence=2),))
        first.step(self.now + 1, make_view(), WARRIOR.class_mask)
        self.assertEqual(first.agreements[response.message.split("|")[2]].accepted_guids,
                         {1, 2})

    def test_friend_backfill_is_bounded_per_peer(self):
        self.adapter.party = friendly_party(contacts=())
        runtime = self.runtime()
        for _ in range(60):
            self.now += 31
            runtime.step(self.now, make_view(), WARRIOR.class_mask)
        counts = {name: 0 for name in ("Growth2", "Growth3", "Growth4", "Growth5")}
        for payload in self.adapter.named("add_friend"):
            counts[payload["name"]] += 1
        self.assertEqual(counts["Growth2"], 3)
        self.assertTrue(all(0 <= count <= 3 for count in counts.values()))
        self.assertTrue(runtime._initialization_failed)

    def test_protojson_normalizes_cooperation_uint64_and_offer_fields(self):
        party = normalize_party({
            "epoch": "11", "pendingInvite": {"inviteId": "7", "inviter": "Growth2"},
            "members": [{"guid": "2", "name": "Growth2"}],
            "messages": [{"epoch": "11", "sequence": "4", "senderGuid": "2",
                          "message": "AGW1"}],
        })
        self.assertEqual(party.epoch, 11)
        self.assertEqual(party.pending_invite.invite_id, 7)
        self.assertEqual(party.messages[0].sender_guid, 2)
        inventory = normalize_inventory({
            "itemTemplates": [{"itemId": "55", "templateKnown": True}],
            "trade": {"tradeId": "8", "traderGuid": "2", "revision": "3",
                      "ownItems": [{"slot": 0, "itemId": "55", "count": 1}]},
        })
        self.assertEqual(inventory.item_templates[0].item_id, 55)
        self.assertEqual(inventory.trade.trader_guid, 2)


if __name__ == "__main__":
    unittest.main()
