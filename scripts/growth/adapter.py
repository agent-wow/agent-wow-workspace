"""Protocol adapter boundary between the growth state machine and worldrpc.

The growth worker talks to the local worldrpc endpoint through ``rpc.call``.
Module field names are still being extended by a parallel protocol unit, so
this module is the *only* place that knows their wire shape. It accepts both
the existing snake_case and the newer lowerCamelCase protojson spellings,
never invents a ``ready`` flag, and exposes a small client surface that tests
can replace with an explicit fake.

Facts that remain unverified live (generic ``power``, ``session_id``) are
read opportunistically. Their absence yields ``None``/not-ready instead of a
fabricated default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .items import InventoryState, Item
from .rpc import RPCError, call

LIVE_KEYS = (
    "guid", "name", "level", "health", "max_health", "xp", "next_level_xp",
    "dead", "in_combat", "faction", "flags", "player_flags", "has_position",
    "type", "class_id", "race_id", "money",
)
EPOCH_KEYS = ("epoch", "session_epoch", "session_id", "sessionEpoch", "sessionId")
GHOST_FLAG = 0x10


class AdapterError(RuntimeError):
    """A world read or action could not be completed."""


class Transport(Protocol):
    def call(self, method: str, params: dict | None = None, *,
             timeout: float = 10.0) -> dict: ...


class RPCCallTransport:
    """Production transport: direct localhost worldrpc, no account files."""

    def __init__(self, port: int):
        self.port = int(port)

    def call(self, method: str, params: dict | None = None, *,
             timeout: float = 10.0) -> dict:
        return call(self.port, method, params, timeout=timeout)


@dataclass(frozen=True)
class WorldView:
    """One consistent-enough read of the character's live facts."""

    ready: bool = False
    epoch: str | None = None
    map_id: int | None = None
    self_state: dict = field(default_factory=dict)
    position: dict = field(default_factory=dict)
    nearby: tuple[dict, ...] = ()
    target: dict | None = None
    combat: dict = field(default_factory=dict)

    # Convenience accessors; all values originate in the snapshot.
    @property
    def guid(self) -> int | None:
        return _opt_int(self.self_state.get("guid"))

    @property
    def name(self) -> str:
        return str(self.self_state.get("name") or "")

    @property
    def level(self) -> int:
        return int(self.self_state.get("level") or 0)

    @property
    def health(self) -> int:
        return int(self.self_state.get("health") or 0)

    @property
    def max_health(self) -> int:
        return int(self.self_state.get("max_health") or 0)

    @property
    def xp(self) -> int:
        return int(self.self_state.get("xp") or 0)

    @property
    def next_level_xp(self) -> int:
        return int(self.self_state.get("next_level_xp") or 0)

    @property
    def dead(self) -> bool:
        return bool(self.self_state.get("dead"))

    @property
    def ghost(self) -> bool:
        return bool(self.self_state.get("ghost"))

    @property
    def in_combat(self) -> bool:
        return bool(self.self_state.get("in_combat"))

    @property
    def orientation(self) -> float:
        value = self.position.get("orientation", self.self_state.get("orientation"))
        return float(value or 0.0)

    @property
    def target_guid(self) -> int | None:
        if self.target:
            guid = _opt_int(self.target.get("guid"))
            if guid is not None:
                return guid
        return _opt_int(self.combat.get("target_guid"))

    def power(self, resource: str) -> int | None:
        powers = self.self_state.get("powers") or {}
        for key in (resource, f"self_{resource}", f"{resource}_power", "power",
                    "self_power"):
            value = powers.get(key)
            if value is not None:
                return _opt_int(value)
        return None


