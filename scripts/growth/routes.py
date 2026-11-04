"""Versioned per-map route content with measured waypoints.

Routes are small, hand-verified content packs. They are not a navmesh and they
make no claim about live state: the worker walks a waypoint only after reading
its actual position and map from the server. Each band records the live
observation that produced it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "data" / "routes"
ZONE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class RouteError(ValueError):
    """A route file is missing or internally inconsistent."""


@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class LevelBand:
    min_level: int
    max_level: int
    waypoints: tuple[Waypoint, ...]
    source: str


@dataclass(frozen=True)
class TargetHints:
    hostile_factions: tuple[int, ...]
    hostile_names: tuple[str, ...]
    critter_names: tuple[str, ...]
    unit_types: tuple[int, ...]
    level_floor_delta: int
    level_ceiling_delta: int


@dataclass(frozen=True)
class RecoveryHints:
    waypoint_arrival_radius: float
    waypoint_dwell_seconds: float
    route_change_waypoints: int
    spirit_healer_names: tuple[str, ...]
    spirit_healer_interact_range: float
    ghost_search_seconds: float


@dataclass(frozen=True)
class TrainingRoute:
    position: dict
    waypoints: tuple[dict, ...] = ()
    vendor_position: dict | None = None
    vendor_waypoints: tuple[dict, ...] = ()
    minimum_money: int = 0


@dataclass(frozen=True)
class Route:
    version: int
    zone: str
    map_id: int
    source: str
    bands: tuple[LevelBand, ...]
    targets: TargetHints
    recovery: RecoveryHints
    training: dict[str, TrainingRoute] = field(default_factory=dict)

    def training_for(self, class_name: str) -> TrainingRoute | None:
        return self.training.get(class_name)

    def band_for(self, level: int) -> LevelBand | None:
        candidates = [
            band for band in self.bands
            if band.min_level <= level <= band.max_level
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda band: band.min_level)

    def anchors_for(self, level: int) -> tuple[Waypoint, ...]:
        band = self.band_for(level)
        return band.waypoints if band is not None else ()


def load_route(zone: str, *, directory: str | Path | None = None) -> Route:
    name = str(zone).strip().lower()
    if not ZONE_RE.match(name):
        raise RouteError(f"invalid zone name {zone!r}")
    base = Path(directory) if directory is not None else DEFAULT_DIR
    path = base / f"{name}.json"
    if not path.is_file():
        raise RouteError(
            f"no route content for {name!r} at {path}; add measured waypoints "
            "for each final start zone")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteError(f"route {path} is unreadable: {exc}") from exc
    return parse_route(data, expected_zone=name)


def parse_route(data: dict, *, expected_zone: str | None = None) -> Route:
    if not isinstance(data, dict):
        raise RouteError("route must be a mapping")
    zone = _required_text(data, "zone").lower()
    if expected_zone is not None and zone != expected_zone:
        raise RouteError(f"route zone {zone!r} does not match {expected_zone!r}")
    version = _required_int(data, "version", minimum=1)
    map_id = _required_int(data, "map_id", minimum=0)
    source = _required_text(data, "source")

    raw_bands = data.get("bands")
    if not isinstance(raw_bands, list) or not raw_bands:
        raise RouteError("route.bands must be a non-empty list")
    bands = tuple(_parse_band(raw, index) for index, raw in enumerate(raw_bands))

    targets = _parse_targets(data.get("targets"))
    recovery = _parse_recovery(data.get("recovery"))
    training = _parse_training(data.get("training", {}))
    return Route(
        version=version, zone=zone, map_id=map_id, source=source,
        bands=bands, targets=targets, recovery=recovery, training=training)


def _parse_training(raw) -> dict[str, TrainingRoute]:
    if not isinstance(raw, dict):
        raise RouteError("route.training must be a mapping by class")
    result = {}
    for class_name, entry in raw.items():
        if not isinstance(class_name, str) or not ZONE_RE.match(class_name):
            raise RouteError("route.training contains an invalid class name")
        if not isinstance(entry, dict):
            raise RouteError(f"route.training.{class_name} must be a mapping")
        vendor = entry.get("vendor_position")
        minimum_money = entry.get("minimum_money", 0)
        if (isinstance(minimum_money, bool) or not isinstance(minimum_money, int)
                or minimum_money < 0):
            raise RouteError(
                f"route.training.{class_name}.minimum_money must be >= 0")
        result[class_name] = TrainingRoute(
            position=_parse_service_position(entry.get("position"), class_name),
            waypoints=_parse_service_waypoints(entry.get("waypoints", [])),
            vendor_position=(_parse_service_position(vendor, class_name)
                             if vendor is not None else None),
            vendor_waypoints=_parse_service_waypoints(
                entry.get("vendor_waypoints", [])),
            minimum_money=minimum_money)
    return result


def _parse_service_position(raw, class_name: str) -> dict:
    if not isinstance(raw, dict):
        raise RouteError(f"route.training.{class_name} positions must be mappings")
    return {axis: _required_number(raw, axis) for axis in ("x", "y", "z")}


def _parse_service_waypoints(raw) -> tuple[dict, ...]:
    if not isinstance(raw, list):
        raise RouteError("route.training waypoints must be lists")
    return tuple(_parse_service_position(row, "waypoint") for row in raw)


def _parse_band(raw: dict, index: int) -> LevelBand:
    if not isinstance(raw, dict):
        raise RouteError(f"bands[{index}] must be a mapping")
    minimum = _required_int(raw, "min_level", minimum=1)
    maximum = _required_int(raw, "max_level", minimum=minimum)
    source = _required_text(raw, "source")
    raw_waypoints = raw.get("waypoints")
    if not isinstance(raw_waypoints, list) or not raw_waypoints:
        raise RouteError(f"bands[{index}].waypoints must be a non-empty list")
    waypoints = tuple(_parse_waypoint(item, index, position)
                      for position, item in enumerate(raw_waypoints))
    return LevelBand(min_level=minimum, max_level=maximum,
                     waypoints=waypoints, source=source)


def _parse_waypoint(raw: dict, band: int, index: int) -> Waypoint:
    if not isinstance(raw, dict):
        raise RouteError(f"bands[{band}].waypoints[{index}] must be a mapping")
    return Waypoint(
        x=_required_number(raw, "x"),
        y=_required_number(raw, "y"),
        z=_required_number(raw, "z"),
    )


def _parse_targets(raw) -> TargetHints:
    if not isinstance(raw, dict):
        raise RouteError("route.targets must be a mapping")
    return TargetHints(
        hostile_factions=tuple(
            _int_values(raw, "hostile_factions", minimum=0)),
        hostile_names=tuple(_text_values(raw, "hostile_names")),
        critter_names=tuple(_text_values(raw, "critter_names")),
        unit_types=tuple(_int_values(raw, "unit_types", minimum=0)),
        level_floor_delta=_required_int(raw, "level_floor_delta", minimum=0),
        level_ceiling_delta=_required_int(raw, "level_ceiling_delta", minimum=0),
    )


def _parse_recovery(raw) -> RecoveryHints:
    if not isinstance(raw, dict):
        raise RouteError("route.recovery must be a mapping")
    return RecoveryHints(
        waypoint_arrival_radius=_required_number(raw, "waypoint_arrival_radius"),
        waypoint_dwell_seconds=_required_number(raw, "waypoint_dwell_seconds"),
        route_change_waypoints=_required_int(
            raw, "route_change_waypoints", minimum=1),
        spirit_healer_names=tuple(_text_values(raw, "spirit_healer_names")),
        spirit_healer_interact_range=_required_number(
            raw, "spirit_healer_interact_range"),
        ghost_search_seconds=_required_number(raw, "ghost_search_seconds"),
    )


def _required_text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RouteError(f"route.{key} must be a non-empty string")
    return value.strip()


def _required_int(raw: dict, key: str, *, minimum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteError(f"route.{key} must be an integer")
    if value < minimum:
        raise RouteError(f"route.{key} must be >= {minimum}")
    return value


def _required_number(raw: dict, key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RouteError(f"route.{key} must be a number")
    return float(value)


def _text_values(raw: dict, key: str) -> list[str]:
    values = raw.get(key)
    if not isinstance(values, list) or not values:
        raise RouteError(f"route.{key} must be a non-empty list")
    out = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise RouteError(f"route.{key} contains an invalid value")
        out.append(value.strip().lower())
    return out


def _int_values(raw: dict, key: str, *, minimum: int) -> list[int]:
    values = raw.get(key)
    if not isinstance(values, list) or not values:
        raise RouteError(f"route.{key} must be a non-empty list")
    out = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise RouteError(f"route.{key} contains an invalid value")
        out.append(value)
    return out
