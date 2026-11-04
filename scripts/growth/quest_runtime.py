"""Ordered, server-reconciled quest navigation for versioned route content.

The content describes *what* to do; live quest packets decide whether an
objective or turn-in is complete. A worker restart returns to the current
quest's accept step so ALREADY_ON and ALREADY_DONE responses reconcile state
before another reward request is sent.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .adapter import AdapterError, WorldView
from .social import RuntimeAction

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "data" / "quests"
ZONE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class QuestContentError(ValueError):
    pass


@dataclass(frozen=True)
class QuestNPC:
    name: str
    guid: int
    position: dict
    waypoints: tuple[dict, ...] = ()
    recovery_waypoints: tuple[dict, ...] = ()
    vendor_position: dict | None = None
    vendor_waypoints: tuple[dict, ...] = ()


@dataclass(frozen=True)
class QuestObjective:
    entry: int
    name: str
    required: int
    position: dict | None = None


@dataclass(frozen=True)
class QuestStep:
    quest_id: int
    title: str
    accept: QuestNPC
    turn_in: QuestNPC
    objectives: tuple[QuestObjective, ...]
    min_level: int = 0
    quest_level: int = 0
    reward_xp_difficulty: int = 0
    batch: str = ""
    recommended_level: int = 0


@dataclass(frozen=True)
class QuestContent:
    version: int
    zone: str
    map_id: int
    source: str
    quests: tuple[QuestStep, ...]


def load_quest_content(zone: str, *, directory: str | Path | None = None) -> QuestContent:
    name = str(zone).strip().lower()
    if not ZONE_RE.match(name):
        raise QuestContentError(f"invalid quest zone {zone!r}")
    base = Path(directory) if directory is not None else DEFAULT_DIR
    path = base / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuestContentError(f"quest route {path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("zone") != name:
        raise QuestContentError(f"quest route {path} has the wrong zone")
    try:
        quests = tuple(_quest(row) for row in raw["quests"])
        if not quests:
            raise ValueError("empty quest list")
        if len({row.quest_id for row in quests}) != len(quests):
            raise ValueError("duplicate quest id")
        return QuestContent(
            version=int(raw["version"]), zone=name, map_id=int(raw["map_id"]),
            source=str(raw["source"]), quests=quests)
    except (KeyError, TypeError, ValueError) as exc:
        raise QuestContentError(f"quest route {path} is invalid: {exc}") from exc


def _position(raw: dict) -> dict:
    return {key: float(raw[key]) for key in ("x", "y", "z")}


def _npc(raw: dict) -> QuestNPC:
    return QuestNPC(
        name=str(raw["npc"]), guid=int(raw["guid"]),
        position=_position(raw["position"]),
        waypoints=tuple(_position(row) for row in raw.get("waypoints", ())),
        recovery_waypoints=tuple(
            _position(row) for row in raw.get("recovery_waypoints", ())),
        vendor_position=(_position(raw["vendor_position"])
                         if raw.get("vendor_position") is not None else None),
        vendor_waypoints=tuple(
            _position(row) for row in raw.get("vendor_waypoints", ())))


def _objective(raw: dict) -> QuestObjective:
    position = raw.get("position")
    return QuestObjective(
        entry=int(raw["entry"]), name=str(raw["name"]),
        required=int(raw["required"]),
        position=_position(position) if position is not None else None)


def _quest(raw: dict) -> QuestStep:
    objectives = tuple(_objective(row) for row in raw["objectives"])
    if any(row.entry <= 0 or row.required <= 0 for row in objectives):
        raise ValueError("objective entries and counts must be positive")
    return QuestStep(
        quest_id=int(raw["quest_id"]), title=str(raw["title"]),
        accept=_npc(raw["accept"]), turn_in=_npc(raw["turn_in"]),
        objectives=objectives, min_level=int(raw.get("min_level", 0)),
        quest_level=int(raw.get("quest_level", 0)),
        reward_xp_difficulty=int(raw.get("reward_xp_difficulty", 0)),
        batch=str(raw.get("batch", "")),
        recommended_level=int(raw.get("recommended_level", 0)))


class QuestRuntime:
    """Executes accept -> hunt -> turn-in steps with bounded retries."""

    def __init__(self, content: QuestContent, adapter, store=None, *,
                 clock: Callable[[], float] = time.time,
                 logger: Callable[[str], None] | None = None):
        self.content = content
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.log = logger or (lambda message: None)
        self.index = self._load_index()
        self.phase = "accept"
        self.attempts = 0
        self.last_attempt = -1e9
        self.turn_in_baseline = 0
        self.approach_step = 20.0
        self.approach_npc: int | None = None
        self.approach_waypoint: int | None = None
        self.approach_recovery = False
        self.objective_key: tuple[int, int] | None = None
        self.objective_waypoint: int | None = None

    def _load_index(self) -> int:
        if self.store is None:
            return 0
        try:
            value = int(self.store.get_meta(self._index_key(), "0") or 0)
        except (TypeError, ValueError):
            return 0
        return min(max(value, 0), len(self.content.quests))

    def _index_key(self) -> str:
        return f"quest:{self.content.zone}:index"

    def _save_index(self) -> None:
        if self.store is not None:
            self.store.set_meta(self._index_key(), str(self.index))

    def current(self) -> QuestStep | None:
        if self.index >= len(self.content.quests):
            return None
        return self.content.quests[self.index]

    def observe_level(self, level: int) -> None:
        """Batch runtime may defer dangerous objectives until later levels."""

    def target_entries(self) -> frozenset[int]:
        current = self.current()
        if self.phase != "hunt" or current is None:
            return frozenset()
        return frozenset(row.entry for row in current.objectives)

    def target_position(self, position: dict | None = None) -> dict | None:
        current = self.current()
        if self.phase != "hunt" or current is None:
            return None
        for row in current.objectives:
            if row.position is not None:
                return self._objective_position(current, row, position)
        return None

    def _objective_position(self, quest: QuestStep, objective: QuestObjective,
                            position: dict | None) -> dict | None:
        anchor = objective.position
        key = (quest.quest_id, objective.entry)
        if key != self.objective_key:
            self.objective_key = key
            self.objective_waypoint = None
        if position is None or not quest.turn_in.waypoints:
            return anchor
        distance_to_anchor = math.hypot(
            float(anchor["x"]) - float(position.get("x") or 0),
            float(anchor["y"]) - float(position.get("y") or 0))
        if distance_to_anchor <= 60.0:
            self.objective_waypoint = None
            return anchor
        # NPC detours are stored objective -> NPC. Travelling back to the
        # objective follows the same measured corridor in reverse.
        path = tuple(reversed(quest.turn_in.waypoints))
        if self.objective_waypoint is not None and self.objective_waypoint >= len(path):
            return anchor
        if self.objective_waypoint is None:
            self.objective_waypoint = min(
                range(len(path)),
                key=lambda index: math.hypot(
                    float(path[index]["x"]) - float(position.get("x") or 0),
                    float(path[index]["y"]) - float(position.get("y") or 0)))
        else:
            current_waypoint = path[self.objective_waypoint]
            current_distance = math.hypot(
                float(current_waypoint["x"]) - float(position.get("x") or 0),
                float(current_waypoint["y"]) - float(position.get("y") or 0))
            if current_distance > 24.0:
                nearest = min(
                    range(len(path)),
                    key=lambda index: math.hypot(
                        float(path[index]["x"]) - float(position.get("x") or 0),
                        float(path[index]["y"]) - float(position.get("y") or 0)))
                if nearest > self.objective_waypoint:
                    self.objective_waypoint = nearest
        while self.objective_waypoint < len(path):
            waypoint = path[self.objective_waypoint]
            distance = math.hypot(
                float(waypoint["x"]) - float(position.get("x") or 0),
                float(waypoint["y"]) - float(position.get("y") or 0))
            if distance > 6.0:
                return waypoint
            self.objective_waypoint += 1
        return anchor

    def ready_to_turn_in(self, view: WorldView) -> bool:
        current = self.current()
        if current is None or view.quest is None:
            return False
        active = _active(view.quest, current.quest_id)
        return active is not None and _objectives_complete(current, active)

    def step(self, now: float, view: WorldView) -> RuntimeAction | None:
        current = self.current()
        if current is None or view.quest is None:
            return None
        if view.map_id is not None and int(view.map_id) != self.content.map_id:
            return None
        if self.phase == "accept" and current.min_level > view.level:
            # Level-gated content yields to ordinary grinding instead of
            # sending a known-unavailable accept and skipping the route.
            return None
        state = view.quest
        active = _active(state, current.quest_id)
        error = str(state.get("last_error") or "")
        if int(state.get("last_quest_id") or 0) != current.quest_id:
            error = ""

        if self.phase == "hunt":
            if active is None:
                self._reconcile()
                return None
            if _objectives_complete(current, active):
                self._begin_turn_in(state)
                return RuntimeAction("quest_objectives_complete", current.title)
            return None

        if self.phase == "turn_in":
            if int(state.get("completed") or 0) > self.turn_in_baseline:
                return self._advance(current, "quest_complete")
            if "reason 7" in error:
                return self._advance(current, "quest_already_complete")
            if "quest failed" in error and active is not None:
                self.phase = "hunt"
                self.attempts = 0
                self.last_attempt = -1e9
                return RuntimeAction("quest_incomplete", current.title)
            if self.attempts and active is None and not error and not int(
                    state.get("turning_in_quest_id") or 0):
                # The module lost its session-local row without a completion
                # packet. Reconcile at the acceptor instead of choosing a
                # reward blindly.
                self._reconcile()
                return RuntimeAction("quest_reconcile", current.title)
            if int(state.get("turning_in_quest_id") or 0) == current.quest_id:
                return RuntimeAction("quest_turn_in_wait", current.title)
            if (self.attempts >= 2 and "turn-in timed out" in error
                    and active is not None and current.objectives
                    and not active.get("objectives")):
                # Some servers silently refuse an incomplete RequestReward.
                # Unknown counters make a timeout inconclusive, so bound the
                # probe and resume killing instead of looping at the NPC forever.
                self.phase = "hunt"
                self.attempts = 0
                self.last_attempt = -1e9
                return RuntimeAction("quest_incomplete_timeout", current.title)
            turn_in = self._resolve_npc(current.turn_in, view)
            approached = self._approach(turn_in, view)
            if approached is not None:
                return approached
            if now - self.last_attempt < self._retry_delay():
                if self.attempts >= 3:
                    return None
                return RuntimeAction("quest_turn_in_wait", current.title)
            try:
                self.adapter.quest_turn_in(turn_in.guid, current.quest_id)
            except AdapterError as exc:
                self.log(f"quest {current.quest_id} turn-in failed: {exc}")
            self.turn_in_baseline = int(state.get("completed") or 0)
            self.attempts += 1
            self.last_attempt = now
            return RuntimeAction("quest_turn_in", current.title)

        # Accept is also the restart reconciliation boundary. The module adds
        # an optimistic active row; reason 7 means the server already rewarded
        # it, while reason 13 confirms that it is already in the quest log.
        if "reason 7" in error:
            return self._advance(current, "quest_already_complete")
        if active is not None:
            self.attempts = 0
            if current.objectives and not _objectives_complete(current, active):
                unknown_objectives = not active.get("objectives") and (
                    "reason 13" in error or "turn-in timed out" in error)
                if unknown_objectives:
                    # A reconnect cannot reconstruct the server's objective
                    # counters. Probe the normal turn-in boundary: completion
                    # rewards the quest, while QUEST_FAILED resumes the hunt.
                    # A prior timeout is also inconclusive, not proof of 0/8.
                    self._begin_turn_in(state)
                    return RuntimeAction("quest_reconcile_turn_in", current.title)
                self.phase = "hunt"
                return None
            self._begin_turn_in(state)
            return RuntimeAction("quest_ready", current.title)
        if error and "reason 13" not in error:
            self.log(f"quest {current.quest_id} unavailable: {error}")
            return self._advance(current, "quest_skip")
        accept = self._resolve_npc(current.accept, view)
        approached = self._approach(accept, view)
        if approached is not None:
            return approached
        if now - self.last_attempt < self._retry_delay():
            if self.attempts >= 3:
                return None
            return RuntimeAction("quest_accept_wait", current.title)
        try:
            self.adapter.quest_accept(accept.guid, current.quest_id)
        except AdapterError as exc:
            self.log(f"quest {current.quest_id} accept failed: {exc}")
        self.attempts += 1
        self.last_attempt = now
        return RuntimeAction("quest_accept", current.title)

    def _retry_delay(self) -> float:
        return 30.0 if self.attempts >= 3 else 3.0

    def _resolve_npc(self, npc: QuestNPC, view: WorldView) -> QuestNPC:
        """Refresh a respawned NPC's packed GUID from live nearby entities."""
        candidates = []
        for row in view.nearby:
            if (str(row.get("name") or "").casefold() != npc.name.casefold()
                    or row.get("guid") is None
                    or not isinstance(row.get("position"), dict)):
                continue
            try:
                guid = int(row["guid"])
                position = {key: float(row["position"][key])
                            for key in ("x", "y", "z")}
            except (KeyError, TypeError, ValueError):
                continue
            distance = math.hypot(
                position["x"] - float(npc.position["x"]),
                position["y"] - float(npc.position["y"]))
            candidates.append((distance, guid, position))
        if not candidates:
            return npc
        _, guid, position = min(candidates)
        return replace(npc, guid=guid, position=position)

    def _approach(self, npc: QuestNPC, view: WorldView) -> RuntimeAction | None:
        if not view.position:
            return RuntimeAction("quest_wait_position", npc.name)
        if self.approach_npc != npc.guid:
            self.approach_npc = npc.guid
            self.approach_waypoint = None
            self.approach_step = 20.0
            self.approach_recovery = False
        path = (npc.recovery_waypoints if self.approach_recovery
                else npc.waypoints)
        destination = npc.position
        if (self.approach_waypoint is not None
                and self.approach_waypoint < len(path)):
            destination = path[self.approach_waypoint]
        dx = float(destination["x"]) - float(view.position.get("x") or 0)
        dy = float(destination["y"]) - float(view.position.get("y") or 0)
        distance = math.hypot(dx, dy)
        if distance <= 4:
            if (self.approach_waypoint is not None
                    and self.approach_waypoint < len(path)):
                self.approach_waypoint += 1
                self.approach_step = 20.0
                return RuntimeAction("quest_waypoint", npc.name)
            self.approach_waypoint = None
            self.approach_step = 20.0
            self.approach_recovery = False
            return None
        step = min(self.approach_step, distance)
        try:
            # Estimate the next segment from the live ground height, not the
            # distant NPC's height. Open terrain uses fast 20-yard segments;
            # a rejected correction shortens the next attempt so a steep but
            # walkable terrace descent does not become a permanent cliff loop.
            self.adapter.move_to(
                float(view.position.get("x") or 0) + dx / distance * step,
                float(view.position.get("y") or 0) + dy / distance * step,
                float(view.position.get("z", destination["z"])))
        except AdapterError as exc:
            self.log(f"quest approach {npc.name} failed: {exc}")
            error = str(exc)
            if "terrain height" in error and "differs from estimated" in error:
                if self.approach_step > 4.0:
                    self.approach_step = max(4.0, min(8.0, self.approach_step / 2.0))
                elif npc.waypoints or npc.recovery_waypoints:
                    # A short segment can still point into a vertical wall.
                    # Only then fall back to measured route data; flat ground
                    # keeps the cheaper direct path. Recovery routes handle a
                    # character that combat pulled onto a ledge off the normal
                    # objective corridor.
                    recovery = bool(npc.recovery_waypoints)
                    path_changed = recovery != self.approach_recovery
                    self.approach_recovery = recovery
                    path = (npc.recovery_waypoints if self.approach_recovery
                            else npc.waypoints)
                    if self.approach_waypoint is not None and not path_changed:
                        try:
                            self.adapter.navigate(
                                float(destination["x"]), float(destination["y"]),
                                float(destination["z"]))
                            self.log(f"quest approach {npc.name}: terrain navigation")
                            return RuntimeAction("quest_approach", npc.name)
                        except AdapterError as nav_exc:
                            self.log(f"quest navigate {npc.name} failed: {nav_exc}")
                    if self.approach_waypoint is None or path_changed:
                        self.approach_waypoint = min(
                            range(len(path)),
                            key=lambda index: math.hypot(
                                float(path[index]["x"])
                                - float(view.position.get("x") or 0),
                                float(path[index]["y"])
                                - float(view.position.get("y") or 0)))
                    # Prime the detour with one short step. Re-selecting and
                    # immediately retrying 20 yards can otherwise loop forever
                    # at the same ledge without giving the safe segment a chance.
                    self.approach_step = 4.0
                    self.log(f"quest approach {npc.name}: using detour waypoints")
                else:
                    try:
                        self.adapter.navigate(
                            float(destination["x"]), float(destination["y"]),
                            float(destination["z"]))
                        self.log(f"quest approach {npc.name}: terrain navigation")
                    except AdapterError as nav_exc:
                        self.log(f"quest navigate {npc.name} failed: {nav_exc}")
        return RuntimeAction("quest_approach", npc.name)

    def _begin_turn_in(self, state: dict) -> None:
        self.phase = "turn_in"
        self.attempts = 0
        self.last_attempt = -1e9
        self.turn_in_baseline = int(state.get("completed") or 0)
        self.objective_key = None
        self.objective_waypoint = None

    def _reconcile(self) -> None:
        self.phase = "accept"
        self.attempts = 0
        self.last_attempt = -1e9

    def _advance(self, current: QuestStep, action: str) -> RuntimeAction:
        self.log(f"quest {current.quest_id}: {action}")
        self.index += 1
        self._save_index()
        self._reconcile()
        return RuntimeAction(action, current.title)