class WorldAdapter:
    """Read facts and issue bounded actions through a transport."""

    def __init__(self, transport: Transport):
        self.transport = transport

    # --------------------------------------------------------------- reads

    def _call(self, method: str, params: dict | None = None, *,
              timeout: float = 10.0) -> dict:
        try:
            result = self.transport.call(method, params, timeout=timeout)
        except AdapterError:
            raise
        except RPCError as exc:
            raise AdapterError(str(exc)) from exc
        if not isinstance(result, dict):
            raise AdapterError(f"{method} returned a non-object result")
        return result

    def get_self(self) -> dict:
        return normalize_self(self._call("sense.get_self"))

    def get_position(self) -> dict:
        raw = self._call("move.get_position")
        if isinstance(raw.get("position"), dict):
            return _normalize_position(raw["position"])
        return _normalize_position(raw)

    def get_nearby(self, *, radius: float = 0.0, limit: int = 200) -> list[dict]:
        raw = self._call("sense.get_nearby", {"radius": radius, "limit": limit})
        units = raw.get("units") or []
        if not isinstance(units, list):
            raise AdapterError("sense.get_nearby returned a non-list units field")
        return [normalize_unit(unit) for unit in units if isinstance(unit, dict)]

    def get_combat(self) -> dict:
        return normalize_combat(self._call("combat.get_state"))

    def get_target(self) -> dict | None:
        try:
            raw = self._call("sense.get_target", timeout=5.0)
        except AdapterError:
            # ``sense.get_target`` predates the combat read-back; combat state
            # remains an acceptable fallback and is reconciled by the caller.
            return None
        if not raw.get("has_target") and not raw.get("hasTarget"):
            return None
        unit = raw.get("unit")
        if isinstance(unit, dict):
            normalized = normalize_unit(unit)
            if normalized.get("guid") is None:
                normalized["guid"] = _opt_int(
                    _pick(raw, "target_guid", "targetGuid"))
            return normalized
        guid = _opt_int(_pick(raw, "target_guid", "targetGuid"))
        return {"guid": guid} if guid is not None else None

    def read_view(self) -> WorldView:
        snapshot = self.get_self()
        epoch = snapshot.get("epoch")
        map_id = snapshot.get("map_id")
        if not snapshot.get("ready"):
            # Do not read or retain stale position/nearby facts before ready.
            return WorldView(ready=False, epoch=epoch, map_id=map_id)

        position = self.get_position()
        nearby = self.get_nearby()
        combat = self.get_combat()
        target = self.get_target()

        self_state = dict(snapshot)
        # Sense owns the accumulated server-derived self snapshot. Combat may
        # still be between world-reset and its first update while sense is
        # ready, so its zero/stale health, level, XP and death fields must not
        # overwrite the fresh facts. It only supplements power and combat
        # counters/target state here.
        powers = dict(snapshot.get("powers") or {})
        powers.update(combat.get("powers") or {})
        self_state["powers"] = powers
        if not position:
            position = dict(snapshot.get("position") or {})
        if target is None:
            target_guid = _opt_int(combat.get("target_guid"))
            target = {"guid": target_guid} if target_guid is not None else None

        return WorldView(
            ready=True,
            epoch=epoch,
            map_id=map_id,
            self_state=self_state,
            position=position,
            nearby=tuple(nearby),
            target=target,
            combat=combat,
        )

    # ------------------------------------------------------------- actions

    def attack(self, guid: int) -> dict:
        return normalize_combat(self._call(
            "combat.attack", {"guid": str(int(guid))}, timeout=20.0))

    def stop_attack(self) -> dict:
        return normalize_combat(self._call("combat.stop_attack", {}, timeout=15.0))

    def cast(self, spell_id: int, target_guid: int) -> dict:
        return normalize_combat(self._call(
            "combat.cast",
            {"spell_id": int(spell_id), "targetGuid": str(int(target_guid))},
            timeout=15.0))

    def move_to(self, x: float, y: float, z: float) -> dict:
        raw = self._call(
            "move.move_to", {"x": float(x), "y": float(y), "z": float(z)},
            timeout=45.0)
        if isinstance(raw.get("position"), dict):
            return {"position": _normalize_position(raw["position"])}
        return {"position": _normalize_position(raw)}

    def turn_to(self, orientation: float) -> dict:
        return self._call(
            "move.turn_to", {"orientation": float(orientation)}, timeout=15.0)

    def loot(self, guid: int) -> dict:
        return self._call("loot.loot", {"guid": str(int(guid))}, timeout=20.0)

    def get_inventory(self) -> InventoryState:
        return normalize_inventory(self._call("inventory.get_state"))

    def list_inventory(self, vendor_guid: int) -> InventoryState:
        return normalize_inventory(self._call(
            "inventory.list_inventory", {"vendorGuid": str(int(vendor_guid))},
            timeout=15.0))

    def buy_item(self, vendor_guid: int, item_id: int, slot: int,
                 count: int) -> InventoryState:
        return normalize_inventory(self._call(
            "inventory.buy", {
                "vendorGuid": str(int(vendor_guid)), "itemId": int(item_id),
                "slot": int(slot), "count": int(count),
            }, timeout=15.0))

    def sell_item(self, vendor_guid: int, item_guid: int,
                  count: int = 0) -> InventoryState:
        return normalize_inventory(self._call(
            "inventory.sell", {
                "vendorGuid": str(int(vendor_guid)),
                "itemGuid": str(int(item_guid)), "count": int(count),
            }, timeout=15.0))

    def repair_item(self, vendor_guid: int, item_guid: int = 0) -> InventoryState:
        return normalize_inventory(self._call(
            "inventory.repair", {
                "vendorGuid": str(int(vendor_guid)),
                "itemGuid": str(int(item_guid)), "useGuildBank": False,
            }, timeout=15.0))

    def equip_item(self, item_guid: int, destination_slot: int) -> InventoryState:
        return normalize_inventory(self._call(
            "inventory.equip", {
                "itemGuid": str(int(item_guid)),
                "destinationSlot": int(destination_slot),
            }, timeout=15.0))

    def release_spirit(self) -> dict:
        return normalize_combat(self._call(
            "combat.release_spirit", {}, timeout=20.0))

    def reclaim_corpse(self) -> dict:
        return normalize_combat(self._call(
            "combat.reclaim_corpse", {}, timeout=20.0))

    def resurrect_at_spirit_healer(self, healer_guid: int) -> dict:
        return normalize_combat(self._call(
            "combat.resurrect_at_spirit_healer",
            {"healerGuid": str(int(healer_guid))}, timeout=25.0))


