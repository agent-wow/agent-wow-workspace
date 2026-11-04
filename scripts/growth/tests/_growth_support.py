"""Shared fakes for the U2 solo-growth tests.

Everything here is deterministic: a fake transport/adapter, a compact world
view builder, and helpers for the versioned profile/route content. No test in
this module touches a live session or the account tree.
"""
from __future__ import annotations

from dataclasses import replace

from scripts.growth.adapter import WorldView
from scripts.growth.items import InventoryState
from scripts.growth.profiles import load_profile
from scripts.growth.routes import load_route

ROUTE = load_route("elwynn-forest")
WARRIOR = load_profile("warrior")
PRIEST = load_profile("priest")
ROGUE = load_profile("rogue")


def with_combat(profile, **overrides):
    return replace(profile, combat=replace(profile.combat, **overrides))


def unit(
    guid: int,
    *,
    name: str = "Wolf",
    level: int = 5,
    health: int = 100,
    max_health: int = 100,
    x: float = 2.0,
    y: float = 0.0,
    z: float = 0.0,
    faction: int = 32,
    dead: bool = False,
    unit_type: int = 3,
) -> dict:
    return {
        "guid": guid,
        "name": name,
        "type": unit_type,
        "level": level,
        "health": health,
        "max_health": max_health,
        "dead": dead,
        "faction": faction,
        "has_position": True,
        "position": {"x": x, "y": y, "z": z},
        "in_combat": False,
    }


def make_view(
    *,
    ready: bool = True,
    epoch: str | None = "1",
    map_id: int | None = 0,
    units=(),
    target_guid: int | None = None,
    level: int = 5,
    xp: int = 0,
    next_level_xp: int = 100,
    health: int = 100,
    max_health: int = 100,
    dead: bool = False,
    ghost: bool = False,
    in_combat: bool = False,
    powers: dict | None = None,
    position: dict | None = None,
    corpse_reclaimable: bool = False,
    kills: int = 0,
    deaths: int = 0,
    cast_failures: int = 0,
    combat_extra: dict | None = None,
) -> WorldView:
    pos = position if position is not None else {"x": 0.0, "y": 0.0, "z": 0.0,
                                                 "orientation": 0.0}
    self_state = {
        "guid": 1,
        "name": "Growth1",
        "level": level,
        "health": health,
        "max_health": max_health,
        "xp": xp,
        "next_level_xp": next_level_xp,
        "dead": dead,
        "ghost": ghost,
        "in_combat": in_combat,
        "corpse_reclaimable": corpse_reclaimable,
        "powers": dict(powers or {}),
        "position": dict(pos),
        "orientation": float(pos.get("orientation", 0.0)),
    }
    combat = {
        "kills": kills,
        "deaths": deaths,
        "self_dead": dead,
        "in_combat": in_combat,
        "cast_failures": cast_failures,
    }
    combat.update(combat_extra or {})
    return WorldView(
        ready=ready,
        epoch=epoch,
        map_id=map_id,
        self_state=self_state,
        position=dict(pos),
        nearby=tuple(units),
        target={"guid": target_guid} if target_guid is not None else None,
        combat=combat,
    )


class FakeAdapter:
    """Records actions and returns a canned world view."""

    def __init__(self, view: WorldView):
        self.view = view
        self.calls: list[tuple[str, tuple]] = []
        self.error: Exception | None = None

    def read_view(self) -> WorldView:
        if self.error is not None:
            raise self.error
        return self.view

    def attack(self, guid):
        self.calls.append(("attack", (int(guid),)))
        return {"target_guid": int(guid), "attacking": True}

    def get_inventory(self):
        self.calls.append(("get_inventory", ()))
        return InventoryState()

    def stop_attack(self):
        self.calls.append(("stop_attack", ()))
        return {}

    def stop_movement(self):
        self.calls.append(("stop_movement", ()))
        return {}

    def set_ammo(self, item_id):
        self.calls.append(("set_ammo", (int(item_id),)))
        return {}

    def cast(self, spell_id, target_guid):
        self.calls.append(("cast", (int(spell_id), int(target_guid))))
        return {}

    def move_to(self, x, y, z):
        self.calls.append(("move_to", (float(x), float(y), float(z))))
        return {"position": {"x": float(x), "y": float(y), "z": float(z)}}

    def navigate(self, x, y, z):
        self.calls.append(("navigate", (float(x), float(y), float(z))))
        return {"position": {"x": float(x), "y": float(y), "z": float(z)}}

    def turn_to(self, orientation):
        self.calls.append(("turn_to", (float(orientation),)))
        return {}

    def loot(self, guid):
        self.calls.append(("loot", (int(guid),)))
        return {}

    def release_spirit(self):
        self.calls.append(("release_spirit", ()))
        return {}

    def reclaim_corpse(self):
        self.calls.append(("reclaim_corpse", ()))
        return {}

    def resurrect_at_spirit_healer(self, guid):
        self.calls.append(("resurrect_at_spirit_healer", (int(guid),)))
        return {}

    def named(self, name: str) -> list[tuple]:
        return [args for call, args in self.calls if call == name]
