"""Read-only AzerothCore dump parsing and Durotar quest graph generation.

Downloaded SQL is data, never executable content. This module accepts explicit
paths, validates the dump footer before parsing, and emits candidates only;
live NPC and walkability verification remain a separate deployment gate.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, TextIO

CREATE_RE = re.compile(r"^  `([^`]+)`")
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
INT_RE = re.compile(r"^[+-]?\d+$")
FOOTER = b"-- Dump completed on "
HORDE_RACES = 1 | 2 | 4 | 8 | 32 | 128 | 512
ROSTER_CLASSES = 1 | 4 | 16 | 64 | 256


class SQLDumpError(ValueError):
    pass


def parse_insert(statement: str, columns: int) -> list[list[object]]:
    """Parse one VALUES statement without splitting inside quoted strings."""
    match = re.match(r"^\s*INSERT\s+INTO\s+`[^`]+`\s+VALUES\s*", statement,
                     re.IGNORECASE)
    if match is None:
        raise SQLDumpError("not an INSERT INTO `table` VALUES statement")
    parser = _ValuesParser(statement, match.end(), columns)
    return parser.parse()


class _ValuesParser:
    def __init__(self, text: str, offset: int, columns: int):
        self.text = text
        self.pos = offset
        self.columns = columns

    def parse(self) -> list[list[object]]:
        rows = []
        self._space()
        while self._peek() == "(":
            rows.append(self._row())
            self._space()
            if self._peek() == ",":
                self.pos += 1
                self._space()
                continue
            break
        if self._peek() != ";":
            raise SQLDumpError(f"expected statement terminator at byte {self.pos}")
        self.pos += 1
        self._space()
        if self.pos != len(self.text):
            raise SQLDumpError("unexpected data after INSERT statement")
        if not rows:
            raise SQLDumpError("INSERT statement has no rows")
        return rows

    def _row(self) -> list[object]:
        self._expect("(")
        row = []
        while True:
            self._space()
            row.append(self._string() if self._peek() == "'" else self._scalar())
            self._space()
            char = self._peek()
            if char == ",":
                self.pos += 1
                continue
            if char == ")":
                self.pos += 1
                break
            raise SQLDumpError(f"expected comma or closing parenthesis at byte {self.pos}")
        if len(row) != self.columns:
            raise SQLDumpError(
                f"row has {len(row)} values; schema declares {self.columns}")
        return row

    def _string(self) -> str:
        self._expect("'")
        result = []
        escapes = {"0": "\0", "b": "\b", "n": "\n", "r": "\r",
                   "t": "\t", "Z": "\x1a"}
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == "\\":
                if self.pos >= len(self.text):
                    raise SQLDumpError("truncated backslash escape")
                escaped = self.text[self.pos]
                self.pos += 1
                result.append(escapes.get(escaped, escaped))
            elif char == "'":
                if self._peek() == "'":
                    self.pos += 1
                    result.append("'")
                else:
                    return "".join(result)
            else:
                result.append(char)
        raise SQLDumpError("unterminated quoted string")

    def _scalar(self) -> object:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",) \t\r\n":
            self.pos += 1
        token = self.text[start:self.pos]
        if token.upper() == "NULL":
            return None
        if INT_RE.match(token):
            return int(token)
        if NUMBER_RE.match(token):
            return float(token)
        raise SQLDumpError(f"unsupported or empty scalar {token!r} at byte {start}")

    def _space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _expect(self, expected: str) -> None:
        if self._peek() != expected:
            raise SQLDumpError(f"expected {expected!r} at byte {self.pos}")
        self.pos += 1


def schema_columns(handle: TextIO) -> list[str]:
    columns = []
    in_create = False
    for line in handle:
        if line.startswith("CREATE TABLE `"):
            if in_create or columns:
                raise SQLDumpError("dump must contain exactly one CREATE TABLE")
            in_create = True
            continue
        if in_create:
            match = CREATE_RE.match(line)
            if match:
                columns.append(match.group(1))
            elif line.startswith(") ENGINE="):
                if not columns:
                    raise SQLDumpError("CREATE TABLE has no columns")
                return columns
    raise SQLDumpError("complete CREATE TABLE was not found before INSERT")


def iter_table(path: str | Path) -> Iterator[dict[str, object]]:
    """Stream row dictionaries; truncated dumps fail instead of yielding partial data."""
    source = Path(path)
    with source.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 4096))
        if FOOTER not in handle.read():
            raise SQLDumpError(f"{source} has no dump-completed footer")
    with source.open("r", encoding="utf-8") as handle:
        columns = schema_columns(handle)
        statement = None
        quoted = escaped = False
        for line in handle:
            if statement is None:
                if line.startswith("INSERT INTO `"):
                    statement = [line]
                    quoted = escaped = False
                continue
            statement.append(line)
            for char in line:
                if escaped:
                    escaped = False
                elif quoted and char == "\\":
                    escaped = True
                elif char == "'":
                    quoted = not quoted
            if not quoted and not escaped and line.rstrip().endswith(";"):
                rows = parse_insert("".join(statement), len(columns))
                for row in rows:
                    yield dict(zip(columns, row))
                statement = None
        if statement is not None:
            raise SQLDumpError(f"{source} ends inside an INSERT statement")


def _relations(path: Path, relation_key: str, id_key: str) -> dict[int, list[int]]:
    result = defaultdict(list)
    for row in iter_table(path):
        result[int(row[id_key])].append(int(row[relation_key]))
    return result


def _position(row: dict) -> dict[str, float | int]:
    return {"guid": int(row["guid"]), "x": float(row["position_x"]),
            "y": float(row["position_y"]), "z": float(row["position_z"])}


def _cluster(spawns: list[dict]) -> dict | None:
    if not spawns:
        return None
    # Choose a real medoid spawn, not independent median axes that can form a
    # point inside a wall or above the terrain.
    center = {axis: sum(row[axis] for row in spawns) / len(spawns)
              for axis in ("x", "y", "z")}
    nearest = min(spawns, key=lambda row: sum(
        (row[axis] - center[axis]) ** 2 for axis in ("x", "y", "z")))
    return {axis: nearest[axis] for axis in ("x", "y", "z")} | {
        "guid": nearest["guid"], "spawn_count": len(spawns)}


def build_durotar_graph(source_dir: str | Path) -> dict:
    base = Path(source_dir)
    starters = _relations(base / "creature_queststarter/creature_queststarter.sql",
                          "id", "quest")
    enders = _relations(base / "creature_questender/creature_questender.sql",
                        "id", "quest")
    addons = {int(row["ID"]): row for row in iter_table(
        base / "quest_template_addon/quest_template_addon.sql")}
    quests = {}
    rejected = defaultdict(int)
    for row in iter_table(base / "quest_template/quest_template.sql"):
        if int(row["QuestSortID"]) not in (14, 363):
            continue
        if not 1 <= int(row["QuestLevel"]) <= 20:
            rejected["outside_1_20"] += 1
            continue
        if int(row["AllowableRaces"]) and not int(row["AllowableRaces"]) & HORDE_RACES:
            rejected["not_horde"] += 1
            continue
        addon = addons.get(int(row["ID"]), {})
        classes = int(addon.get("AllowableClasses") or 0)
        if classes and not classes & ROSTER_CLASSES:
            rejected["not_roster_class"] += 1
            continue
        npcs = [int(row[f"RequiredNpcOrGo{i}"]) for i in range(1, 5)]
        items = [int(row[f"RequiredItemId{i}"]) for i in range(1, 7)]
        reasons = []
        if any(items):
            reasons.append("item_objectives_not_decoded")
        if any(entry < 0 for entry in npcs):
            reasons.append("game_object_objectives_not_decoded")
        if int(row["RequiredPlayerKills"]):
            reasons.append("pvp_objective")
        if int(row["TimeAllowed"]):
            reasons.append("timed")
        if int(row["SuggestedGroupNum"]) > 1:
            reasons.append("group_quest")
        if int(row["StartItem"]):
            reasons.append("external_start_item")
        objectives = [{"entry": entry,
                       "required": int(row[f"RequiredNpcOrGoCount{i}"])}
                      for i, entry in enumerate(npcs, 1) if entry > 0]
        quests[int(row["ID"])] = {
            "quest_id": int(row["ID"]), "title": row["LogTitle"],
            "quest_level": int(row["QuestLevel"]), "min_level": int(row["MinLevel"]),
            "sort_id": int(row["QuestSortID"]),
            "reward_xp_difficulty": int(row["RewardXPDifficulty"]),
            "reward_money": int(row["RewardMoney"]),
            "races": int(row["AllowableRaces"]), "classes": classes,
            "prev_quest_id": int(addon.get("PrevQuestID") or 0),
            "next_quest_id": int(addon.get("NextQuestID") or 0),
            "exclusive_group": int(addon.get("ExclusiveGroup") or 0),
            "starter_entries": sorted(starters.get(int(row["ID"]), [])),
            "ender_entries": sorted(enders.get(int(row["ID"]), [])),
            "objectives": objectives, "excluded": reasons,
        }
    npc_ids = {entry for quest in quests.values()
               for entry in quest["starter_entries"] + quest["ender_entries"]
               + [row["entry"] for row in quest["objectives"]]}
    templates = {}
    for row in iter_table(base / "creature_template/creature_template.sql"):
        if int(row["entry"]) in npc_ids:
            templates[int(row["entry"])] = {
                "name": row["name"], "min_level": int(row["minlevel"]),
                "max_level": int(row["maxlevel"]), "faction": int(row["faction"]),
                "rank": int(row["rank"]), "type": int(row["type"]),
            }
    spawns = defaultdict(list)
    for row in iter_table(base / "creature/creature.sql"):
        if int(row["map"]) == 1 and int(row["id1"]) in npc_ids:
            spawns[int(row["id1"])].append(_position(row))

    candidates = []
    for quest in quests.values():
        reasons = list(quest.pop("excluded"))
        for role in ("starter_entries", "ender_entries"):
            entries = quest[role]
            label = role.removesuffix("_entries")
            if not entries:
                reasons.append(f"no_creature_{label}")
            if not any(spawns.get(entry) for entry in entries):
                reasons.append(f"no_map_1_{label}_spawn")
        for objective in quest["objectives"]:
            entry = objective["entry"]
            objective.update(templates.get(entry, {"name": f"entry {entry}"}))
            objective["candidate_anchor"] = _cluster(spawns.get(entry, []))
            objective["candidate_spawns"] = spawns.get(entry, [])
            if not spawns.get(entry):
                reasons.append(f"no_map_1_objective_spawn:{entry}")
        quest["accept_candidates"] = [
            {"entry": entry, **templates.get(entry, {}),
             "spawns": spawns.get(entry, [])} for entry in quest["starter_entries"]]
        quest["turn_in_candidates"] = [
            {"entry": entry, **templates.get(entry, {}),
             "spawns": spawns.get(entry, [])} for entry in quest["ender_entries"]]
        quest["eligible_kill_or_talk"] = not reasons
        quest["validation_required"] = sorted(set(reasons)) if reasons else [
            "live_npc_identity", "live_walkability", "live_quest_accept"]
        quest["estimated_kill_cost"] = sum(
            row["required"] for row in quest["objectives"])
        candidates.append(quest)
    candidates.sort(key=lambda row: (row["min_level"], row["quest_level"],
                                     row["quest_id"]))
    return {
        "version": 1, "zone": "durotar", "map_id": 1,
        "source": "AzerothCore master world SQL snapshot downloaded and hash-checked 2026-11-04",
        "scope": "QuestSortID 14/363, levels 1-20, Horde and current roster classes",
        "limitations": [
            "Candidates are not an executable route until live NPC and walkability checks pass.",
            "reward_xp_difficulty is a DBC index, not an XP amount.",
            "Item, game-object, timed, PvP and group objectives are excluded from execution.",
        ],
        "rejected_before_graph": dict(sorted(rejected.items())),
        "eligible_count": sum(row["eligible_kill_or_talk"] for row in candidates),
        "quests": candidates,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    graph = build_durotar_graph(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output), "quests": len(graph["quests"]),
                      "eligible": graph["eligible_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
