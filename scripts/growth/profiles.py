"""Versioned class profiles for the solo growth loop.

A profile is *content*, not live state. It declares a class's roles, primary
resource, initial skill priorities, recovery thresholds and which route zone the
class should use. The live power/health/cooldown values always come from the
server through :mod:`scripts.growth.adapter`; nothing here pretends to know what
the character is doing right now.

Skill ``cost`` is expressed in the class's native resource units (15 rage, 45
energy, 30 mana). ``resource_scale`` converts that to the observed field units:
warrior rage is stored x10 in its primary power field; mana and energy are x1.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "data" / "profiles"
RESOURCES = ("rage", "mana", "energy")
ROLES = ("tank", "healer", "dps")
CLASS_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ProfileError(ValueError):
    """A class profile is missing or internally inconsistent."""


@dataclass(frozen=True)
class Skill:
    name: str
    spell_id: int
    cost: int
    priority: int
    min_level: int = 1
    requires_target: bool = True
    in_combat_only: bool = False
    when_health_below_pct: float | None = None
    when_health_above_pct: float | None = None


@dataclass(frozen=True)
class RecoveryRules:
    rest_below_pct: float
    rest_until_pct: float
    flee_below_pct: float
    max_rest_seconds: float
    rest_step_seconds: float


@dataclass(frozen=True)
class CombatRules:
    engage_radius: float
    melee_range: float
    auto_attack: bool
    approach_step: float
    dz_tolerance: float
    attack_reissue_seconds: float
    cast_interval_seconds: float
    stall_seconds: float
    no_kill_seconds: float
    max_approach_seconds: float
    blacklist_seconds: float
    side_step_distance: float
    crowd_radius: float
    max_crowd: int
    lost_target_limit: int
    spell_disable_seconds: float


@dataclass(frozen=True)
class RouteSelection:
    zone: str
    map_id: int


@dataclass(frozen=True)
class ClassProfile:
    version: int
    class_name: str
    source: str
    roles: tuple[str, ...]
    resource: str
    resource_scale: int
    skills: tuple[Skill, ...]
    recovery: RecoveryRules
    combat: CombatRules
    route: RouteSelection

    def supports_role(self, role: str) -> bool:
        return role in self.roles

    def skill_order(self, level: int) -> tuple[Skill, ...]:
        """Skills unlocked at *level*, highest priority first (stable order)."""
        eligible = [skill for skill in self.skills if skill.min_level <= level]
        return tuple(sorted(eligible, key=lambda s: (-s.priority, s.spell_id)))


def load_profile(
    class_name: str, *, directory: str | Path | None = None
) -> ClassProfile:
    name = str(class_name).strip().lower()
    if not CLASS_RE.match(name):
        raise ProfileError(f"invalid class name {class_name!r}")
    base = Path(directory) if directory is not None else DEFAULT_DIR
    path = base / f"{name}.json"
    if not path.is_file():
        raise ProfileError(
            f"no class profile for {name!r} at {path}; add versioned content "
            "for each final roster class")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"profile {path} is unreadable: {exc}") from exc
    return parse_profile(data, expected_class=name)


def parse_profile(data: dict, *, expected_class: str | None = None) -> ClassProfile:
    if not isinstance(data, dict):
        raise ProfileError("profile must be a mapping")
    class_name = _required_text(data, "class").lower()
    if expected_class is not None and class_name != expected_class:
        raise ProfileError(
            f"profile class {class_name!r} does not match {expected_class!r}")
    version = _required_int(data, "version", minimum=1)
    source = _required_text(data, "source")

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ProfileError("profile.roles must be a non-empty list")
    roles = tuple(str(role).strip().lower() for role in raw_roles)
    unknown = [role for role in roles if role not in ROLES]
    if unknown:
        raise ProfileError(f"profile.roles contains unknown roles: {unknown}")

    resource = _required_text(data, "resource").lower()
    if resource not in RESOURCES:
        raise ProfileError(f"profile.resource must be one of {RESOURCES}")
    scale = _required_int(data, "resource_scale", minimum=1)

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills:
        raise ProfileError("profile.skills must be a non-empty list")
    skills = tuple(_parse_skill(raw, index) for index, raw in enumerate(raw_skills))

    recovery = _parse_recovery(data.get("recovery"))
    combat = _parse_combat(data.get("combat"))
    route = _parse_route(data.get("route"))

    return ClassProfile(
        version=version, class_name=class_name, source=source, roles=roles,
        resource=resource, resource_scale=scale, skills=skills,
        recovery=recovery, combat=combat, route=route)


def _parse_skill(raw: dict, index: int) -> Skill:
    if not isinstance(raw, dict):
        raise ProfileError(f"skills[{index}] must be a mapping")
    name = _required_text(raw, "name")
    spell_id = _required_int(raw, "spell_id", minimum=1)
    cost = _required_int(raw, "cost", minimum=0)
    priority = _required_int(raw, "priority", minimum=0)
    min_level = _optional_int(raw, "min_level", 1, minimum=1)
    requires_target = _optional_bool(raw, "requires_target", True)
    in_combat_only = _optional_bool(raw, "in_combat_only", False)
    below = _optional_pct(raw, "when_health_below_pct")
    above = _optional_pct(raw, "when_health_above_pct")
    return Skill(
        name=name, spell_id=spell_id, cost=cost, priority=priority,
        min_level=min_level, requires_target=requires_target,
        in_combat_only=in_combat_only,
        when_health_below_pct=below, when_health_above_pct=above)


def _parse_recovery(raw) -> RecoveryRules:
    if not isinstance(raw, dict):
        raise ProfileError("profile.recovery must be a mapping")
    rest_below = _required_float(raw, "rest_below_pct", minimum=0.0, maximum=1.0)
    rest_until = _required_float(raw, "rest_until_pct", minimum=0.0, maximum=1.0)
    flee = _required_float(raw, "flee_below_pct", minimum=0.0, maximum=1.0)
    if flee > rest_below or rest_below > rest_until:
        raise ProfileError(
            "recovery thresholds must satisfy flee_below_pct <= "
            "rest_below_pct <= rest_until_pct")
    return RecoveryRules(
        rest_below_pct=rest_below,
        rest_until_pct=rest_until,
        flee_below_pct=flee,
        max_rest_seconds=_required_float(raw, "max_rest_seconds", minimum=1.0),
        rest_step_seconds=_required_float(raw, "rest_step_seconds", minimum=0.1),
    )


def _parse_combat(raw) -> CombatRules:
    if not isinstance(raw, dict):
        raise ProfileError("profile.combat must be a mapping")
    return CombatRules(
        engage_radius=_required_float(raw, "engage_radius", minimum=1.0),
        melee_range=_required_float(raw, "melee_range", minimum=1.0),
        auto_attack=_optional_bool(raw, "auto_attack", True),
        approach_step=_required_float(raw, "approach_step", minimum=1.0),
        dz_tolerance=_required_float(raw, "dz_tolerance", minimum=0.0),
        attack_reissue_seconds=_required_float(raw, "attack_reissue_seconds", minimum=0.1),
        cast_interval_seconds=_required_float(raw, "cast_interval_seconds", minimum=0.1),
        stall_seconds=_required_float(raw, "stall_seconds", minimum=1.0),
        no_kill_seconds=_required_float(raw, "no_kill_seconds", minimum=1.0),
        max_approach_seconds=_required_float(raw, "max_approach_seconds", minimum=1.0),
        blacklist_seconds=_required_float(raw, "blacklist_seconds", minimum=1.0),
        side_step_distance=_required_float(raw, "side_step_distance", minimum=1.0),
        crowd_radius=_required_float(raw, "crowd_radius", minimum=1.0),
        max_crowd=_required_int(raw, "max_crowd", minimum=0),
        lost_target_limit=_required_int(raw, "lost_target_limit", minimum=1),
        spell_disable_seconds=_required_float(raw, "spell_disable_seconds", minimum=1.0),
    )


def _parse_route(raw) -> RouteSelection:
    if not isinstance(raw, dict):
        raise ProfileError("profile.route must be a mapping")
    return RouteSelection(
        zone=_required_text(raw, "zone").lower(),
        map_id=_required_int(raw, "map_id", minimum=0),
    )


# --------------------------------------------------------------- primitives

def _required_text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"profile.{key} must be a non-empty string")
    return value.strip()


def _required_int(raw: dict, key: str, *, minimum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"profile.{key} must be an integer")
    if value < minimum:
        raise ProfileError(f"profile.{key} must be >= {minimum}")
    return value


def _optional_int(raw: dict, key: str, default: int, *, minimum: int) -> int:
    if key not in raw:
        return default
    return _required_int(raw, key, minimum=minimum)


def _required_float(
    raw: dict, key: str, *, minimum: float, maximum: float | None = None
) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"profile.{key} must be a number")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        raise ProfileError(f"profile.{key} is out of range: {number}")
    return number


def _optional_bool(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ProfileError(f"profile.{key} must be a boolean")
    return value


def _optional_pct(raw: dict, key: str) -> float | None:
    if raw.get(key) is None:
        return None
    return _required_float(raw, key, minimum=0.0, maximum=1.0)
