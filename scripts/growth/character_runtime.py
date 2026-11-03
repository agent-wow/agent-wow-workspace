"""Durable class training, talents, crafting and gathering policy."""
from __future__ import annotations

import json
import math
import time
from typing import Callable

from .actions import DurableActionExecutor
from .adapter import AdapterError, CharacterState, SkillLine, WorldView
from .professions import ProfessionContent
from .profiles import ClassProfile, TalentChoice
from .social import RuntimeAction


class CharacterGrowthRuntime:
    """Runs at the town boundary, never during combat or recovery.

    Mutating requests have stable checkpoint intents. Lost replies are resolved
    from spellbook, talent, skill, inventory and nearby-object facts; a timeout
    never becomes an automatic purchase or cast retry.
    """

    def __init__(
        self, profile: ClassProfile, content: ProfessionContent, adapter,
        store, *, clock: Callable[[], float] = time.time,
    ):
        self.profile = profile
        self.content = content
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.actions = DurableActionExecutor(store, clock=clock, unknown_timeout=45.0)
        self._open_attempts: dict[int, float] = {}
        self._open_failures: dict[int, int] = {}

    def step(self, now: float, view: WorldView, role: str) -> RuntimeAction | None:
        resumed = self._resume(now)
        if resumed is not None:
            return resumed
        try:
            state = self.adapter.get_character_state()
            skills = {row.id: row for row in self.adapter.get_skills()}
            spells = self.adapter.get_spells()
            inventory = self.adapter.get_inventory()
        except AdapterError:
            return None
        talent = self._talent(now, view.level, state, role)
        if talent is not None:
            return talent
        trained = self._training(now, view, state, skills, spells, inventory)
        if trained is not None:
            return trained
        crafted = self._crafting(now, view, skills, spells, inventory)
        if crafted is not None:
            return crafted
        return self._gathering(now, view, skills)

    # --------------------------------------------------------------- recovery

    def _resume(self, now: float) -> RuntimeAction | None:
        for status in ("sending", "sent", "unknown"):
            for intent in self.store.intents(state=status):
                key = str(intent["intent_key"])
                if not key.startswith("character:"):
                    continue
                kind = str(intent["kind"])
                payload = intent["payload"]
                result = self.actions.run(
                    key, kind, payload, self._reject_redispatch,
                    lambda kind=kind, payload=payload: self._readback(kind, payload))
                if result.state == "abandoned" and payload.get("retry_key"):
                    self._cool_down(str(payload["retry_key"]), now)
                return RuntimeAction(kind, result.state)
        return None

    @staticmethod
    def _reject_redispatch():
        raise AdapterError("an in-flight character action cannot be redispatched")

    def _readback(self, kind: str, payload: dict) -> bool | None:
        try:
            if kind == "trainer_buy":
                spells = self.adapter.get_spells()
                state = self.adapter.get_character_state()
                # Money is read as part of reconciliation, but a unchanged
                # balance alone cannot prove failure while packets are in flight.
                self.adapter.get_inventory()
                spell_id = int(payload["spell_id"])
                skill_id = int(payload.get("skill_id") or 0)
                if spell_id in spells:
                    return True
                if skill_id and payload.get("establish_skill") and any(
                        row.id == skill_id for row in self.adapter.get_skills()):
                    return True
                if state.last_buy_spell_id == spell_id:
                    if state.last_buy_succeeded:
                        return True
                    if state.last_buy_result_known:
                        return False
                return None
            if kind == "talent_learn":
                state = self.adapter.get_character_state()
                if state.has_talents and state.talents.get(int(payload["talent_id"]), -1) >= int(payload["requested_rank"]):
                    return True
                return None
            if kind == "profession_craft":
                skills = {row.id: row for row in self.adapter.get_skills()}
                inventory = self.adapter.get_inventory()
                current = skills.get(int(payload["skill_id"]))
                if current and current.value > int(payload["skill_value"]):
                    self._record_skill_up(current, int(payload["skill_value"]))
                    return True
                required = payload.get("required_materials") or {}
                if required and all(
                        inventory.count_of(int(item)) == int(count) - int(required[item])
                        for item, count in payload["materials"].items()):                    return True
                return None
            if kind == "profession_gather":
                skills = {row.id: row for row in self.adapter.get_skills()}
                current = skills.get(int(payload["skill_id"]))
                if current and current.value > int(payload["skill_value"]):
                    self._record_skill_up(current, int(payload["skill_value"]))
                    return True
                current_self = self.adapter.get_self()
                if str(current_self.get("epoch")) != str(payload.get("epoch")):
                    return None
                objects = self.adapter.get_game_objects()
                if all(int(row.get("guid") or 0) != int(payload["guid"]) for row in objects):
                    return True
                return None
        except (AdapterError, TypeError, ValueError):
            return None
        return None

    def _record_skill_up(self, skill: SkillLine, previous: int) -> None:
        self.store.set_meta("character:last_skill_up", json.dumps({
            "skill_id": skill.id, "name": skill.name,
            "previous": previous, "value": skill.value,
        }, sort_keys=True))

    # ----------------------------------------------------------------- talents

    def _talent(self, now: float, level: int, state: CharacterState, role: str) -> RuntimeAction | None:
        if not state.has_talents or state.free_talent_points < 1:
            return None
        for choice in self.profile.talents.get(role, ()):
            if level < choice.min_level or not self._talent_eligible(choice, state):
                continue
            requested = state.talents.get(choice.talent_id, -1) + 1
            payload = {"talent_id": choice.talent_id, "requested_rank": requested}
            key = f"character:talent:{role}:{choice.talent_id}:{requested}"
            if self._terminal(key):
                continue
            result = self.actions.run(
                key, "talent_learn", payload,
                lambda: self.adapter.learn_talent(choice.talent_id, requested),
                lambda: self._readback("talent_learn", payload))
            return RuntimeAction("talent_learn", result.state)
        return None

    def _talent_eligible(self, choice: TalentChoice, state: CharacterState) -> bool:
        # The worker's live level is checked by the caller's content eligibility;
        # rank zero is a learned first point and must not be treated as absent.
        if choice.required_talent_id:
            if state.talents.get(choice.required_talent_id, -1) < choice.required_rank:
                return False
        # The content pack records each talent's actual maximum zero-based rank.
        return state.talents.get(choice.talent_id, -1) < choice.max_rank

    # ---------------------------------------------------------------- training

    def _training(self, now, view, state, skills, spells, inventory) -> RuntimeAction | None:
        desired = {skill.spell_id: 0 for skill in self.profile.skills}
        desired.update({row.trainer_spell_id: row.skill_id for row in self.content.professions})
        desired.update({row.spell_id: row.skill_id for row in self.content.recipes})
        for row in state.trainer_spells if state.trainer_open else ():
            skill_id = desired.get(row.spell_id)
            if skill_id is None or row.spell_id in spells or row.usable != 0:
                continue
            if row.required_level > view.level:
                continue
            if row.required_skill_line:
                required = skills.get(row.required_skill_line)
                if required is None or required.value < row.required_skill_rank:
                    continue
            if any(ability and ability not in spells for ability in row.required_abilities):
                continue
            if inventory.money < row.money_cost + self.profile.training_reserve:
                continue
            if row.profession_point_cost and skill_id not in skills:
                primary = sum(1 for skill in skills.values() if skill.profession)
                if primary >= 2:
                    continue
            payload = {
                "trainer_guid": state.trainer_guid, "spell_id": row.spell_id,
                "skill_id": skill_id, "money": inventory.money,
                "expected_cost": row.money_cost,
                "establish_skill": skill_id != 0 and skill_id not in skills,
            }
            key = f"character:trainer:{state.trainer_guid}:spell:{row.spell_id}"
            if self._terminal(key):
                continue
            result = self.actions.run(
                key, "trainer_buy", payload,
                lambda: self.adapter.buy_spell(state.trainer_guid, row.spell_id),
                lambda: self._readback("trainer_buy", payload))
            return RuntimeAction("trainer_buy", result.state)
        return self._open_trainer(now, view, state, spells)

    def _open_trainer(self, now, view, state, spells) -> RuntimeAction | None:
        missing_class = any(skill.spell_id not in spells for skill in self.profile.skills)
        missing_profession = any(row.trainer_spell_id not in spells
                                 for row in self.content.professions)
        missing_recipe = any(row.spell_id not in spells for row in self.content.recipes)
        categories = []
        if missing_class:
            categories.append(0x30)
        if missing_profession or missing_recipe:
            categories.append(self.content.trainer_npc_flags)
        candidates = []
        for category, required_flags in enumerate(categories):
            for npc in view.nearby:
                guid = int(npc.get("guid") or 0)
                if guid <= 0 or self._failed(f"trainer:{guid}", now):
                    continue
                if int(npc.get("npc_flags") or 0) & required_flags != required_flags:
                    continue
                if not npc.get("has_position"):
                    continue
                distance = _distance(view.position, npc.get("position") or {})
                if distance <= self.content.approach_distance:
                    # Once an open trainer has no actionable rows, prefer another
                    # nearby trainer before refreshing the same server snapshot.
                    current = int(state.trainer_open and state.trainer_guid == guid)
                    candidates.append((current, category, distance, guid, npc))
        if not candidates:
            return None
        _, _, distance, guid, npc = min(candidates, key=lambda row: row[:4])
        if distance > self.content.interaction_distance:
            pos = npc["position"]
            try:
                self.adapter.move_to(float(pos.get("x") or 0), float(pos.get("y") or 0),
                                     float(pos.get("z") or 0))
            except AdapterError:
                self._cool_down(f"trainer:{guid}", now)
                return None
            return RuntimeAction("trainer_approach", str(guid))
        refresh_seconds = 15.0 if state.trainer_open and state.trainer_guid == guid else 5.0
        if now - self._open_attempts.get(guid, -1e9) < refresh_seconds:
            return None
        self._open_attempts[guid] = now
        try:
            self.adapter.open_trainer(guid)
        except AdapterError:
            self._cool_down(f"trainer:{guid}", now)
            self._open_attempts.pop(guid, None)
            self._open_failures.pop(guid, None)
            return None
        if state.trainer_open and state.trainer_guid == guid:
            return RuntimeAction("trainer_refresh", str(guid))
        attempts = self._open_failures.get(guid, 0) + 1
        if attempts >= 3:
            self._cool_down(f"trainer:{guid}", now)
            self._open_attempts.pop(guid, None)
            self._open_failures.pop(guid, None)
        else:
            self._open_failures[guid] = attempts
        return RuntimeAction("trainer_open", str(guid))

    # -------------------------------------------------------------- professions

    def _crafting(self, now, view, skills, spells, inventory) -> RuntimeAction | None:
        for recipe in self.content.recipes:
            skill = skills.get(recipe.skill_id)
            if skill is None or recipe.spell_id not in spells:
                continue
            if skill.value < recipe.min_skill or skill.value >= recipe.target_skill or skill.value >= skill.max:
                continue
            if self._failed(f"recipe:{recipe.spell_id}", now):
                continue
            if any(inventory.count_of(item) < count for item, count in recipe.materials.items()):
                continue
            sequence = self._next_sequence()
            payload = {
                "spell_id": recipe.spell_id, "skill_id": recipe.skill_id,
                "skill_value": skill.value, "materials": {
                    str(item): inventory.count_of(item) for item in recipe.materials},
                "retry_key": f"recipe:{recipe.spell_id}", "sequence": sequence,
                "required_materials": {
                    str(item): int(count) for item, count in recipe.materials.items()},
            }
            key = f"character:craft:{recipe.spell_id}:{sequence}"
            result = self.actions.run(
                key, "profession_craft", payload,
                lambda: self.adapter.cast(
                    recipe.spell_id, 0, preserve_empty_target=True),
                lambda: self._readback("profession_craft", payload))
            if result.state == "abandoned":
                self._cool_down(payload["retry_key"], now)
            return RuntimeAction("profession_craft", result.state)
        return None

    def _gathering(self, now, view, skills) -> RuntimeAction | None:
        profession = next(row for row in self.content.professions if row.kind == "gathering")
        skill = skills.get(profession.skill_id)
        if skill is None or skill.value >= skill.max:
            return None
        try:
            objects = self.adapter.get_game_objects()
        except AdapterError:
            return None
        candidates = []
        for row in objects:
            entry = int(row.get("entry") or 0)
            guid = int(row.get("guid") or 0)
            if entry not in self.content.gathering_entries or guid <= 0:
                continue
            if self._failed(f"node:{guid}", now):
                continue
            distance = _distance(view.position, row.get("position") or {})
            if distance <= self.content.approach_distance:
                candidates.append((distance, guid, row))
        if not candidates:
            return None
        distance, guid, obj = min(candidates, key=lambda row: (row[0], row[1]))
        if distance > self.content.interaction_distance:
            pos = obj.get("position") or {}
            try:
                self.adapter.move_to(float(pos.get("x") or 0), float(pos.get("y") or 0),
                                     float(pos.get("z") or 0))
            except AdapterError:
                self._cool_down(f"node:{guid}", now)
                return None
            return RuntimeAction("gather_approach", str(guid))
        sequence = self._next_sequence()
        payload = {
            "guid": guid, "skill_id": skill.id, "skill_value": skill.value,
            "retry_key": f"node:{guid}", "sequence": sequence,
            "epoch": view.epoch,
        }
        # Herbalism is an open-lock cast at the node. Its completion opens the
        # loot window for the existing auto-loot worker.
        result = self.actions.run(
            f"character:gather:{guid}:{sequence}", "profession_gather", payload,
            lambda: self.adapter.cast(
                profession.trainer_spell_id, guid, target_is_game_object=True),
            lambda: self._readback("profession_gather", payload))
        if result.state == "abandoned":
            self._cool_down(payload["retry_key"], now)
        return RuntimeAction("profession_gather", result.state)

    def _next_sequence(self) -> int:
        value = int(self.store.get_meta("character:sequence", "0") or 0) + 1
        self.store.set_meta("character:sequence", str(value))
        return value

    def _cool_down(self, key: str, now: float) -> None:
        deadline = now + self.content.retry_cooldown_seconds
        self.store.set_meta(f"character:cooldown:{key}", str(deadline))

    def _failed(self, key: str, now: float) -> bool:
        try:
            deadline = float(self.store.get_meta(
                f"character:cooldown:{key}", "0") or 0)
        except (TypeError, ValueError):
            return False
        return deadline > now

    def _terminal(self, intent_key: str) -> bool:
        intent = self.store.intent(intent_key)
        return bool(intent and intent.get("state") in {"confirmed", "failed", "abandoned"})


def _distance(a: dict, b: dict) -> float:
    if not a or not b:
        return math.inf
    return math.hypot(float(a.get("x") or 0) - float(b.get("x") or 0),
                      float(a.get("y") or 0) - float(b.get("y") or 0))
