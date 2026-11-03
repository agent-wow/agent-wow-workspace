"""Roster-only social consent, AGW1 agreements, group rolls and trade hooks."""
from __future__ import annotations

import json
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, replace
from typing import Callable

from .actions import DurableActionExecutor
from .adapter import AdapterError, WorldAdapter, WorldView
from .items import EquipmentPolicy, InventoryState
from .profiles import InventoryRules
from .roster import CharacterSpec, Roster
from .social import (
    Agreement, AgreementMessage, ChatMessage, Invite, PartyState, RollOffer,
    RuntimeAction, encode_agreement_response, encode_proposal,
    parse_agreement_message,
)
from .trade_runtime import TradeRuntime

AGREEMENTS_KEY = "cooperation.agreements"
FRIEND_ATTEMPTS_KEY = "cooperation.friend_attempts"
INVITE_ATTEMPTS_KEY = "cooperation.invite_attempts"
PARTY_EPOCH_KEY = "cooperation.party_epoch"
CHAT_DOMAIN = "party.chat"
MAX_CHAT_PER_WINDOW = 8
CHAT_WINDOW_SECONDS = 10.0
FRIEND_INITIALIZATION_SECONDS = 180.0
INVITE_COOLDOWN_SECONDS = 120.0
GROUP_LOOT_METHOD = 3
GROUP_LOOT_THRESHOLD = 2


def _reject_redispatch():
    raise AdapterError("an in-flight cooperation action cannot be redispatched")


