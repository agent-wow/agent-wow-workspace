"""Normalized cooperation facts and the deliberately small AGW1 chat grammar."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

AGW1_PREFIX = "AGW1"
AGREEMENT_ACTIVITIES = frozenset({"grind", "quest", "dungeon", "trade"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{8,32}$")


@dataclass(frozen=True)
class RuntimeAction:
    action: str
    detail: str = ""


@dataclass(frozen=True)
class Contact:
    guid: int
    flags: int = 0
    note: str = ""
    status: int = 0
    area: int = 0
    level: int = 0
    class_id: int = 0


@dataclass(frozen=True)
class Invite:
    invite_id: int
    inviter: str
    received_at: int = 0
    expires_at: int = 0


@dataclass(frozen=True)
class GroupMember:
    name: str
    guid: int
    status: int = 0
    subgroup: int = 0
    flags: int = 0
    roles: int = 0
    is_self: bool = False


@dataclass(frozen=True)
class ChatMessage:
    epoch: int
    sequence: int
    sender_guid: int
    receiver_guid: int
    message: str
    received_at: int = 0


@dataclass(frozen=True)
class PartyState:
    epoch: int = 0
    revision: int = 0
    contacts: tuple[Contact, ...] = ()
    who: tuple[dict, ...] = ()
    pending_invite: Invite | None = None
    in_group: bool = False
    group_guid: int = 0
    leader_guid: int = 0
    members: tuple[GroupMember, ...] = ()
    loot_method: int = 0
    master_looter_guid: int = 0
    loot_threshold: int = 0
    dungeon_difficulty: int = 0
    messages: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True)
class RollOffer:
    item_guid: int
    slot: int
    item_id: int
    map_id: int = 0
    random_suffix: int = 0
    random_property: int = 0
    count: int = 1
    countdown_ms: int = 0
    deadline_at: int = 0
    vote_mask: int = 7
    own_vote: int = -1
    vote_confirmed: bool = False
    active: bool = True
    outcome: str = ""
    winner_guid: int = 0


@dataclass(frozen=True)
class LootState:
    rolls: tuple[RollOffer, ...] = ()


@dataclass(frozen=True)
class TradeItem:
    slot: int
    item_id: int
    count: int
    random_property: int = 0


@dataclass(frozen=True)
class TradeState:
    trade_id: int = 0
    revision: int = 0
    pending: bool = False
    active: bool = False
    trader_guid: int = 0
    own_accepted: bool = False
    trader_accepted: bool = False
    complete: bool = False
    cancelled: bool = False
    last_status: int = 0
    close_result: int = 0
    own_items: tuple[TradeItem, ...] = ()
    trader_items: tuple[TradeItem, ...] = ()
    own_money: int = 0
    trader_money: int = 0


@dataclass(frozen=True)
class AgreementMessage:
    kind: str
    agreement_id: str
    nonce: str
    expires_at: int = 0
    activity: str = ""
    participants: tuple[str, ...] = ()


@dataclass
class Agreement:
    agreement_id: str
    nonce: str
    expires_at: int
    activity: str
    participants: tuple[str, ...]
    proposer_guid: int
    accepted_guids: set[int] = field(default_factory=set)
    declined_guids: set[int] = field(default_factory=set)
    cancelled: bool = False


def encode_proposal(
    agreement_id: str, nonce: str, expires_at: int, activity: str,
    participants: tuple[str, ...] | list[str],
) -> str:
    message = (f"AGW1|PROPOSE|{agreement_id}|{nonce}|{int(expires_at)}|"
               f"{activity}|{','.join(participants)}")
    # Reuse the parser as the outbound schema validator.
    parse_agreement_message(message)
    return message


def encode_agreement_response(kind: str, agreement_id: str, nonce: str) -> str:
    kind = kind.upper()
    if kind not in {"ACCEPT", "DECLINE", "CANCEL"}:
        raise ValueError("unsupported agreement response")
    message = f"AGW1|{kind}|{agreement_id}|{nonce}"
    parse_agreement_message(message)
    return message


def parse_agreement_message(text: str) -> AgreementMessage | None:
    if not isinstance(text, str) or len(text) > 512:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    parts = text.split("|")
    if len(parts) < 2 or parts[0] != AGW1_PREFIX:
        return None
    kind = parts[1]
    if kind == "PROPOSE":
        if len(parts) != 7:
            return None
        _, _, agreement_id, nonce, raw_expiry, activity, raw_participants = parts
        if activity not in AGREEMENT_ACTIVITIES or not raw_participants:
            return None
        participants = tuple(raw_participants.split(","))
        if any(not name or len(name) > 64 for name in participants):
            return None
    elif kind in {"ACCEPT", "DECLINE", "CANCEL"}:
        if len(parts) != 4:
            return None
        _, _, agreement_id, nonce = parts
        raw_expiry, activity, participants = "0", "", ()
    else:
        return None
    if not _ID_RE.fullmatch(agreement_id) or not _NONCE_RE.fullmatch(nonce):
        return None
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return None
    if expires_at < 0 or expires_at > 2**63 - 1:
        return None
    return AgreementMessage(
        kind=kind, agreement_id=agreement_id, nonce=nonce,
        expires_at=expires_at, activity=activity, participants=participants)
