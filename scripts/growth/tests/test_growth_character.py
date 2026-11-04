from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from _support import ROOT  # noqa: F401
from _growth_support import WARRIOR, make_view

from scripts.growth.adapter import AdapterError, CharacterState, SkillLine, TrainerSpell
from scripts.growth.character_runtime import CharacterGrowthRuntime
from scripts.growth.items import InventoryState, Item
from scripts.growth.professions import load_professions
from scripts.growth.profiles import TalentChoice
from scripts.growth.store import CheckpointStore


class FakeCharacterAdapter:
    def __init__(self):
        self.state = CharacterState()
        self.skills = ()
        self.spells = {78: False, 6343: False}
        self.inventory = InventoryState(money=1000)
        self.objects = []
        self.calls = []
        self.lose_buy = False
        self.lose_talent = False
        self.lose_gather = False
        self.empty_target_casts = 0
        self.epoch = "1"

    def get_self(self):
        return {"epoch": self.epoch}

    def get_character_state(self):
        self.calls.append(("get_character_state", ()))
        return self.state

    def get_skills(self):
        self.calls.append(("get_skills", ()))
        return self.skills

    def get_spells(self):
        self.calls.append(("get_spells", ()))
        return self.spells

    def get_inventory(self):
        self.calls.append(("get_inventory", ()))
        return self.inventory

    def buy_spell(self, trainer_guid, spell_id):
        self.calls.append(("buy_spell", (trainer_guid, spell_id)))
        self.spells[spell_id] = False
        if self.lose_buy:
            raise AdapterError("lost reply")
        return self.state

    def learn_talent(self, talent_id, requested_rank):
        self.calls.append(("learn_talent", (talent_id, requested_rank)))
        talents = dict(self.state.talents)
        talents[talent_id] = requested_rank
        self.state = replace(self.state, talents=talents,
                             free_talent_points=self.state.free_talent_points - 1)
        if self.lose_talent:
            raise AdapterError("lost reply")
        return self.state

    def open_trainer(self, guid):
        self.calls.append(("open_trainer", (guid,)))
        self.state = replace(self.state, trainer_guid=guid, trainer_open=True)
        return self.state

    def move_to(self, x, y, z):
        self.calls.append(("move_to", (x, y, z)))
        return {}

    def cast(self, spell_id, target_guid, *, preserve_empty_target=False,
             target_is_game_object=False):
        self.calls.append(("cast", (spell_id, target_guid)))
        self.empty_target_casts += int(preserve_empty_target)
        if target_is_game_object:
            self.use_game_object(target_guid)
            return {}
        recipe = next(row for row in load_professions().recipes if row.spell_id == spell_id)
        items = []
        for item in self.inventory.items:
            count = item.count - recipe.materials.get(item.item_id, 0)
            if count:
                items.append(replace(item, count=count))
        self.inventory = replace(self.inventory, items=tuple(items))
        self.skills = tuple(replace(row, value=row.value + 1)
                            if row.id == recipe.skill_id else row for row in self.skills)
        return {}

    def get_game_objects(self):
        return list(self.objects)

    def use_game_object(self, guid):
        self.calls.append(("use_game_object", (guid,)))
        # Herbalism opens its loot window when the gathering spell completes;
        # the loot module's normal auto-loot worker consumes that window.
        self.objects = [row for row in self.objects if int(row["guid"]) != guid]
        self.skills = tuple(replace(row, value=row.value + 1)
                            if row.id == 182 else row for row in self.skills)
        if self.lose_gather:
            raise AdapterError("lost reply")
        return self.state

    def loot(self, guid):
        self.calls.append(("loot", (guid,)))
        self.objects = [row for row in self.objects if int(row["guid"]) != guid]
        self.skills = tuple(replace(row, value=row.value + 1)
                            if row.id == 182 else row for row in self.skills)
        if self.lose_gather:
            raise AdapterError("lost reply")
        return {}


class CharacterGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CheckpointStore(f"{self.temp.name}/growth.db").open()
        self.adapter = FakeCharacterAdapter()
        self.content = load_professions()
        self.clock = [100.0]
        self.time_patch = patch("scripts.growth.store.time.time", lambda: self.clock[0])
        self.time_patch.start()
        self.runtime = CharacterGrowthRuntime(
            WARRIOR, self.content, self.adapter, self.store,
            clock=lambda: self.clock[0])
        self.view = make_view(level=10)

    def tearDown(self):
        self.store.close()
        self.time_patch.stop()
        self.temp.cleanup()

    def test_lost_trainer_reply_readback_never_buys_twice(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(78, 0, 10, 0, 1, 0, 0),))
        self.adapter.spells = {6343: False, 2366: False, 2259: False, 3273: False}
        self.adapter.lose_buy = True
        action = self.runtime.step(100, self.view, "tank")
        self.assertEqual(action.detail, "unknown")
        action = self.runtime.step(101, self.view, "tank")
        self.assertEqual(action.detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 78))), 1)

    def test_unusable_level_and_budget_rows_are_not_purchased(self):
        rows = (
            TrainerSpell(78, 1, 1, 0, 1, 0, 0),
            TrainerSpell(6343, 0, 1, 0, 20, 0, 0),
        )
        self.adapter.state = CharacterState(9, True, rows)
        self.adapter.spells = {2366: False, 2259: False, 3273: False}
        self.adapter.inventory = InventoryState(money=0)
        self.assertIsNone(self.runtime.step(100, self.view, "tank"))
        self.assertFalse([row for row in self.adapter.calls if row[0] == "buy_spell"])

    def test_training_reserve_boundary_is_enforced(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(78, 0, 10, 0, 1, 0, 0),))
        self.adapter.spells = {6343: False, 2366: False, 2259: False,
                               3273: False, 2330: False, 3275: False}
        self.adapter.inventory = InventoryState(money=59)
        self.assertIsNone(self.runtime.step(100, self.view, "tank"))
        self.assertFalse([row for row in self.adapter.calls if row[0] == "buy_spell"])
        self.adapter.inventory = InventoryState(money=60)
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 78))), 1)

    def test_zero_valued_trainer_failure_is_not_mistaken_for_success(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(78, 0, 10, 0, 1, 0, 0),))
        self.adapter.spells = {2366: False, 2259: False, 3273: False, 2330: False, 3275: False}

        def refuse(trainer_guid, spell_id):
            self.adapter.calls.append(("buy_spell", (trainer_guid, spell_id)))
            self.adapter.state = replace(
                self.adapter.state, last_buy_spell_id=spell_id,
                last_buy_result=0, last_buy_result_known=True,
                last_buy_succeeded=False)
            raise AdapterError("lost reply")

        self.adapter.buy_spell = refuse
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "failed")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 78))), 1)

    def test_lost_recipe_purchase_is_not_confirmed_by_existing_skill_line(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(2330, 0, 10, 0, 1, 171, 1),))
        self.adapter.skills = (SkillLine(171, "Alchemy", value=1, max=75),)
        self.adapter.spells = {skill.spell_id: False for skill in WARRIOR.skills}
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})

        def lose_without_learning(trainer_guid, spell_id):
            self.adapter.calls.append(("buy_spell", (trainer_guid, spell_id)))
            raise AdapterError("lost")

        self.adapter.buy_spell = lose_without_learning
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "unknown")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 2330))), 1)

    def test_recipe_is_trained_and_terminal_failure_does_not_block_other_spells(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(2330, 0, 10, 0, 1, 171, 1),))
        self.adapter.skills = (SkillLine(171, "Alchemy", value=1, max=75),)
        self.adapter.spells = {2259: False}
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 2330))), 1)

        payload = {"trainer_guid": 9, "spell_id": 78, "skill_id": 0,
                   "money": 1000, "expected_cost": 10}
        key = "character:trainer:9:spell:78"
        self.store.begin_intent(key, "trainer_buy", payload)
        self.store.begin_send(key)
        self.store.mark_sent(key)
        self.store.mark_failed(key, "server refused")
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(
                TrainerSpell(78, 0, 10, 0, 1, 0, 0),
                TrainerSpell(6343, 0, 10, 0, 1, 0, 0),
            ))
        self.adapter.spells = {}
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 78))), 0)
        self.assertEqual(self.adapter.calls.count(("buy_spell", (9, 6343))), 1)

    def test_missing_recipe_reopens_profession_trainer(self):
        self.adapter.spells = {skill.spell_id: False for skill in WARRIOR.skills}
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 2330: False})
        view = replace(self.view, nearby=({
            "guid": 44, "npc_flags": 64, "has_position": True,
            "position": {"x": 1, "y": 0, "z": 0},
        },))
        action = self.runtime.step(100, view, "tank")
        self.assertEqual(action.action, "trainer_open")
        self.assertIn(("open_trainer", (44,)), self.adapter.calls)

    def test_future_class_spells_do_not_pull_a_low_level_character_to_trainer(self):
        self.adapter.spells = {
            78: False, 2366: False, 2259: False, 3273: False,
            2330: False, 3275: False,
        }
        view = replace(self.view,
                       self_state={**self.view.self_state, "level": 1},
                       nearby=({
            "guid": 44, "npc_flags": 0x30, "has_position": True,
            "position": {"x": 10, "y": 0, "z": 0},
        },))
        self.assertIsNone(self.runtime.step(100, view, "tank"))
        self.assertNotIn(("move_to", (10, 0, 0)), self.adapter.calls)
        self.assertNotIn(("open_trainer", (44,)), self.adapter.calls)

    def test_talent_rank_zero_and_lost_reply_are_reconciled(self):
        profile = replace(WARRIOR, talents={"tank": (TalentChoice(1001, 10),)})
        runtime = CharacterGrowthRuntime(
            profile, self.content, self.adapter, self.store,
            clock=lambda: self.clock[0])
        self.adapter.state = CharacterState(has_talents=True, free_talent_points=1)
        self.adapter.lose_talent = True
        self.assertEqual(runtime.step(100, self.view, "tank").detail, "unknown")
        self.assertEqual(runtime.step(101, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.state.talents[1001], 0)
        self.assertEqual(self.adapter.calls.count(("learn_talent", (1001, 0))), 1)

    def test_role_plan_does_not_override_another_role(self):
        profile = replace(WARRIOR, talents={
            "tank": (TalentChoice(1001, 10),),
            "dps": (TalentChoice(2001, 10),),
        })
        runtime = CharacterGrowthRuntime(profile, self.content, self.adapter, self.store)
        self.adapter.state = CharacterState(
            has_talents=True, free_talent_points=1, talents={1001: 2})
        runtime.step(100, self.view, "dps")
        self.assertIn(("learn_talent", (2001, 0)), self.adapter.calls)
        self.assertNotIn(("learn_talent", (1001, 3)), self.adapter.calls)

    def test_trainer_open_transport_failure_cools_down_without_raising(self):
        self.adapter.spells = {skill.spell_id: False for skill in WARRIOR.skills}
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 2330: False})
        view = replace(self.view, nearby=({
            "guid": 44, "npc_flags": 64, "has_position": True,
            "position": {"x": 1, "y": 0, "z": 0},
        },))

        def refuse_open(guid):
            self.adapter.calls.append(("open_trainer", (guid,)))
            raise AdapterError("lost")

        self.adapter.open_trainer = refuse_open
        self.assertIsNone(self.runtime.step(100, view, "tank"))
        self.assertIsNone(self.runtime.step(106, view, "tank"))
        self.assertEqual(self.adapter.calls.count(("open_trainer", (44,))), 1)

    def test_unanswered_trainer_opens_enter_a_bounded_cooldown(self):
        self.adapter.spells = {skill.spell_id: False for skill in WARRIOR.skills}
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 2330: False})
        view = replace(self.view, nearby=({
            "guid": 44, "npc_flags": 64, "has_position": True,
            "position": {"x": 1, "y": 0, "z": 0},
        },))

        def no_list(guid):
            self.adapter.calls.append(("open_trainer", (guid,)))
            return self.adapter.state

        self.adapter.open_trainer = no_list
        for now in (100, 105, 110):
            self.assertEqual(self.runtime.step(now, view, "tank").action, "trainer_open")
        self.assertIsNone(self.runtime.step(111, view, "tank"))
        self.assertEqual(self.adapter.calls.count(("open_trainer", (44,))), 3)

    def test_craft_requires_materials_and_records_real_skill_up(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 2330: False})
        self.adapter.skills = (SkillLine(171, "Alchemy", value=1, max=75),)
        self.adapter.inventory = InventoryState(money=1000)
        self.assertIsNone(self.runtime.step(100, self.view, "tank"))
        self.adapter.inventory = InventoryState(
            money=1000,
            items=(Item(1, 2447), Item(2, 765), Item(3, 3372)))
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.skills[0].value, 2)
        self.assertEqual(self.adapter.empty_target_casts, 1)
        self.assertIsNotNone(self.store.get_meta("character:last_skill_up"))

    def test_gathering_uses_object_and_loots_with_skill_readback(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})
        self.adapter.skills = (SkillLine(182, "Herbalism", value=1, max=75),)
        self.adapter.objects = [{"guid": 55, "entry": 1617,
                                 "position": {"x": 1, "y": 0, "z": 0}}]
        self.adapter.lose_gather = True
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("use_game_object", (55,))), 1)
        self.assertEqual(self.adapter.calls.count(("loot", (55,))), 0)
        self.assertEqual(self.adapter.skills[0].value, 2)

    def test_gather_node_absence_does_not_confirm_across_epochs(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})
        self.adapter.skills = (SkillLine(182, "Herbalism", value=1, max=75),)
        self.adapter.objects = [{"guid": 55, "entry": 1617,
                                 "position": {"x": 1, "y": 0, "z": 0}}]

        def lose_without_gathering(guid):
            self.adapter.calls.append(("use_game_object", (guid,)))
            raise AdapterError("lost")

        self.adapter.use_game_object = lose_without_gathering
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.adapter.epoch = "2"
        self.adapter.objects = []
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "unknown")
        self.assertEqual(self.adapter.calls.count(("use_game_object", (55,))), 1)

    def test_distant_gathering_approaches_without_using(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})
        self.adapter.skills = (SkillLine(182, value=1, max=75),)
        self.adapter.objects = [{"guid": 55, "entry": 1617,
                                 "position": {"x": 10, "y": 0, "z": 0}}]
        action = self.runtime.step(100, self.view, "tank")
        self.assertEqual(action.action, "gather_approach")
        self.assertFalse([row for row in self.adapter.calls if row[0] == "use_game_object"])

    def test_gather_approach_transport_failure_cools_down_without_raising(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})
        self.adapter.skills = (SkillLine(182, value=1, max=75),)
        self.adapter.objects = [{"guid": 55, "entry": 1617,
                                 "position": {"x": 10, "y": 0, "z": 0}}]

        def refuse_move(x, y, z):
            self.adapter.calls.append(("move_to", (x, y, z)))
            raise AdapterError("lost")

        self.adapter.move_to = refuse_move
        self.assertIsNone(self.runtime.step(100, self.view, "tank"))
        self.assertIsNone(self.runtime.step(101, self.view, "tank"))
        self.assertEqual(len([row for row in self.adapter.calls if row[0] == "move_to"]), 1)

    def test_material_sale_does_not_confirm_an_unknown_craft(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 3275: False})
        self.adapter.skills = (SkillLine(129, value=1, max=75),)
        self.adapter.inventory = InventoryState(items=(Item(1, 2589, count=2),))

        def lose_cast(spell_id, target_guid, *, preserve_empty_target=False):
            self.adapter.calls.append(("cast", (spell_id, target_guid)))
            raise AdapterError("lost")

        self.adapter.cast = lose_cast
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.adapter.inventory = InventoryState()
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "unknown")
        self.assertEqual(self.adapter.calls.count(("cast", (3275, 0))), 1)

    def test_unknown_craft_expires_without_redispatch_and_cools_down(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 3275: False})
        self.adapter.skills = (SkillLine(129, value=1, max=75),)
        self.adapter.inventory = InventoryState(items=(Item(1, 2589),))

        def lose_cast(spell_id, target_guid, *, preserve_empty_target=False):
            self.adapter.calls.append(("cast", (spell_id, target_guid)))
            raise AdapterError("lost")

        self.adapter.cast = lose_cast
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.clock[0] += 47
        self.assertEqual(self.runtime.step(self.clock[0], self.view, "tank").detail, "abandoned")
        self.assertEqual(self.adapter.calls.count(("cast", (3275, 0))), 1)
        self.runtime.step(self.clock[0] + 1, self.view, "tank")
        self.assertEqual(self.adapter.calls.count(("cast", (3275, 0))), 1)

        restarted = CharacterGrowthRuntime(
            WARRIOR, self.content, self.adapter, self.store,
            clock=lambda: self.clock[0])
        self.assertIsNone(restarted.step(self.clock[0] + 2, self.view, "tank"))
        self.assertEqual(self.adapter.calls.count(("cast", (3275, 0))), 1)
    def test_craft_without_skill_up_confirms_exact_material_consumption(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False, 3275: False})
        self.adapter.skills = (SkillLine(129, value=1, max=75),)
        self.adapter.inventory = InventoryState(items=(Item(1, 2589, count=2),))

        def lose_after_crafting(spell_id, target_guid, *, preserve_empty_target=False):
            self.adapter.calls.append(("cast", (spell_id, target_guid)))
            self.adapter.inventory = InventoryState(items=(Item(1, 2589),))
            raise AdapterError("lost")

        self.adapter.cast = lose_after_crafting
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "confirmed")
        self.assertEqual(self.adapter.calls.count(("cast", (3275, 0))), 1)

    def test_unrelated_loot_does_not_confirm_gathering(self):
        self.adapter.spells.update({2366: False, 2259: False, 3273: False})
        self.adapter.skills = (SkillLine(182, value=1, max=75),)
        self.adapter.objects = [{"guid": 55, "entry": 1617,
                                 "position": {"x": 1, "y": 0, "z": 0}}]

        def lose_cast(spell_id, target_guid, *, target_is_game_object=False):
            self.adapter.calls.append(("cast", (spell_id, target_guid)))
            raise AdapterError("lost")

        self.adapter.cast = lose_cast
        self.assertEqual(self.runtime.step(100, self.view, "tank").detail, "unknown")
        self.adapter.inventory = InventoryState(items=(Item(1, 2589),))
        self.assertEqual(self.runtime.step(101, self.view, "tank").detail, "unknown")

    def test_open_trainer_refreshes_rows_after_they_become_available(self):
        self.adapter.state = CharacterState(
            trainer_guid=9, trainer_open=True,
            trainer_spells=(TrainerSpell(78, 1, 10, 0, 10, 0, 0),))
        self.adapter.spells = {6343: False, 284: False, 285: False,
                               2366: False, 2259: False, 3273: False,
                               2330: False, 3275: False}
        view = replace(self.view, nearby=({
            "guid": 9, "npc_flags": 0x30, "has_position": True,
            "position": {"x": 1, "y": 0, "z": 0},
        },))
        self.assertEqual(self.runtime.step(100, view, "tank").action, "trainer_refresh")
        self.adapter.state = replace(
            self.adapter.state,
            trainer_spells=(TrainerSpell(78, 0, 10, 0, 10, 0, 0),))
        self.assertEqual(self.runtime.step(116, view, "tank").detail, "confirmed")
        self.assertIn(("buy_spell", (9, 78)), self.adapter.calls)

    def test_missing_professions_do_not_exclude_class_trainer(self):
        self.adapter.spells = {}
        view = replace(self.view, nearby=({
            "guid": 9, "npc_flags": 0x30, "has_position": True,
            "position": {"x": 1, "y": 0, "z": 0},
        },))
        self.assertEqual(self.runtime.step(100, view, "tank").action, "trainer_open")
        self.assertIn(("open_trainer", (9,)), self.adapter.calls)

    def test_talent_prerequisite_defaults_to_first_rank(self):
        profile = replace(WARRIOR, talents={"tank": (
            TalentChoice(1001, 10, required_talent_id=99),)})
        runtime = CharacterGrowthRuntime(
            profile, self.content, self.adapter, self.store,
            clock=lambda: self.clock[0])
        self.adapter.state = CharacterState(has_talents=True, free_talent_points=1)
        runtime.step(100, self.view, "tank")
        self.assertFalse([row for row in self.adapter.calls if row[0] == "learn_talent"])
        self.adapter.state = replace(self.adapter.state, talents={99: 0})
        runtime.step(101, self.view, "tank")
        self.assertIn(("learn_talent", (1001, 0)), self.adapter.calls)


if __name__ == "__main__":
    unittest.main()
