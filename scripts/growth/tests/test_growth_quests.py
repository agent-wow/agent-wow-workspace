from __future__ import annotations

import math
import unittest
from dataclasses import replace
from unittest.mock import Mock

from _support import ROOT  # noqa: F401
from _growth_support import FakeAdapter, WARRIOR, make_view, unit

from scripts.growth.adapter import AdapterError
from scripts.growth.machine import SoloGrowthMachine
from scripts.growth.inventory_runtime import InventoryStep
from scripts.growth.profiles import PurchaseRule, load_profile, with_route
from scripts.growth.quest_runtime import (
    BatchQuestRuntime, QuestRuntime, load_quest_content)
from scripts.growth.routes import load_route


CONTENT = load_quest_content("durotar")
DUROTAR = load_route("durotar")
KALTUNK = CONTENT.quests[0].accept.position
GORNEK = CONTENT.quests[0].turn_in.position


class MemoryStore:
    def __init__(self, index=0):
        self.values = {"quest:durotar:index": str(index)}

    def get_meta(self, key, default=None):
        return self.values.get(key, default)

    def set_meta(self, key, value):
        self.values[key] = str(value)


class QuestAdapter(FakeAdapter):
    def quest_accept(self, guid, quest_id):
        self.calls.append(("quest_accept", (int(guid), int(quest_id))))
        return {}

    def quest_turn_in(self, guid, quest_id):
        self.calls.append(("quest_turn_in", (int(guid), int(quest_id))))
        return {}


def quest_state(active=(), *, completed=0, error="", last_quest=0, turning=0):
    return {
        "active": list(active), "completed": completed,
        "last_error": error, "last_quest_id": last_quest,
        "turning_in_quest_id": turning,
    }


def active_quest(quest_id, *, complete=False, objectives=()):
    return {"quest_id": quest_id, "complete": complete,
            "objectives": list(objectives)}


def at_npc(view, position=GORNEK, quest=None):
    return replace(view, map_id=1,
                   position={**position, "orientation": 0.0}, quest=quest)


