"""Versioned five-character roster (config/roster.yaml).

JSON is valid YAML, so the roster stays parseable with the standard library.
An empty or explicitly unconfigured roster fails closed: no worker may start
until the user supplies the five characters.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROSTER = ROOT / "config" / "roster.yaml"
ROLES = ("tank", "healer", "dps")
ROLE_LABELS = {"tank": "tank", "healer": "healer", "dps": "damage"}
ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
EXPECTED_CHARACTERS = 5


class RosterError(ValueError):
    """The roster is missing, unconfigured, or internally inconsistent."""


@dataclass(frozen=True)
class CharacterSpec:
    account: str
    name: str
    guid: int
    port: int
    role: str
    class_name: str
    race: str
    faction: str
    start_zone: str


@dataclass(frozen=True)
class Roster:
    version: int
    configured: bool
    faction: str
    start_zone: str
    characters: tuple[CharacterSpec, ...] = field(default_factory=tuple)

    def by_account(self) -> dict[str, CharacterSpec]:
        return {spec.account: spec for spec in self.characters}

    def by_character(self) -> dict[str, CharacterSpec]:
        return {spec.name: spec for spec in self.characters}


def load_roster(path: str | Path | None = None) -> Roster:
    target = Path(path) if path is not None else DEFAULT_ROSTER
    if not target.is_file():
        raise RosterError(
            f"roster {target} is missing; create it with five characters")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RosterError(f"roster {target} is unreadable: {exc}") from exc
    return parse_roster(data)


def parse_roster(data: dict) -> Roster:
    if not isinstance(data, dict):
        raise RosterError("roster must be a mapping")
    if data.get("configured") is not True:
        raise RosterError("roster is not configured (set configured: true)")
    faction = _required_text(data, "faction")
    start_zone = _required_text(data, "start_zone")
    raw_characters = data.get("characters")
    if not isinstance(raw_characters, list):
        raise RosterError("roster.characters must be a list")
    if len(raw_characters) != EXPECTED_CHARACTERS:
        raise RosterError(
            f"roster needs exactly {EXPECTED_CHARACTERS} characters, "
            f"found {len(raw_characters)}")

    characters: list[CharacterSpec] = []
    for index, raw in enumerate(raw_characters):
        if not isinstance(raw, dict):
            raise RosterError(f"characters[{index}] must be a mapping")
        account = _required_text(raw, "account", index)
        if not ACCOUNT_RE.match(account):
            raise RosterError(f"characters[{index}].account is invalid: {account!r}")
        name = _required_text(raw, "name", index)
        guid = _required_int(raw, "guid", index, minimum=1)
        port = _required_int(raw, "port", index, minimum=1, maximum=65535)
        role = _required_text(raw, "role", index).lower()
        if role not in ROLES:
            raise RosterError(
                f"characters[{index}].role must be one of {ROLES}: {role!r}")
        class_name = _required_text(raw, "class", index)
        race = _required_text(raw, "race", index)
        char_faction = str(raw.get("faction") or faction).strip().lower()
        char_zone = str(raw.get("start_zone") or start_zone).strip().lower()
        if char_faction != faction.lower():
            raise RosterError(
                f"characters[{index}] faction {char_faction!r} != {faction!r}")
        if char_zone != start_zone.lower():
            raise RosterError(
                f"characters[{index}] start_zone {char_zone!r} != {start_zone!r}")
        characters.append(CharacterSpec(
            account=account, name=name, guid=guid, port=port, role=role,
            class_name=class_name, race=race, faction=faction,
            start_zone=start_zone,
        ))

    roster = Roster(
        version=int(data.get("version", 1)),
        configured=True,
        faction=faction,
        start_zone=start_zone,
        characters=tuple(characters),
    )
    _validate_uniqueness(roster)
    _validate_roles(roster)
    return roster


def _required_text(raw: dict, key: str, index: int | None = None) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        where = f"characters[{index}]." if index is not None else ""
        raise RosterError(f"roster.{where}{key} must be a non-empty string")
    return value.strip()


def _required_int(
    raw: dict, key: str, index: int, *, minimum: int, maximum: int | None = None
) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RosterError(f"characters[{index}].{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise RosterError(f"characters[{index}].{key} out of range: {value}")
    return value


def _validate_uniqueness(roster: Roster) -> None:
    for attr, label in (
        ("account", "account"),
        ("name", "character name"),
        ("guid", "GUID"),
        ("port", "worldrpc port"),
    ):
        values = [getattr(spec, attr) for spec in roster.characters]
        normalized = [v.casefold() if isinstance(v, str) else v for v in values]
        if len(set(normalized)) != len(normalized):
            raise RosterError(f"duplicate {label} in roster")


def _validate_roles(roster: Roster) -> None:
    present = {spec.role for spec in roster.characters}
    missing = [role for role in ROLES if role not in present]
    if missing:
        raise RosterError(
            "roster lacks role coverage: " + ", ".join(missing))