class BatchQuestRuntime(QuestRuntime):
    """Accept a contiguous quest batch, merge hunts, then turn in each member.

    The persisted index remains at the first unfinished batch member. A restart
    therefore reconciles ALREADY_DONE/ALREADY_ON at normal NPC boundaries rather
    than trusting session-local objective counters or inventing completion.
    """

    def __init__(self, content: QuestContent, adapter, store=None, **kwargs):
        super().__init__(content, adapter, store, **kwargs)
        self.done: set[int] = set()
        self.accepted: set[int] = set()
        self.accept_id: int | None = None
        self.turn_in_id: int | None = None
        self.waiting_for_level = False
        self.observed: dict[int, dict] = {}
        self.level: int | None = None

    def observe_level(self, level: int) -> None:
        self.level = level

    def _quest_deferred(self, quest: QuestStep) -> bool:
        return self.level is not None and (
            self.level < quest.recommended_level
            or quest.quest_level > self.level + 2)

    def _batch(self) -> tuple[QuestStep, ...]:
        first = super().current()
        if first is None:
            return ()
        label = first.batch or f"quest:{first.quest_id}"
        rows = []
        for quest in self.content.quests[self.index:]:
            if (quest.batch or f"quest:{quest.quest_id}") != label:
                break
            rows.append(quest)
        return tuple(rows)

    def _pending(self) -> tuple[QuestStep, ...]:
        return tuple(row for row in self._batch() if row.quest_id not in self.done)

    def current(self) -> QuestStep | None:
        if self.phase == "turn_in" and self.turn_in_id is not None:
            return next((row for row in self._batch()
                         if row.quest_id == self.turn_in_id), None)
        pending = self._pending()
        if self.accept_id is not None:
            selected = next((row for row in pending
                             if row.quest_id == self.accept_id), None)
            if selected is not None:
                return selected
        return pending[0] if pending else None

    def target_entries(self) -> frozenset[int]:
        if self.waiting_for_level and not (self.accepted - self.done):
            current = self.current()
            if current is not None and self._quest_deferred(current):
                return frozenset()
            return (frozenset(row.entry for row in current.objectives)
                    if current is not None else frozenset())
        if self.phase != "hunt":
            return frozenset()
        return frozenset(objective.entry for quest in self._pending()
                         if quest.quest_id in self.accepted
                         and not self._quest_deferred(quest)
                         for objective in quest.objectives
                         if self._objective_pending(quest, objective))

    def target_position(self, position: dict | None = None) -> dict | None:
        if self.waiting_for_level and not (self.accepted - self.done):
            current = self.current()
            if current is not None and self._quest_deferred(current):
                return None
            if current is not None:
                for objective in current.objectives:
                    if objective.position is not None:
                        return self._objective_position(
                            current, objective, position)
            return None
        if self.phase != "hunt":
            return None
        for quest in self._pending():
            if (quest.quest_id not in self.accepted
                    or self._quest_deferred(quest)):
                continue
            for objective in quest.objectives:
                if (objective.position is not None
                        and self._objective_pending(quest, objective)):
                    return self._objective_position(quest, objective, position)
        return None

    def _objective_pending(self, quest: QuestStep,
                           objective: QuestObjective) -> bool:
        active = self.observed.get(quest.quest_id)
        if active is None or not active.get("objectives"):
            return True
        for row in active["objectives"]:
            if int(row.get("entry") or 0) == objective.entry:
                return int(row.get("current") or 0) < objective.required
        return True

    def ready_to_turn_in(self, view: WorldView) -> bool:
        if view.quest is None:
            return False
        return any(_objectives_complete(quest, active)
                   for quest in self._pending()
                   if (active := _active(view.quest, quest.quest_id)) is not None)

    def step(self, now: float, view: WorldView) -> RuntimeAction | None:
        current = self.current()
        if current is None or view.quest is None:
            return self._finish_batch_if_needed()
        if view.map_id is not None and int(view.map_id) != self.content.map_id:
            return None
        self.waiting_for_level = False
        state = view.quest

        if self.phase != "turn_in":
            for quest in self._pending():
                active = _active(state, quest.quest_id)
                if active is not None:
                    self.accepted.add(quest.quest_id)
                    self.observed[quest.quest_id] = active
                if active is not None and _objectives_complete(quest, active):
                    self._start_turn_in(quest, state)
                    return RuntimeAction("quest_objectives_complete", quest.title)

        if self.phase == "hunt":
            active_pending = [
                quest for quest in self._pending()
                if _active(state, quest.quest_id) is not None]
            if (active_pending
                    and all(self._quest_deferred(quest)
                            for quest in active_pending)):
                return None
            unlocked = next((quest for quest in self._pending()
                             if quest.quest_id not in self.accepted
                             and quest.min_level <= view.level), None)
            if unlocked is not None:
                self.phase = "accept"
                self.accept_id = unlocked.quest_id
                self.attempts = 0
                self.last_attempt = -1e9
                return RuntimeAction("quest_batch_accept_next", unlocked.title)
            if not any(_active(state, quest.quest_id) is not None
                       for quest in self._pending()):
                self.phase = "accept"
                self.accepted.clear()
                self.observed.clear()
                self.attempts = 0
                self.last_attempt = -1e9
                return RuntimeAction("quest_reconcile", current.title)
            return None

        if self.phase == "turn_in":
            return self._turn_in_step(now, view, current, state)

        error = str(state.get("last_error") or "")
        if int(state.get("last_quest_id") or 0) != current.quest_id:
            error = ""
        active = _active(state, current.quest_id)
        if "reason 7" in error:
            return self._mark_done(current, "quest_already_complete")
        if active is not None:
            self.accepted.add(current.quest_id)
            self.attempts = 0
            if current.objectives and not _objectives_complete(current, active):
                unknown = not active.get("objectives") and (
                    "reason 13" in error or "turn-in timed out" in error)
                if unknown:
                    self._start_turn_in(current, state)
                    return RuntimeAction("quest_reconcile_turn_in", current.title)
                return self._accept_next(current)
            self._start_turn_in(current, state)
            return RuntimeAction("quest_ready", current.title)
        if error and "reason 13" not in error:
            self.log(f"quest {current.quest_id} unavailable: {error}")
            return self._mark_done(current, "quest_skip")
        if current.min_level > view.level:
            self.waiting_for_level = True
            if self.accepted - self.done:
                self.phase = "hunt"
                return RuntimeAction("quest_batch_hunt", current.title)
            return None
        if self._quest_deferred(current):
            self.waiting_for_level = True
            return None
        accept = self._resolve_npc(current.accept, view)
        approached = self._approach(accept, view)
        if approached is not None:
            return approached
        if now - self.last_attempt < self._retry_delay():
            if self.attempts >= 3:
                return None
            return RuntimeAction("quest_accept_wait", current.title)
        try:
            self.adapter.quest_accept(accept.guid, current.quest_id)
        except AdapterError as exc:
            self.log(f"quest {current.quest_id} accept failed: {exc}")
        self.attempts += 1
        self.last_attempt = now
        return RuntimeAction("quest_accept", current.title)

    def _accept_next(self, current: QuestStep) -> RuntimeAction:
        pending = self._pending()
        following = next((row for row in pending
                          if row.quest_id != current.quest_id
                          and row.quest_id not in self.accepted), None)
        if following is not None:
            self.accept_id = following.quest_id
            self.attempts = 0
            self.last_attempt = -1e9
            return RuntimeAction("quest_batch_accept_next", following.title)
        self.phase = "hunt"
        self.attempts = 0
        self.last_attempt = -1e9
        return RuntimeAction("quest_batch_hunt", current.title)

    def _start_turn_in(self, quest: QuestStep, state: dict) -> None:
        self.turn_in_id = quest.quest_id
        self._begin_turn_in(state)

    def _turn_in_step(self, now: float, view: WorldView,
                      current: QuestStep, state: dict) -> RuntimeAction | None:
        active = _active(state, current.quest_id)
        error = str(state.get("last_error") or "")
        if int(state.get("last_quest_id") or 0) != current.quest_id:
            error = ""
        if int(state.get("completed") or 0) > self.turn_in_baseline:
            return self._mark_done(current, "quest_complete")
        if "reason 7" in error:
            return self._mark_done(current, "quest_already_complete")
        if "quest failed" in error and active is not None:
            self.phase = "hunt"
            self.turn_in_id = None
            self.attempts = 0
            self.last_attempt = -1e9
            return RuntimeAction("quest_incomplete", current.title)
        if self.attempts and active is None and not error and not int(
                state.get("turning_in_quest_id") or 0):
            self.phase = "accept"
            self.turn_in_id = None
            self.attempts = 0
            self.last_attempt = -1e9
            return RuntimeAction("quest_reconcile", current.title)
        if int(state.get("turning_in_quest_id") or 0) == current.quest_id:
            return RuntimeAction("quest_turn_in_wait", current.title)
        if (self.attempts >= 2 and "turn-in timed out" in error
                and active is not None and current.objectives
                and not active.get("objectives")):
            # A silently refused incomplete reward must not strand a reconnect
            # in an endless probe loop when the server counters are unavailable.
            self.phase = "hunt"
            self.turn_in_id = None
            self.attempts = 0
            self.last_attempt = -1e9
            return RuntimeAction("quest_incomplete_timeout", current.title)
        turn_in = self._resolve_npc(current.turn_in, view)
        approached = self._approach(turn_in, view)
        if approached is not None:
            return approached
        if now - self.last_attempt < self._retry_delay():
            if self.attempts >= 3:
                return None
            return RuntimeAction("quest_turn_in_wait", current.title)
        try:
            self.adapter.quest_turn_in(turn_in.guid, current.quest_id)
        except AdapterError as exc:
            self.log(f"quest {current.quest_id} turn-in failed: {exc}")
        self.turn_in_baseline = int(state.get("completed") or 0)
        self.attempts += 1
        self.last_attempt = now
        return RuntimeAction("quest_turn_in", current.title)

    def _mark_done(self, current: QuestStep, action: str) -> RuntimeAction:
        self.log(f"quest {current.quest_id}: {action}")
        self.done.add(current.quest_id)
        self.accepted.discard(current.quest_id)
        self.observed.pop(current.quest_id, None)
        self.turn_in_id = None
        self.accept_id = None
        self.phase = "accept"
        self.attempts = 0
        self.last_attempt = -1e9
        if not self._pending():
            batch = self._batch()
            if batch:
                self.index += len(batch)
                self._save_index()
            self.done.clear()
            self.accepted.clear()
            self.observed.clear()
        return RuntimeAction(action, current.title)

    def _finish_batch_if_needed(self) -> RuntimeAction | None:
        if self._pending() or not self._batch():
            return None
        self.index += len(self._batch())
        self._save_index()
        self.done.clear()
        self.accepted.clear()
        self.observed.clear()
        self.phase = "accept"
        self.turn_in_id = None
        return RuntimeAction("quest_batch_complete", self.content.zone)

def _active(state: dict, quest_id: int) -> dict | None:
    for row in state.get("active") or []:
        if int(row.get("quest_id") or 0) == quest_id:
            return row
    return None


def _objectives_complete(quest: QuestStep, active: dict) -> bool:
    if active.get("complete"):
        return True
    if not quest.objectives:
        return True
    observed = {int(row.get("entry") or 0): int(row.get("current") or 0)
                for row in active.get("objectives") or []}
    return all(observed.get(row.entry, 0) >= row.required
               for row in quest.objectives)