class CooperationRuntime:
    def __init__(
        self, roster: Roster, spec: CharacterSpec, adapter: WorldAdapter, store,
        inventory_rules: InventoryRules, *, clock: Callable[[], float] = time.time,
        invite_consent: Callable[[Invite, WorldView], bool] | None = None,
        agreement_consent: Callable[[AgreementMessage, WorldView], bool] | None = None,
    ):
        self.roster = roster
        self.spec = spec
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.equipment = EquipmentPolicy(inventory_rules)
        self.actions = DurableActionExecutor(store, clock=clock, unknown_timeout=30.0)
        self.trade = TradeRuntime(roster, spec, adapter, store, clock=clock)
        self.invite_consent = invite_consent or (
            lambda invite, view: not view.dead and not view.in_combat)
        self.agreement_consent = agreement_consent or (
            lambda message, view: not view.dead and not view.in_combat)
        self._by_name = {row.name.casefold(): row for row in roster.characters}
        self._by_guid = {row.guid: row for row in roster.characters}
        self.agreements: dict[str, Agreement] = {}
        self._load_agreements()
        for event in store.events(CHAT_DOMAIN):
            self._reduce_chat(event.get("payload") or {})
        self._party_epoch = int(self.store.get_meta(PARTY_EPOCH_KEY) or 0)
        self._chat_times: dict[int, deque[float]] = defaultdict(deque)
        self._next_contacts_at = 0.0
        self._query_cooldown: dict[tuple[int, int], float] = {}
        self._friend_attempts = self._load_friend_attempts()
        self._invite_attempts = self._load_timestamps(INVITE_ATTEMPTS_KEY)
        self._started_at = self.clock()
        self._initialization_failed = False

    # --------------------------------------------------------------- public

    def step(self, now: float, view: WorldView, class_mask: int) -> RuntimeAction | None:
        try:
            party = self.adapter.get_party()
        except AdapterError:
            return None
        if party.in_group and self._group_has_outsider(party):
            return self._leave_group(party)
        friends_ready = self._friends_ready(now, party)
        if friends_ready:
            self._ingest_chat(now, view, party)
        self._prune_agreements(now)
        if party.in_group and self._has_finished_agreement(now):
            return self._leave_group(party, reason="agreement_finished")
        if party.pending_invite is not None:
            action = self._respond_invite(
                now, view, party.pending_invite, party, friends_ready=friends_ready)
            if action is not None:
                return action
        if not friends_ready:
            friend = self._ensure_friends(now, view, party)
            if friend is not None:
                return friend
            # An already configured trade may still need crash reconciliation;
            # no new invitation, agreement or roll starts before contacts are ready.
            if self.trade.plan is not None and not view.dead and not view.in_combat:
                return self.trade.step(now)
            return None
        response = self._respond_agreement(now, view, party)
        if response is not None:
            return response
        loot_settings = self._ensure_group_loot(party)
        if loot_settings is not None:
            return loot_settings
        roll = self._roll_step(now, view, class_mask, party)
        if roll is not None:
            return roll
        if view.dead or view.in_combat:
            return None
        trade = self.trade.step(now)
        if trade is not None:
            return trade
        proposal = self._maybe_propose_grind(now, party)
        if proposal is not None:
            return proposal
        return self._maybe_invite(now, view, party)

    def request_invite(self, peer: str) -> RuntimeAction:
        target = self._by_name.get(peer.casefold())
        if target is None or target.guid == self.spec.guid:
            raise ValueError("invite target must be another roster character")
        try:
            party = self.adapter.get_party()
        except AdapterError:
            return RuntimeAction("cooperation_unavailable")
        if not self._friends_ready(self.clock(), party):
            return RuntimeAction("cooperation_not_ready")
        key = f"party:invite:{self.spec.guid}:{target.guid}:{uuid.uuid4().hex}"
        result = self.actions.run(
            key, "party_invite", {"target": target.name},
            lambda: self.adapter.invite(target.name),
            lambda: self._party_readback(lambda party: (
                True if any(row.guid == target.guid for row in party.members) else None)))
        return RuntimeAction("party_invite", f"{target.name}: {result.state}")

    def propose(
        self, activity: str, participants: tuple[str, ...] | list[str], *,
        ttl: float = 30.0,
    ) -> RuntimeAction:
        if not 1.0 <= ttl <= 120.0:
            raise ValueError("agreement TTL must be 1..120 seconds")
        try:
            party = self.adapter.get_party()
        except AdapterError:
            return RuntimeAction("cooperation_unavailable")
        if not self._friends_ready(self.clock(), party):
            return RuntimeAction("cooperation_not_ready")
        if not party.in_group or self._group_has_outsider(party):
            return RuntimeAction("agreement_requires_roster_group")
        member_names = {row.name.casefold() for row in party.members}
        names = tuple(participants)
        if self.spec.name not in names:
            names = (self.spec.name,) + names
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("duplicate agreement participant")
        if any(name.casefold() not in self._by_name for name in names):
            raise ValueError("agreement participants must be roster characters")
        if any(name.casefold() not in member_names for name in names):
            raise ValueError("agreement participants must be current group members")
        now = self.clock()
        self._prune_agreements(now)
        if any(not self._agreement_finished(row, now) for row in self.agreements.values()):
            return RuntimeAction("agreement_busy")
        agreement = Agreement(
            agreement_id=uuid.uuid4().hex, nonce=secrets.token_hex(8),
            expires_at=int(now + ttl), activity=activity, participants=names,
            proposer_guid=self.spec.guid, accepted_guids={self.spec.guid},
        )
        if agreement.activity not in {"grind", "quest", "dungeon", "trade"}:
            raise ValueError("unsupported agreement activity")
        self.agreements[agreement.agreement_id] = agreement
        self._save_agreements()
        text = encode_proposal(
            agreement.agreement_id, agreement.nonce, agreement.expires_at,
            agreement.activity, agreement.participants)
        return self._send_chat(
            f"agreement:{agreement.agreement_id}:propose", text,
            lambda party: self._chat_echoed(party, text))

    def _maybe_invite(
        self, now: float, view: WorldView, party: PartyState,
    ) -> RuntimeAction | None:
        if (party.in_group or party.pending_invite is not None or self.trade.plan is not None
                or view.dead or view.in_combat or view.level <= 0):
            return None
        for state in ("sending", "sent", "unknown"):
            for intent in self.store.intents(state=state):
                if intent.get("kind") != "party_invite" or not isinstance(
                        intent.get("payload"), dict):
                    continue
                payload = intent["payload"]
                target = self._by_name.get(str(payload.get("target", "")).casefold())
                if target is None:
                    continue
                self.actions.run(
                    str(intent["intent_key"]), "party_invite", payload,
                    _reject_redispatch,
                    lambda target=target: self._party_readback(lambda current: (
                        True if any(row.guid == target.guid for row in current.members)
                        else None)))
                # Invitation ambiguity must not pause solo growth. The durable
                # intent is reconciled in the background and never redispatched.
                return None
        candidates = []
        for contact in party.contacts:
            peer = self._by_guid.get(contact.guid)
            if (peer is None or peer.guid == self.spec.guid or contact.status == 0
                    or contact.level <= 0 or abs(contact.level - view.level) > 2):
                continue
            if now - self._invite_attempts.get(peer.name, 0.0) < INVITE_COOLDOWN_SECONDS:
                continue
            candidates.append(peer)
        if not candidates:
            return None
        # One compatible online peer is enough for a temporary grind pair. A
        # cooldown prevents repeated invitations from becoming a fixed party.
        desired_role = {"tank": "healer", "healer": "tank"}.get(
            self.spec.role, "healer")
        peer = min(candidates, key=lambda row: (row.role != desired_role, row.guid))
        self._invite_attempts[peer.name] = now
        self.store.set_meta(INVITE_ATTEMPTS_KEY, json.dumps(
            self._invite_attempts, sort_keys=True))
        return self.request_invite(peer.name)

    def _maybe_propose_grind(self, now: float, party: PartyState) -> RuntimeAction | None:
        if (not party.in_group or party.leader_guid != self.spec.guid
                or len(party.members) < 2 or self.trade.plan is not None):
            return None
        self._prune_agreements(now)
        if any(not self._agreement_finished(row, now) for row in self.agreements.values()):
            return None
        return self.propose(
            "grind", tuple(row.name for row in party.members), ttl=60.0)

    # --------------------------------------------------------------- consent

    def _respond_invite(
        self, now: float, view: WorldView, invite: Invite, party: PartyState,
        *, friends_ready: bool,
    ) -> RuntimeAction | None:
        inviter = self._by_name.get(invite.inviter.casefold())
        accept = bool(
            inviter is not None and inviter.guid != self.spec.guid
            and friends_ready and not party.in_group
            and self.invite_consent(invite, view))
        key = f"party:invite_response:{invite.invite_id}:{accept}"
        payload = {"invite_id": invite.invite_id, "inviter": invite.inviter,
                   "accept": accept}
        def readback() -> bool | None:
            def predicate(current: PartyState):
                pending = current.pending_invite
                if accept:
                    if current.in_group and inviter is not None and any(
                            row.guid == inviter.guid for row in current.members):
                        return True
                    return None if pending is None or pending.invite_id == invite.invite_id else False
                return True if pending is None or pending.invite_id != invite.invite_id else None
            return self._party_readback(predicate)
        result = self.actions.run(
            key, "party_invite_response", payload,
            lambda: self.adapter.respond_invite(invite.invite_id, invite.inviter, accept),
            readback)
        return RuntimeAction("accept_invite" if accept else "decline_invite", result.state)

    def _leave_group(self, party: PartyState, *, reason: str = "non_roster") -> RuntimeAction:
        signature = ",".join(str(row.guid) for row in sorted(
            party.members, key=lambda row: row.guid))
        result = self.actions.run(
            f"party:leave:{reason}:{party.group_guid}:{signature}", "party_leave",
            {"group_guid": party.group_guid, "reason": reason},
            lambda: self.adapter.leave_party(),
            lambda: self._party_readback(lambda current: True if not current.in_group else None))
        return RuntimeAction(f"leave_{reason}_group", result.state)

    # --------------------------------------------------------------- contacts

    def _friends_ready(self, now: float, party: PartyState) -> bool:
        contacts = {row.guid for row in party.contacts}
        if all(peer.guid == self.spec.guid or peer.guid in contacts
               for peer in self.roster.characters):
            return not self._initialization_failed
        if now - self._started_at > FRIEND_INITIALIZATION_SECONDS:
            self._initialization_failed = True
        return False

    def _ensure_friends(
        self, now: float, view: WorldView, party: PartyState,
    ) -> RuntimeAction | None:
        if self._initialization_failed:
            return None
        if now >= self._next_contacts_at:
            self._next_contacts_at = now + 60.0
            try:
                party = self.adapter.request_contacts()
            except AdapterError:
                return None
            return RuntimeAction("request_contacts")
        contacts = {row.guid for row in party.contacts}
        for peer in self.roster.characters:
            if peer.guid == self.spec.guid or peer.guid in contacts:
                continue
            attempts = int(self._friend_attempts.get(peer.name, 0))
            if attempts >= 3:
                continue
            key = f"friend:{view.epoch}:{peer.guid}:{attempts}"
            existing = self.store.intent(key)
            if existing and existing["state"] in {"confirmed", "failed", "abandoned"}:
                self._friend_attempts[peer.name] = attempts + 1
                self._save_friend_attempts()
                continue
            result = self.actions.run(
                key, "friend_add", {"peer": peer.name, "guid": peer.guid},
                lambda peer=peer: self.adapter.add_friend(peer.name),
                lambda peer=peer: self._party_readback(lambda current: (
                    True if any(row.guid == peer.guid for row in current.contacts) else None)))
            return RuntimeAction("add_friend", f"{peer.name}: {result.state}")
        return None

    # -------------------------------------------------------------- agreements

    def _ingest_chat(self, now: float, view: WorldView, party: PartyState) -> None:
        if party.epoch == 0:
            return
        party_changed = party.epoch != self._party_epoch
        if party_changed:
            self._party_epoch = party.epoch
            self._chat_times.clear()
            self.store.set_meta(PARTY_EPOCH_KEY, str(party.epoch))
        cursor = 0 if party_changed else int(self.store.cursor(CHAT_DOMAIN)["cursor"])
        messages = sorted(
            (row for row in party.messages if row.epoch == party.epoch and row.sequence > cursor),
            key=lambda row: row.sequence)
        if not messages:
            return
        known = self.store.applied_event_ids()
        events = []
        reductions = []
        for message in messages[:64]:
            event_id = f"party:{message.epoch}:{message.sequence}"
            payload = self._chat_payload(now, party, message)
            if payload is None or event_id in known:
                continue
            events.append({"id": event_id, "domain": CHAT_DOMAIN,
                           "seq": message.sequence, "payload": payload})
            reductions.append(payload)
        next_cursor = max(row.sequence for row in messages)
        self.store.commit_poll(
            epoch=view.epoch or "", events=events,
            cursors={CHAT_DOMAIN: next_cursor})
        for payload in reductions:
            self._reduce_chat(payload)
        if reductions:
            self._save_agreements()

    def _chat_payload(self, now: float, party: PartyState, message: ChatMessage) -> dict | None:
        if not party.in_group or self._group_has_outsider(party):
            return None
        sender = self._by_guid.get(message.sender_guid)
        if sender is None or sender.guid == self.spec.guid:
            return None
        times = self._chat_times[sender.guid]
        while times and now - times[0] >= CHAT_WINDOW_SECONDS:
            times.popleft()
        if len(times) >= MAX_CHAT_PER_WINDOW:
            return None
        times.append(now)
        parsed = parse_agreement_message(message.message)
        if parsed is None:
            return None
        return {
            "message": asdict(parsed), "sender_guid": sender.guid,
            "group_guids": [row.guid for row in party.members],
            "received_at": message.received_at,
        }

    def _reduce_chat(self, payload: dict) -> None:
        try:
            raw = payload["message"]
            message = AgreementMessage(
                kind=str(raw["kind"]), agreement_id=str(raw["agreement_id"]),
                nonce=str(raw["nonce"]), expires_at=int(raw.get("expires_at", 0)),
                activity=str(raw.get("activity", "")),
                participants=tuple(raw.get("participants") or ()),
            )
            sender_guid = int(payload["sender_guid"])
            group_guids = {int(value) for value in payload["group_guids"]}
        except (KeyError, TypeError, ValueError):
            return
        if parse_agreement_message(self._reencode(message)) is None:
            return
        sender = self._by_guid.get(sender_guid)
        if sender is None or sender_guid not in group_guids:
            return
        agreement = self.agreements.get(message.agreement_id)
        if message.kind == "PROPOSE":
            if agreement is not None or not self._valid_proposal(message, sender_guid, group_guids):
                return
            self.agreements[message.agreement_id] = Agreement(
                agreement_id=message.agreement_id, nonce=message.nonce,
                expires_at=message.expires_at, activity=message.activity,
                participants=message.participants, proposer_guid=sender_guid,
                accepted_guids={sender_guid},
            )
            return
        if agreement is None or agreement.nonce != message.nonce:
            return
        participant_guids = {
            spec.guid for name in agreement.participants
            if (spec := self._by_name.get(name.casefold())) is not None}
        if (len(participant_guids) != len(agreement.participants)
                or sender_guid not in participant_guids):
            return
        if message.kind == "ACCEPT":
            agreement.accepted_guids.add(sender_guid)
        elif message.kind == "DECLINE":
            agreement.declined_guids.add(sender_guid)
        elif message.kind == "CANCEL" and sender_guid == agreement.proposer_guid:
            agreement.cancelled = True

    def _valid_proposal(
        self, message: AgreementMessage, sender_guid: int, group_guids: set[int],
    ) -> bool:
        if message.expires_at <= int(self.clock()) or message.expires_at > int(self.clock()) + 120:
            return False
        if len(message.participants) < 2 or len(message.participants) > len(self.roster.characters):
            return False
        specs = [self._by_name.get(name.casefold()) for name in message.participants]
        if any(row is None for row in specs):
            return False
        if len({row.guid for row in specs}) != len(specs):
            return False
        if self.spec.guid not in {row.guid for row in specs}:
            return False
        sender = self._by_guid.get(sender_guid)
        return bool(sender and sender.name.casefold() in
                    {name.casefold() for name in message.participants}
                    and {row.guid for row in specs}.issubset(group_guids))

    def _respond_agreement(
        self, now: float, view: WorldView, party: PartyState,
    ) -> RuntimeAction | None:
        if not party.in_group:
            return None
        for agreement in sorted(self.agreements.values(), key=lambda row: row.agreement_id):
            if (agreement.proposer_guid == self.spec.guid
                    or self._agreement_finished(agreement, now)
                    or self.spec.guid in agreement.accepted_guids
                    or self.spec.guid in agreement.declined_guids):
                continue
            message = AgreementMessage(
                kind="PROPOSE", agreement_id=agreement.agreement_id,
                nonce=agreement.nonce, expires_at=agreement.expires_at,
                activity=agreement.activity, participants=agreement.participants)
            accept_key = f"agreement:{agreement.agreement_id}:accept:{self.spec.guid}"
            decline_key = f"agreement:{agreement.agreement_id}:decline:{self.spec.guid}"
            if self.store.intent(accept_key) is not None:
                accept = True
            elif self.store.intent(decline_key) is not None:
                accept = False
            else:
                accept = self.agreement_consent(message, view)
            kind = "ACCEPT" if accept else "DECLINE"
            key = accept_key if accept else decline_key
            text = encode_agreement_response(kind, agreement.agreement_id, agreement.nonce)
            result = self._send_chat(
                key, text, lambda current, text=text: self._chat_echoed(current, text))
            if (self.store.intent(key) or {}).get("state") == "confirmed":
                # Only a server echo proves that the response was delivered.
                # Unknown/failed responses keep their original durable key and
                # are reconciled without selecting a different response.
                if accept:
                    agreement.accepted_guids.add(self.spec.guid)
                else:
                    agreement.declined_guids.add(self.spec.guid)
                self._save_agreements()
            return result
        return None

    def _send_chat(self, key: str, text: str, readback) -> RuntimeAction:
        result = self.actions.run(
            key, "party_chat", {"message": text},
            lambda: self.adapter.send_party_chat(text),
            lambda: self._party_readback(readback))
        return RuntimeAction("party_chat", f"{key}: {result.state}")

    @staticmethod
    def _chat_echoed(party: PartyState, text: str) -> bool | None:
        return True if any(row.message == text for row in party.messages) else None

    # ------------------------------------------------------------------ rolls

    def _ensure_group_loot(self, party: PartyState) -> RuntimeAction | None:
        if not party.in_group or party.loot_method == GROUP_LOOT_METHOD:
            return None
        if party.leader_guid != self.spec.guid:
            return None
        key = (f"party:loot_method:{party.group_guid}:{party.loot_method}:"
               f"{GROUP_LOOT_METHOD}:{GROUP_LOOT_THRESHOLD}")
        result = self.actions.run(
            key, "party_loot_method",
            {"group_guid": party.group_guid, "method": GROUP_LOOT_METHOD,
             "threshold": GROUP_LOOT_THRESHOLD},
            lambda: self.adapter.set_loot_method(
                GROUP_LOOT_METHOD, 0, GROUP_LOOT_THRESHOLD),
            lambda: self._party_readback(lambda current: (
                True if current.group_guid == party.group_guid
                and current.loot_method == GROUP_LOOT_METHOD
                and current.loot_threshold == GROUP_LOOT_THRESHOLD else None)))
        return RuntimeAction("set_group_loot", result.state)

    def _roll_step(
        self, now: float, view: WorldView, class_mask: int, party: PartyState,
    ) -> RuntimeAction | None:
        if not party.in_group or party.loot_method != GROUP_LOOT_METHOD:
            return None
        try:
            loot = self.adapter.get_loot_state()
        except AdapterError:
            return None
        resumed = self._resume_roll()
        if resumed is not None:
            return resumed
        offer = next((row for row in sorted(
            loot.rolls, key=lambda row: row.deadline_at)
            if row.active and row.own_vote < 0), None)
        if offer is None:
            return None
        try:
            inventory = self.adapter.get_inventory()
        except AdapterError:
            return None
        template = next((row for row in inventory.item_templates
                         if row.item_id == offer.item_id and row.template_known), None)
        deadline_seconds = offer.deadline_at / 1000.0
        if template is None and now + 2.0 < deadline_seconds:
            key = (offer.item_guid, offer.slot)
            if self._query_cooldown.get(key, 0.0) <= now:
                self._query_cooldown[key] = now + 5.0
                try:
                    self.adapter.query_item(offer.item_id)
                except AdapterError:
                    return None
                return RuntimeAction("query_roll_item", str(offer.item_id))
            return None
        vote = 0
        if template is not None:
            candidate = replace(template, guid=offer.item_guid, count=offer.count)
            upgrades = self.equipment.upgrades(
                InventoryState(items=inventory.items + (candidate,)),
                level=view.level, class_mask=class_mask,
                skills=view.self_state.get("skills") or {})
            if any(row.item.guid == offer.item_guid for row in upgrades) and offer.vote_mask & 1:
                vote = 1
            elif offer.vote_mask & 2:
                vote = 2
        key = f"roll:{offer.item_guid}:{offer.slot}"
        result = self.actions.run(
            key, "loot_roll", {"item_guid": offer.item_guid, "slot": offer.slot, "vote": vote},
            lambda: self.adapter.roll(offer.item_guid, offer.slot, vote),
            lambda: self._roll_readback(offer))
        return RuntimeAction("roll", f"{vote}: {result.state}")

    def _resume_roll(self) -> RuntimeAction | None:
        for state in ("sending", "sent", "unknown"):
            for intent in self.store.intents(state=state):
                if intent.get("kind") != "loot_roll" or not isinstance(
                        intent.get("payload"), dict):
                    continue
                payload = intent["payload"]
                offer = RollOffer(
                    item_guid=int(payload["item_guid"]), slot=int(payload["slot"]),
                    item_id=int(payload.get("item_id", 0)))
                result = self.actions.run(
                    str(intent["intent_key"]), "loot_roll", payload,
                    _reject_redispatch, lambda offer=offer: self._roll_readback(offer))
                if result.state != "confirmed":
                    return RuntimeAction("roll_resume", result.state)
        return None

    def _roll_readback(self, offer: RollOffer) -> bool | None:
        try:
            current = next((row for row in self.adapter.get_loot_state().rolls
                            if row.item_guid == offer.item_guid and row.slot == offer.slot), None)
        except AdapterError:
            return None
        if current is None:
            return None
        return True if current.vote_confirmed else None

    # --------------------------------------------------------------- helpers

    def _party_readback(self, predicate) -> bool | None:
        try:
            return predicate(self.adapter.get_party())
        except AdapterError:
            return None

    def _group_has_outsider(self, party: PartyState) -> bool:
        return any(row.guid not in self._by_guid for row in party.members)

    def _has_finished_agreement(self, now: float) -> bool:
        for row in self.agreements.values():
            participants = {name.casefold() for name in row.participants}
            if (self.spec.name.casefold() in participants
                    and self._agreement_finished(row, now)):
                return True
        return False

    @staticmethod
    def _agreement_finished(agreement: Agreement, now: float) -> bool:
        return agreement.cancelled or bool(agreement.declined_guids) or now >= agreement.expires_at

    def _prune_agreements(self, now: float) -> None:
        old = [key for key, row in self.agreements.items()
               if self._agreement_finished(row, now) and now >= row.expires_at + 300]
        for key in old:
            del self.agreements[key]
        if old:
            self._save_agreements()

    @staticmethod
    def _reencode(message: AgreementMessage) -> str:
        if message.kind == "PROPOSE":
            return encode_proposal(
                message.agreement_id, message.nonce, message.expires_at,
                message.activity, message.participants)
        return encode_agreement_response(
            message.kind, message.agreement_id, message.nonce)

    def _load_agreements(self) -> None:
        raw = self.store.get_meta(AGREEMENTS_KEY)
        if not raw:
            return
        try:
            rows = json.loads(raw)
            for row in rows:
                agreement = Agreement(
                    agreement_id=row["agreement_id"], nonce=row["nonce"],
                    expires_at=int(row["expires_at"]), activity=row["activity"],
                    participants=tuple(row["participants"]),
                    proposer_guid=int(row["proposer_guid"]),
                    accepted_guids={int(v) for v in row.get("accepted_guids", [])},
                    declined_guids={int(v) for v in row.get("declined_guids", [])},
                    cancelled=bool(row.get("cancelled", False)),
                )
                self.agreements[agreement.agreement_id] = agreement
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.agreements = {}

    def _save_agreements(self) -> None:
        rows = []
        for row in sorted(self.agreements.values(), key=lambda value: value.agreement_id):
            rows.append({
                "agreement_id": row.agreement_id, "nonce": row.nonce,
                "expires_at": row.expires_at, "activity": row.activity,
                "participants": list(row.participants),
                "proposer_guid": row.proposer_guid,
                "accepted_guids": sorted(row.accepted_guids),
                "declined_guids": sorted(row.declined_guids),
                "cancelled": row.cancelled,
            })
        self.store.set_meta(AGREEMENTS_KEY, json.dumps(rows, sort_keys=True))

    def _load_friend_attempts(self) -> dict[str, int]:
        try:
            return {str(key): int(value) for key, value in
                    json.loads(self.store.get_meta(FRIEND_ATTEMPTS_KEY) or "{}").items()}
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return {}

    def _load_timestamps(self, key: str) -> dict[str, float]:
        try:
            return {str(name): float(value) for name, value in
                    json.loads(self.store.get_meta(key) or "{}").items()}
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return {}

    def _save_friend_attempts(self) -> None:
        self.store.set_meta(FRIEND_ATTEMPTS_KEY, json.dumps(
            self._friend_attempts, sort_keys=True))