class QuestRuntimeTests(unittest.TestCase):
    def runtime(self, adapter, index=0):
        return QuestRuntime(CONTENT, adapter, MemoryStore(index))

    def test_far_quest_giver_is_approached_before_accept(self):
        adapter = QuestAdapter(make_view())
        view = at_npc(adapter.view, quest=quest_state())
        view = replace(view, position={"x": 0, "y": 0, "z": 63})
        result = self.runtime(adapter).step(0, view)
        self.assertEqual(result.action, "quest_approach")
        self.assertEqual(adapter.named("quest_accept"), [])
        self.assertEqual(adapter.named("move_to")[0][2], 63.0,
                         "the next segment uses live rather than remote NPC height")

    def test_live_npc_respawn_guid_replaces_stale_content_guid(self):
        adapter = QuestAdapter(make_view())
        npc = CONTENT.quests[0].accept
        live_guid = npc.guid + 12001
        live = unit(live_guid, name=npc.name, unit_type=7,
                    x=npc.position["x"], y=npc.position["y"],
                    z=npc.position["z"])
        view = replace(at_npc(adapter.view, npc.position, quest_state()),
                       nearby=(live,))

        result = self.runtime(adapter).step(0, view)

        self.assertEqual(result.action, "quest_accept")
        self.assertEqual(adapter.named("quest_accept"),
                         [(live_guid, CONTENT.quests[0].quest_id)])

    def test_terrain_rejection_shortens_only_later_approach_steps(self):
        class CliffAdapter(QuestAdapter):
            def __init__(self, view):
                super().__init__(view)
                self.failures = 2

            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                if self.failures:
                    self.failures -= 1
                    raise AdapterError(
                        "terrain height 49.07 differs from estimated 58.43 "
                        "by more than 8 yards")
                return {}

        adapter = CliffAdapter(make_view())
        view = replace(at_npc(adapter.view, quest=quest_state()),
                       position={"x": 0, "y": 0, "z": 58.43})
        runtime = self.runtime(adapter)
        for now in range(3):
            self.assertEqual(runtime.step(now, view).action, "quest_approach")
        self.assertEqual([math.hypot(call[0], call[1])
                          for call in adapter.named("move_to")],
                         [20.0, 8.0, 4.0])

    def test_vertical_wall_uses_detour_only_after_short_steps_fail(self):
        class WallAdapter(QuestAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                if len(self.named("move_to")) <= 3:
                    raise AdapterError(
                        "terrain height 75.77 differs from estimated 43.75 "
                        "by more than 8 yards")
                return {}

        adapter = WallAdapter(make_view())
        view = replace(at_npc(adapter.view, quest=quest_state()),
                       position={"x": 0, "y": 0, "z": 43.75})
        npc = replace(CONTENT.quests[0].accept, waypoints=(
            {"x": 0.0, "y": -20.0, "z": 40.0},
            {"x": -20.0, "y": -20.0, "z": 40.0},
        ))
        runtime = self.runtime(adapter)
        for now in range(4):
            runtime._approach(npc, view)
        self.assertEqual(runtime.approach_waypoint, 0)
        self.assertEqual(adapter.named("move_to")[-1], (0.0, -4.0, 43.75),
                         "the first detour segment stays short instead of looping")

        at_waypoint = replace(view, position={"x": 0, "y": -20, "z": 40})
        self.assertEqual(runtime._approach(npc, at_waypoint).action,
                         "quest_waypoint")
        self.assertEqual(runtime.approach_waypoint, 1)

    def test_repeated_detour_failure_does_not_select_a_previous_waypoint(self):
        class WallAdapter(QuestAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                raise AdapterError(
                    "terrain height 75.77 differs from estimated 43.75 "
                    "by more than 8 yards")

        adapter = WallAdapter(make_view())
        view = replace(at_npc(adapter.view, quest=quest_state()),
                       position={"x": 0, "y": 0, "z": 43.75})
        npc = replace(CONTENT.quests[0].accept, waypoints=(
            {"x": 0.0, "y": -20.0, "z": 40.0},
            {"x": 0.0, "y": -40.0, "z": 40.0},
            {"x": 0.0, "y": -60.0, "z": 40.0},
        ))
        runtime = self.runtime(adapter)
        for _ in range(4):
            runtime._approach(npc, view)
        runtime._approach(npc, replace(
            view, position={"x": 0, "y": -20, "z": 40}))
        runtime._approach(npc, replace(
            view, position={"x": 0, "y": -40, "z": 40}))
        self.assertEqual(runtime.approach_waypoint, 2)

        runtime._approach(npc, replace(
            view, position={"x": 0, "y": -42, "z": 40}))

        self.assertEqual(runtime.approach_waypoint, 2,
                         "a failed short segment must not walk the route backward")
        self.assertTrue(adapter.named("navigate"),
                        "a blocked measured detour falls back to terrain A*")

    def test_objective_navigation_follows_the_npc_detour_in_reverse(self):
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=2)
        runtime.phase = "hunt"
        waypoints = CONTENT.quests[2].turn_in.waypoints

        at_npc = runtime.target_position(CONTENT.quests[2].turn_in.position)
        self.assertEqual(at_npc, waypoints[-1])
        next_waypoint = runtime.target_position(waypoints[-1])
        self.assertEqual(next_waypoint, waypoints[-2])
        at_objective = runtime.target_position(waypoints[0])
        self.assertEqual(at_objective, CONTENT.quests[2].objectives[0].position)
        self.assertEqual(runtime.target_position(waypoints[0]), at_objective,
                         "the direct final leg remains valid on later ticks")

    def test_nearby_objective_does_not_take_the_long_detour(self):
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=2)
        runtime.phase = "hunt"
        objective = CONTENT.quests[2].objectives[0].position

        self.assertEqual(runtime.target_position(objective), objective)

    def test_already_done_accept_response_skips_the_whole_quest(self):
        # The current module leaves an optimistic active row on invalid packets;
        # ALREADY_DONE must take precedence over that local row.
        state = quest_state([active_quest(4641)], error="quest invalid: reason 7",
                            last_quest=4641)
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter)
        result = runtime.step(0, at_npc(adapter.view, KALTUNK, state))
        self.assertEqual(result.action, "quest_already_complete")
        self.assertEqual(runtime.index, 1)

    def test_already_on_kill_quest_enters_hunt_and_filters_targets(self):
        state = quest_state([active_quest(788, objectives=[{
            "entry": 3098, "current": 0, "required": 8}])],
                            error="quest invalid: reason 13", last_quest=788)
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=1)
        result = runtime.step(0, at_npc(adapter.view, quest=state))
        self.assertIsNone(result)
        self.assertEqual(runtime.phase, "hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3098}))

    def test_objective_completion_moves_to_turn_in_then_advances(self):
        active = active_quest(788, objectives=[{
            "entry": 3098, "current": 8, "required": 8}])
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=1)
        ready = runtime.step(0, at_npc(
            adapter.view, quest=quest_state([active], last_quest=788)))
        self.assertEqual(ready.action, "quest_ready")
        sent = runtime.step(1, at_npc(
            adapter.view, quest=quest_state([active], last_quest=788)))
        self.assertEqual(sent.action, "quest_turn_in")
        self.assertEqual(len(adapter.named("quest_turn_in")), 1)
        done = runtime.step(2, at_npc(adapter.view, quest=quest_state(
            completed=1, last_quest=788)))
        self.assertEqual(done.action, "quest_complete")
        self.assertEqual(runtime.index, 2)

    def test_restart_reconciles_current_quest_at_acceptor(self):
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=1)
        result = runtime.step(0, at_npc(adapter.view, quest=quest_state()))
        self.assertEqual(result.action, "quest_accept")
        self.assertEqual(adapter.named("quest_accept"), [
            (CONTENT.quests[1].accept.guid, 788)])

    def test_restart_with_unknown_objectives_probes_turn_in_then_resumes_hunt(self):
        state = quest_state([active_quest(788)], error="quest invalid: reason 13",
                            last_quest=788)
        adapter = QuestAdapter(make_view())
        view = at_npc(adapter.view, quest=state)
        runtime = self.runtime(adapter, index=1)

        probe = runtime.step(0, view)
        sent = runtime.step(1, view)
        incomplete = runtime.step(2, at_npc(adapter.view, quest=quest_state(
            [active_quest(788)], error="quest failed: quest 788", last_quest=788)))

        self.assertEqual(probe.action, "quest_reconcile_turn_in")
        self.assertEqual(sent.action, "quest_turn_in")
        self.assertEqual(incomplete.action, "quest_incomplete")
        self.assertEqual(runtime.phase, "hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3098}))
        self.assertEqual(runtime.index, 1)

    def test_restart_after_inconclusive_timeout_reprobes_turn_in(self):
        state = quest_state(
            [active_quest(788)], error="quest 788 turn-in timed out",
            last_quest=788)
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=1)

        result = runtime.step(0, at_npc(adapter.view, quest=state))

        self.assertEqual(result.action, "quest_reconcile_turn_in")
        self.assertEqual(runtime.phase, "turn_in")
        self.assertEqual(runtime.index, 1)
        self.assertEqual(adapter.named("quest_turn_in"), [])

    def test_repeated_inconclusive_timeout_resumes_hunt(self):
        state = quest_state(
            [active_quest(788)], error="quest 788 turn-in timed out",
            last_quest=788)
        adapter = QuestAdapter(make_view())
        runtime = self.runtime(adapter, index=1)
        view = at_npc(adapter.view, quest=state)

        self.assertEqual(runtime.step(0, view).action, "quest_reconcile_turn_in")
        self.assertEqual(runtime.step(1, view).action, "quest_turn_in")
        self.assertEqual(runtime.step(4, view).action, "quest_turn_in")
        result = runtime.step(5, view)

        self.assertEqual(result.action, "quest_incomplete_timeout")
        self.assertEqual(runtime.phase, "hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3098}))
        self.assertEqual(runtime.index, 1)
        self.assertEqual(len(adapter.named("quest_turn_in")), 2)

    def test_level_gated_quest_grinds_instead_of_accepting_or_skipping(self):
        adapter = QuestAdapter(make_view(level=1))
        view = replace(make_view(level=1), map_id=1,
                       position=CONTENT.quests[2].accept.position,
                       quest=quest_state())
        runtime = self.runtime(adapter, index=2)

        self.assertIsNone(runtime.step(0, view))
        self.assertEqual(runtime.index, 2)
        self.assertEqual(adapter.named("quest_accept"), [])

        ready = runtime.step(1, replace(view, self_state={
            **view.self_state, "level": 2}))
        self.assertEqual(ready.action, "quest_accept")
        self.assertEqual(adapter.named("quest_accept"), [
            (CONTENT.quests[2].accept.guid, 792)])

    def test_unanswered_accept_retries_without_skipping_the_route(self):
        adapter = QuestAdapter(make_view())
        view = at_npc(adapter.view, KALTUNK, quest_state())
        runtime = self.runtime(adapter)

        for now in (0, 3, 6):
            self.assertEqual(runtime.step(now, view).action, "quest_accept")
        self.assertIsNone(runtime.step(7, view), "backoff permits grinding")
        self.assertEqual(runtime.index, 0)
        self.assertEqual(runtime.step(36, view).action, "quest_accept")
        self.assertEqual(len(adapter.named("quest_accept")), 4)

    def test_unanswered_turn_in_retries_without_claiming_completion(self):
        state = quest_state([active_quest(4641)], last_quest=4641)
        adapter = QuestAdapter(make_view())
        view = at_npc(adapter.view, GORNEK, state)
        runtime = self.runtime(adapter)

        self.assertEqual(runtime.step(0, view).action, "quest_ready")
        for now in (1, 4, 7):
            self.assertEqual(runtime.step(now, view).action, "quest_turn_in")
        self.assertIsNone(runtime.step(8, view), "backoff permits grinding")
        self.assertEqual(runtime.index, 0)
        self.assertEqual(runtime.step(37, view).action, "quest_turn_in")
        self.assertEqual(len(adapter.named("quest_turn_in")), 4)

    def test_hunt_travels_toward_a_visible_boar_beyond_engage_range(self):
        state = quest_state([active_quest(788)], last_quest=788)
        view = replace(make_view(units=[
            dict(unit(11, name="Mottled Boar", x=60), entry=3098),
        ]), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)

        result = machine.step(now=0)

        self.assertEqual(result.action, "move_to")
        self.assertIn("Mottled Boar", result.detail)
        self.assertIsNone(machine.target_guid)
        self.assertTrue(adapter.named("move_to"))

    def test_hunt_returns_to_its_objective_area_when_no_target_is_visible(self):
        state = quest_state([active_quest(788)], last_quest=788)
        view = replace(make_view(), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)

        result = machine.step(now=0)

        self.assertEqual(result.action, "move_to")
        self.assertIn("quest objective area", result.detail)
        self.assertTrue(adapter.named("move_to"))

    def test_active_quest_allows_nearby_vendor_supply_maintenance(self):
        state = quest_state([active_quest(792, objectives=[{
            "entry": 3101, "current": 0, "required": 8}])], last_quest=792)
        view = replace(make_view(level=2), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = BatchQuestRuntime(CONTENT, adapter, MemoryStore(2))
        quests.phase = "hunt"
        quests.accepted.add(792)
        supplied = replace(
            WARRIOR, inventory=replace(
                WARRIOR.inventory,
                supplies=(PurchaseRule(item_id=2512, target_count=600),)))
        machine = SoloGrowthMachine(
            with_route(supplied, DUROTAR), DUROTAR, adapter=adapter, quests=quests)

        class InventoryProbe:
            calls = 0

            def step(self, now, view, class_mask, **kwargs):
                self.calls += 1
                return InventoryStep("list_inventory", "restock arrows")

        probe = InventoryProbe()
        machine.inventory = probe
        result = machine.step(now=0)

        self.assertEqual(result.action, "list_inventory")
        self.assertEqual(result.detail, "restock arrows")
        self.assertEqual(probe.calls, 1)

    def test_depleted_supplies_preempt_the_next_quest_accept(self):
        view = replace(make_view(level=5), map_id=1, quest=quest_state())
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        supplied = replace(
            WARRIOR, inventory=replace(
                WARRIOR.inventory,
                supplies=(PurchaseRule(item_id=2512, target_count=600),)))
        machine = SoloGrowthMachine(
            with_route(supplied, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine.inventory.step = Mock(return_value=InventoryStep(
            "list_inventory", "restock arrows"))

        result = machine.step(now=0)

        self.assertEqual(result.action, "list_inventory")
        self.assertEqual(result.detail, "restock arrows")
        self.assertEqual(adapter.named("move_to"), [])
        self.assertEqual(adapter.named("quest_accept"), [])

    def test_quest_travel_without_kills_is_not_counted_as_physical_stuck(self):
        state = quest_state([active_quest(788, objectives=[{
            "entry": 3098, "current": 0, "required": 8}])], last_quest=788)
        view = replace(make_view(), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine._last_kill_at = 0

        result = machine.step(now=1000)

        self.assertEqual(result.action, "move_to")
        self.assertNotEqual(result.state.value, "stuck")
        self.assertIn("quest objective area", result.detail)

    def test_active_quest_does_not_wait_for_routine_town_maintenance(self):
        state = quest_state([active_quest(788)], last_quest=788)
        view = replace(make_view(), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine.inventory.step = Mock(return_value=InventoryStep(
            None, "waiting for vendor inventory"))

        result = machine.step(now=0)

        self.assertEqual(result.state.value, "travel")
        self.assertIn("quest objective area", result.detail)
        machine.inventory.step.assert_not_called()

    def test_incomplete_quest_travel_does_not_ignore_a_live_attacker(self):
        attacker = dict(unit(
            11, name="Kul Tiras Sailor", faction=0, x=15.0),
            entry=3128, in_combat=True, target_guid=1)
        state = quest_state([active_quest(784)], last_quest=784)
        view = replace(make_view(
            units=[attacker], in_combat=True,
            combat_extra={"known_spells": {75: False, 2973: False}},
        ), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = BatchQuestRuntime(CONTENT, adapter, MemoryStore(3))
        quests.phase = "hunt"
        quests.accepted.add(784)
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), DUROTAR), DUROTAR,
            adapter=adapter, quests=quests)

        result = machine.step(now=0)

        self.assertEqual(result.state.value, "combat")
        self.assertEqual(adapter.named("cast"), [(75, 11)])
        self.assertEqual(adapter.named("move_to"), [])

    def test_completed_objectives_preempt_low_health_combat_for_turn_in(self):
        active = active_quest(788, complete=True, objectives=[{
            "entry": 3098, "current": 8, "required": 8}])
        view = replace(make_view(
            health=10, max_health=100, in_combat=True,
            units=[dict(unit(11, name="Mottled Boar", x=2), entry=3098)],
        ), map_id=1, quest=quest_state([active], last_quest=788))
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine.target_guid = 11

        completed = machine.step(now=0)
        returned = machine.step(now=1)

        self.assertEqual(completed.action, "quest_ready")
        self.assertEqual(returned.action, "quest_approach")
        self.assertTrue(adapter.named("stop_attack"))
        self.assertIsNone(machine.target_guid)

    def test_full_bag_is_serviced_before_claiming_quest_reward(self):
        active = active_quest(788, complete=True, objectives=[{
            "entry": 3098, "current": 8, "required": 8}])
        view = at_npc(make_view(), quest=quest_state([active], last_quest=788))
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine.inventory.step = Mock(side_effect=[
            InventoryStep("list_inventory", "making room for quest reward"),
            None,
        ])

        ready = machine.step(now=0)
        maintenance = machine.step(now=1)
        self.assertEqual(adapter.named("quest_turn_in"), [])
        turn_in = machine.step(now=2)

        self.assertEqual(ready.action, "quest_ready")
        self.assertEqual(maintenance.state.value, "town")
        self.assertEqual(maintenance.action, "list_inventory")
        self.assertEqual(turn_in.action, "quest_turn_in")
        self.assertEqual(len(adapter.named("quest_turn_in")), 1)
        self.assertEqual(
            machine.inventory.step.call_args_list[0].kwargs["reserve_slots"], 1)

    def test_quest_targets_are_distributed_across_route_offsets(self):
        state = quest_state([active_quest(788)], last_quest=788)
        view = replace(make_view(units=[
            dict(unit(11, name="Mottled Boar", x=2), entry=3098),
            dict(unit(12, name="Mottled Boar", x=3), entry=3098),
        ]), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        first_quests = self.runtime(adapter, index=1)
        second_quests = self.runtime(adapter, index=1)
        first = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter,
            quests=first_quests, route_offset=1)
        second = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter,
            quests=second_quests, route_offset=2)

        first.step(now=0)
        second.step(now=0)

        self.assertIsNotNone(first.target_guid)
        self.assertIsNotNone(second.target_guid)
        self.assertNotEqual(first.target_guid, second.target_guid)

    def test_machine_hunts_boar_instead_of_scorpid_for_cutting_teeth(self):
        state = quest_state([active_quest(788)], last_quest=788)
        view = replace(make_view(units=[
            dict(unit(10, name="Scorpid Worker", x=2), entry=3124),
            dict(unit(11, name="Mottled Boar", x=12), entry=3098),
        ]), map_id=1, quest=state)
        adapter = QuestAdapter(view)
        quests = self.runtime(adapter, index=1)
        machine = SoloGrowthMachine(
            with_route(WARRIOR, DUROTAR), DUROTAR, adapter=adapter, quests=quests)
        machine.step(now=0)
        self.assertEqual(machine.target_guid, 11)


class BatchQuestRuntimeTests(unittest.TestCase):
    def test_batch_accept_merges_targets_and_turns_in_without_losing_progress(self):
        adapter = QuestAdapter(make_view())
        store = MemoryStore(1)
        runtime = BatchQuestRuntime(CONTENT, adapter, store)
        incomplete_boar = active_quest(788, objectives=[{
            "entry": 3098, "current": 0, "required": 8}])
        incomplete_familiar = active_quest(792, objectives=[{
            "entry": 3101, "current": 0, "required": 8}])

        accepted_next = runtime.step(0, at_npc(adapter.view, GORNEK, quest_state(
            [incomplete_boar], last_quest=788)))
        self.assertEqual(accepted_next.action, "quest_batch_accept_next")
        sent = runtime.step(1, at_npc(
            adapter.view, CONTENT.quests[2].accept.position,
            quest_state([incomplete_boar], last_quest=792)))
        self.assertEqual(sent.action, "quest_accept")
        self.assertEqual(adapter.named("quest_accept")[-1],
                         (CONTENT.quests[2].accept.guid, 792))

        hunting = runtime.step(2, at_npc(adapter.view, GORNEK, quest_state(
            [incomplete_boar, incomplete_familiar], last_quest=792)))
        self.assertEqual(hunting.action, "quest_batch_hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3098, 3101}))

        complete_boar = active_quest(788, complete=True, objectives=[{
            "entry": 3098, "current": 8, "required": 8}])
        ready = runtime.step(3, at_npc(adapter.view, GORNEK, quest_state(
            [complete_boar, incomplete_familiar], last_quest=788)))
        self.assertEqual(ready.action, "quest_objectives_complete")
        self.assertEqual(runtime.current().quest_id, 788)
        turned_in = runtime.step(4, at_npc(adapter.view, GORNEK, quest_state(
            [complete_boar, incomplete_familiar], last_quest=788)))
        self.assertEqual(turned_in.action, "quest_turn_in")
        done = runtime.step(5, at_npc(adapter.view, GORNEK, quest_state(
            [incomplete_familiar], completed=1, last_quest=788)))
        self.assertEqual(done.action, "quest_complete")
        self.assertEqual(runtime.index, 1, "the second batch member is still pending")

        hunting = runtime.step(6, at_npc(adapter.view, GORNEK, quest_state(
            [incomplete_familiar], completed=1, last_quest=792)))
        self.assertEqual(hunting.action, "quest_batch_hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3101}))

        complete_familiar = active_quest(792, complete=True, objectives=[{
            "entry": 3101, "current": 8, "required": 8}])
        runtime.step(7, at_npc(adapter.view, CONTENT.quests[2].turn_in.position,
                              quest_state([complete_familiar], completed=1,
                                          last_quest=792)))
        runtime.step(8, at_npc(adapter.view, CONTENT.quests[2].turn_in.position,
                              quest_state([complete_familiar], completed=1,
                                          last_quest=792)))
        finished = runtime.step(9, at_npc(
            adapter.view, CONTENT.quests[2].turn_in.position,
            quest_state(completed=2, last_quest=792)))
        self.assertEqual(finished.action, "quest_complete")
        self.assertEqual(runtime.index, 3)
        self.assertEqual(store.values["quest:durotar:index"], "3")
        self.assertEqual(runtime.current().quest_id, 784)
        runtime = BatchQuestRuntime(CONTENT, QuestAdapter(make_view()), MemoryStore(2))
        self.assertEqual([row.quest_id for row in runtime._batch()], [792])

    def test_multi_objective_anchor_skips_completed_entries_and_unlocks_batch(self):
        adapter = QuestAdapter(make_view(level=5))
        runtime = BatchQuestRuntime(CONTENT, adapter, MemoryStore(3))
        sailor_done = active_quest(784, objectives=[
            {"entry": 3128, "current": 10, "required": 10},
            {"entry": 3129, "current": 0, "required": 8},
            {"entry": 3192, "current": 0, "required": 1},
        ])
        view = replace(make_view(level=5), map_id=1,
                       position=CONTENT.quests[3].accept.position,
                       quest=quest_state([sailor_done], last_quest=784))

        self.assertEqual(runtime.step(0, view).action, "quest_batch_accept_next")
        gated = runtime.step(1, view)
        self.assertEqual(gated.action, "quest_batch_hunt")
        self.assertEqual(runtime.target_entries(), frozenset({3129, 3192}))
        self.assertEqual(runtime.target_position(),
                         CONTENT.quests[3].objectives[1].position)

        unlocked = runtime.step(2, replace(
            view, self_state={**view.self_state, "level": 6}))
        self.assertEqual(unlocked.action, "quest_batch_accept_next")
        self.assertEqual(runtime.current().quest_id, 837)

    def test_dangerous_active_quest_defers_hunt_but_not_completed_turn_in(self):
        adapter = QuestAdapter(make_view(level=4))
        runtime = BatchQuestRuntime(CONTENT, adapter, MemoryStore(3))
        runtime.phase = "hunt"
        runtime.accepted.add(784)
        runtime.observe_level(4)
        incomplete = active_quest(784, objectives=[
            {"entry": 3128, "current": 2, "required": 10}])
        view = replace(make_view(level=4), map_id=1,
                       quest=quest_state([incomplete], last_quest=784))

        self.assertIsNone(runtime.step(0, view))
        self.assertEqual(runtime.target_entries(), frozenset())
        self.assertIsNone(runtime.target_position())

        runtime.observe_level(5)
        self.assertEqual(runtime.target_entries(), frozenset())
        runtime.observe_level(6)
        self.assertIn(3128, runtime.target_entries())
        runtime.observe_level(4)
        complete = active_quest(784, complete=True, objectives=[
            {"entry": 3128, "current": 10, "required": 10}])
        ready = runtime.step(1, replace(
            view, quest=quest_state([complete], last_quest=784)))
        self.assertEqual(ready.action, "quest_objectives_complete")
        self.assertEqual(runtime.phase, "turn_in")

    def test_batch_repeated_timeout_resumes_merged_hunt(self):
        adapter = QuestAdapter(make_view())
        runtime = BatchQuestRuntime(CONTENT, adapter, MemoryStore(2))
        state = quest_state(
            [active_quest(792)], error="quest 792 turn-in timed out",
            last_quest=792)
        view = at_npc(adapter.view, CONTENT.quests[2].turn_in.position, state)

        self.assertEqual(runtime.step(0, view).action, "quest_reconcile_turn_in")
        self.assertEqual(runtime.step(1, view).action, "quest_turn_in")
        self.assertEqual(runtime.step(4, view).action, "quest_turn_in")
        result = runtime.step(5, view)

        self.assertEqual(result.action, "quest_incomplete_timeout")
        self.assertEqual(runtime.phase, "hunt")
        self.assertIsNone(runtime.turn_in_id)
        self.assertEqual(runtime.target_entries(), frozenset({3101}))
        self.assertEqual(runtime.index, 2)

    def test_mid_batch_checkpoint_does_not_reopen_completed_previous_member(self):
        runtime = BatchQuestRuntime(CONTENT, QuestAdapter(make_view()), MemoryStore(2))
        self.assertEqual([row.quest_id for row in runtime._batch()], [792])


if __name__ == "__main__":
    unittest.main()
