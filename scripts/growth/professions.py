"""Versioned profession content; live skill ranks always come from sense."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "data" / "professions" / "default.json"


class ProfessionContentError(ValueError):
    pass


@dataclass(frozen=True)
class Profession:
    kind: str
    skill_id: int
    trainer_spell_id: int


@dataclass(frozen=True)
class Recipe:
    spell_id: int
    skill_id: int
    min_skill: int
    target_skill: int
    materials: dict[int, int]


@dataclass(frozen=True)
class ProfessionContent:
    version: int
    source: str
    trainer_npc_flags: int
    interaction_distance: float
    approach_distance: float
    retry_cooldown_seconds: float
    professions: tuple[Profession, ...]
    recipes: tuple[Recipe, ...]
    gathering_entries: tuple[int, ...]


def load_professions(path: str | Path | None = None) -> ProfessionContent:
    source = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfessionContentError(f"profession content unreadable: {exc}") from exc
    try:
        professions = tuple(Profession(
            kind=str(row["kind"]), skill_id=int(row["skill_id"]),
            trainer_spell_id=int(row["trainer_spell_id"]),
        ) for row in raw["professions"])
        if {row.kind for row in professions} != {"gathering", "manufacturing", "auxiliary"}:
            raise ProfessionContentError("one gathering, manufacturing and auxiliary profession are required")
        if len({row.skill_id for row in professions}) != 3:
            raise ProfessionContentError("profession skill IDs must be distinct")
        recipes = tuple(Recipe(
            spell_id=int(row["spell_id"]), skill_id=int(row["skill_id"]),
            min_skill=int(row["min_skill"]), target_skill=int(row["target_skill"]),
            materials={int(item): int(count) for item, count in row["materials"].items()},
        ) for row in raw["recipes"])
        known_skills = {row.skill_id for row in professions}
        if any(row.skill_id not in known_skills or row.min_skill < 0
               or row.target_skill <= row.min_skill or not row.materials
               or any(count < 1 for count in row.materials.values()) for row in recipes):
            raise ProfessionContentError("invalid profession recipe")
        return ProfessionContent(
            version=int(raw["version"]), source=str(raw["source"]),
            trainer_npc_flags=int(raw["trainer_npc_flags"]),
            interaction_distance=float(raw["interaction_distance"]),
            approach_distance=float(raw["approach_distance"]),
            retry_cooldown_seconds=float(raw["retry_cooldown_seconds"]),
            professions=professions, recipes=recipes,
            gathering_entries=tuple(int(value) for value in raw["gathering_entries"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProfessionContentError):
            raise
        raise ProfessionContentError(f"invalid profession content: {exc}") from exc