# ------------------------------------------------------------- normalization

def _pick(mapping: dict, *names):
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _opt_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int(value, default: int = 0) -> int:
    number = _opt_int(value)
    return default if number is None else number


def _as_float(value, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _bool_opt(value) -> bool | None:
    if value is None:
        return None
    return _as_bool(value, False)


def _normalize_position(raw: dict) -> dict:
    return {
        "x": _as_float(_pick(raw, "x")),
        "y": _as_float(_pick(raw, "y")),
        "z": _as_float(_pick(raw, "z")),
        "orientation": _as_float(_pick(raw, "orientation", "o")),
    }


def _normalize_powers(raw: dict) -> dict:
    powers = {}
    legacy = _pick(raw, "rage", "self_rage", "selfRage")
    generic = _pick(raw, "power", "self_power", "selfPower")
    # Existing modules expose only UNIT_FIELD_POWER1 under ``selfRage``. That
    # slot is the class's primary resource (rage/mana/energy), so it is the
    # legacy spelling of generic power until the parallel protocol unit adds
    # the explicit field.
    if generic is not None:
        powers["power"] = _as_int(generic)
    elif legacy is not None:
        powers["power"] = _as_int(legacy)
    for key, names in (
        ("rage", ("rage", "self_rage", "selfRage")),
        ("mana", ("mana", "self_mana", "selfMana")),
        ("energy", ("energy", "self_energy", "selfEnergy")),
    ):
        value = _pick(raw, *names)
        if value is not None:
            powers[key] = _as_int(value)
    return powers


def normalize_unit(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    position = _pick(raw, "position", "pos") or {}
    if not isinstance(position, dict):
        position = {}
    normalized_position = _normalize_position(position)
    has_position = _bool_opt(_pick(raw, "has_position", "hasPosition"))
    if has_position is None and "x" in position:
        has_position = True
    normalized = {
        "guid": _opt_int(_pick(raw, "guid", "GUID")),
        "name": str(_pick(raw, "name") or ""),
        "type": _as_int(_pick(raw, "type"), 0),
        "level": _as_int(_pick(raw, "level"), 0),
        "health": _as_int(_pick(raw, "health"), 0),
        "max_health": _as_int(_pick(raw, "max_health", "maxHealth"), 0),
        "xp": _as_int(_pick(raw, "xp"), 0),
        "next_level_xp": _as_int(
            _pick(raw, "next_level_xp", "nextLevelXp"), 0),
        "dead": _as_bool(_pick(raw, "dead"), False),
        "in_combat": _as_bool(_pick(raw, "in_combat", "inCombat"), False),
        "faction": _as_int(_pick(raw, "faction"), 0),
        "flags": _as_int(_pick(raw, "flags"), 0),
        "player_flags": _as_int(_pick(raw, "player_flags", "playerFlags"), 0),
        "has_position": bool(has_position),
        "position": normalized_position,
        "orientation": _as_float(
            _pick(raw, "orientation"), normalized_position["orientation"]),
        "class_id": _as_int(_pick(raw, "class_id", "classId"), 0),
        "race_id": _as_int(_pick(raw, "race_id", "raceId"), 0),
        "money": _as_int(_pick(raw, "money"), 0),
        "npc_flags": _as_int(_pick(raw, "npc_flags", "npcFlags"), 0),
        # Optional fact from the parallel protocol unit; absent means the
        # worker falls back to the spirit-healer path.
        "corpse_reclaimable": _as_bool(
            _pick(raw, "corpse_reclaimable", "corpseReclaimable"), False),
    }
    normalized["ghost"] = bool(normalized["player_flags"] & GHOST_FLAG)
    normalized["powers"] = _normalize_powers(raw)
    return normalized


def normalize_self(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise AdapterError("sense.get_self returned a non-object")
    unit_raw = snapshot.get("unit")
    if not isinstance(unit_raw, dict):
        unit_raw = snapshot.get("self") if isinstance(snapshot.get("self"), dict) else {}
    normalized = normalize_unit(unit_raw)
    if unit_raw is not snapshot:
        # Some extended shapes repeat unit fields at the top level; keep the
        # unit object authoritative but fill gaps from the snapshot.
        top = normalize_unit(snapshot)
        for key in LIVE_KEYS:
            if not normalized.get(key) and top.get(key):
                normalized[key] = top[key]

    # Readiness is a server-derived cold-start gate. Legacy in_world/position
    # fields do not prove that the initial self update is complete.
    ready = _bool_opt(snapshot.get("ready")) is True

    epoch = _pick(snapshot, *EPOCH_KEYS)
    if epoch is None:
        epoch = _pick(unit_raw, *EPOCH_KEYS)
    map_id = _opt_int(_pick(snapshot, "map_id", "mapId"))
    if map_id is None:
        map_id = _opt_int(_pick(unit_raw, "map_id", "mapId"))

    normalized["ready"] = bool(ready)
    normalized["epoch"] = str(epoch) if epoch is not None else None
    normalized["map_id"] = map_id
    skills = {}
    raw_skills = snapshot.get("skills") or []
    if isinstance(raw_skills, list):
        for raw_skill in raw_skills:
            if not isinstance(raw_skill, dict):
                continue
            skill_id = _opt_int(_pick(raw_skill, "id", "skill_id", "skillId"))
            if skill_id is not None:
                skills[str(skill_id)] = _as_int(
                    _pick(raw_skill, "value", "rank", "current"), 0)
    normalized["skills"] = skills
    return normalized


def normalize_item(raw: dict) -> Item:
    equipment_slot = _opt_int(_pick(raw, "equipment_slot", "equipmentSlot"))
    if equipment_slot is not None and equipment_slot < 0:
        equipment_slot = None
    stats = _pick(raw, "stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    return Item(
        guid=_as_int(_pick(raw, "guid"), 0),
        item_id=_as_int(_pick(raw, "item_id", "itemId"), 0),
        name=str(_pick(raw, "name") or ""),
        quality=_as_int(_pick(raw, "quality"), 0),
        class_id=_as_int(_pick(raw, "class_id", "classId"), 0),
        subclass=_as_int(_pick(raw, "subclass"), 0),
        inventory_type=_as_int(_pick(raw, "inventory_type", "inventoryType"), 0),
        equipment_slot=equipment_slot,
        required_level=_as_int(_pick(raw, "required_level", "requiredLevel"), 0),
        required_class_mask=_as_int(
            _pick(raw, "required_class_mask", "requiredClassMask"), 0),
        required_skill=_as_int(_pick(raw, "required_skill", "requiredSkill"), 0),
        required_skill_rank=_as_int(
            _pick(raw, "required_skill_rank", "requiredSkillRank"), 0),
        stats={str(name): _as_int(value) for name, value in stats.items()},
        armor=_as_int(_pick(raw, "armor"), 0),
        dps=_as_float(_pick(raw, "dps"), 0.0),
        sell_price=_as_int(_pick(raw, "sell_price", "sellPrice"), 0),
        buy_price=_as_int(_pick(raw, "buy_price", "buyPrice"), 0),
        count=_as_int(_pick(raw, "count"), 1),
        bag=_as_int(_pick(raw, "bag"), 0),
        slot=_as_int(_pick(raw, "slot"), 0),
        container_slots=_as_int(_pick(raw, "container_slots", "containerSlots"), 0),
        equipped=_as_bool(_pick(raw, "equipped"), False),
        quest_item=_as_bool(_pick(raw, "quest_item", "questItem"), False),
        stock=_as_int(_pick(raw, "stock"), -1),
        durability=_as_int(_pick(raw, "durability"), 0),
        max_durability=_as_int(_pick(raw, "max_durability", "maxDurability"), 0),
        template_known=_as_bool(_pick(raw, "template_known", "templateKnown"), False),
        bonding=_as_int(_pick(raw, "bonding"), 0),
    )


def normalize_inventory(raw: dict) -> InventoryState:
    if not isinstance(raw, dict):
        raise AdapterError("inventory state returned a non-object")
    def rows(*names: str) -> tuple[Item, ...]:
        value = _pick(raw, *names) or []
        if not isinstance(value, list):
            raise AdapterError(f"inventory.{names[0]} is not a list")
        return tuple(normalize_item(row) for row in value if isinstance(row, dict))

    failure = _pick(raw, "last_failure", "lastFailure") or {}
    if not isinstance(failure, dict):
        failure = {}
    normalized_failure = {
        "operation": str(_pick(failure, "operation") or ""),
        "result": _as_int(_pick(failure, "result"), 0),
        "item_guid": _as_int(_pick(failure, "item_guid", "itemGuid"), 0),
        "item_id": _as_int(_pick(failure, "item_id", "itemId"), 0),
        "required_level": _as_int(
            _pick(failure, "required_level", "requiredLevel"), 0),
        "detail": str(_pick(failure, "detail") or ""),
    }
    vendor_guid = _as_int(_pick(raw, "vendor_guid", "vendorGuid"), 0)
    return InventoryState(
        items=rows("items"), vendor_items=rows("vendor_items", "vendorItems"),
        buyback=rows("buyback"), money=_as_int(_pick(raw, "money"), 0),
        free_slots=_as_int(_pick(raw, "free_slots", "freeSlots"), 0),
        vendor_guid=vendor_guid or None,
        vendor_loaded=_as_bool(_pick(raw, "vendor_loaded", "vendorLoaded"), False),
        revision=_as_int(_pick(raw, "revision"), 0),
        failure_count=_as_int(_pick(raw, "failure_count", "failureCount"), 0),
        last_failure=normalized_failure,
    )


def normalize_combat(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    keys = {
        "has_target": ("has_target", "hasTarget"),
        "target_guid": ("target_guid", "targetGuid"),
        "attacking": ("attacking",),
        "in_combat": ("in_combat", "inCombat"),
        "target_health": ("target_health", "targetHealth"),
        "target_max_health": ("target_max_health", "targetMaxHealth"),
        "target_dead": ("target_dead", "targetDead"),
        "self_health": ("self_health", "selfHealth"),
        "self_max_health": ("self_max_health", "selfMaxHealth"),
        "self_dead": ("self_dead", "selfDead"),
        "self_level": ("self_level", "selfLevel"),
        "self_xp": ("self_xp", "selfXp"),
        "self_next_level_xp": ("self_next_level_xp", "selfNextLevelXp"),
        "kills": ("kills",),
        "deaths": ("deaths",),
        "last_xp": ("last_xp", "lastXp"),
        "last_victim": ("last_victim", "lastVictim"),
        "last_error": ("last_error", "lastError"),
        "refusals": ("refusals",),
        "level_ups": ("level_ups", "levelUps"),
        "cast_failures": ("cast_failures", "castFailures"),
    }
    out = {}
    for target, names in keys.items():
        value = _pick(raw, *names)
        if value is not None:
            out[target] = value
    out["powers"] = _normalize_powers(raw)
    return out
