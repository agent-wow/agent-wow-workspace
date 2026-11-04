from __future__ import annotations

import math
import unittest
from dataclasses import replace

from _support import ROOT  # noqa: F401  (sets sys.path)

from _growth_support import (
    PRIEST,
    ROGUE,
    ROUTE,
    WARRIOR,
    FakeAdapter,
    make_view,
    unit,
    with_combat,
)
from scripts.growth.adapter import AdapterError
from scripts.growth.machine import GrowthState, SoloGrowthMachine
from scripts.growth.metrics import MetricsWindow
from scripts.growth.profiles import load_profile, with_route
from scripts.growth.routes import TrainingRoute
from scripts.growth.social import RuntimeAction


class LowResourceTests(unittest.TestCase):
    """A low resource pool must suppress the skill, not spin on invalid casts."""

    def run_step(self, profile, powers):
        adapter = FakeAdapter(make_view(
            units=[unit(9)], powers=powers, in_combat=True))
        machine = SoloGrowthMachine(profile, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        return adapter, result

    def test_warrior_below_rage_cost_does_not_cast(self):
        adapter, result = self.run_step(WARRIOR, {"rage": 100})
        self.assertEqual(result.state, GrowthState.COMBAT)
        self.assertTrue(adapter.named("attack"))
        self.assertEqual(adapter.named("cast"), [])

    def test_warrior_with_rage_casts_the_priority_skill(self):
        adapter, _ = self.run_step(WARRIOR, {"rage": 200})
        self.assertEqual(adapter.named("cast"), [(78, 9)])

    def test_mana_healer_below_mana_cost_waits_without_melee_at_range(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0)], powers={"mana": 20}, in_combat=True))
        machine = SoloGrowthMachine(PRIEST, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.COMBAT)
        self.assertEqual(adapter.named("cast"), [])
        self.assertEqual(adapter.named("attack"), [])

    def test_combat_only_skill_waits_for_server_combat_state(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9)], powers={"rage": 200}, in_combat=False))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        machine.step(now=0)
        self.assertEqual(adapter.named("cast"), [])
        self.assertTrue(adapter.named("attack"))

    def test_self_heal_targets_self_not_the_enemy(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9)], powers={"mana": 100}, in_combat=True,
            health=40, max_health=100))
        machine = SoloGrowthMachine(PRIEST, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.COMBAT)
        self.assertEqual(adapter.named("cast"), [(2052, 1)])

    def test_mana_healer_with_mana_casts_damage_not_a_pointless_heal(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0)], powers={"mana": 100}, in_combat=True))
        SoloGrowthMachine(PRIEST, ROUTE, adapter=adapter).step(now=0)
        self.assertEqual(adapter.named("cast"), [(589, 9)])

    def test_rogue_below_energy_cost_does_not_cast(self):
        adapter, _ = self.run_step(ROGUE, {"energy": 20})
        self.assertEqual(adapter.named("cast"), [])

    def test_rogue_with_energy_casts_sinister_strike(self):
        adapter, _ = self.run_step(ROGUE, {"energy": 100})
        self.assertEqual(adapter.named("cast"), [(1752, 9)])
    def test_server_spellbook_filters_unknown_and_disabled_skills(self):
        for known_spells in ({6343: False}, {78: True}):
            with self.subTest(known_spells=known_spells):
                adapter = FakeAdapter(make_view(
                    units=[unit(9)], powers={"rage": 200}, in_combat=True,
                    combat_extra={"known_spells": known_spells}))
                SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter).step(now=0)
                self.assertEqual(adapter.named("cast"), [])

    def test_initialized_spellbook_allows_an_enabled_skill(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9)], powers={"rage": 200}, in_combat=True,
            combat_extra={"known_spells": {78: False}}))
        SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter).step(now=0)
        self.assertEqual(adapter.named("cast"), [(78, 9)])
    def test_sustained_auto_shot_is_not_toggled_off_on_the_next_tick(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0)], powers={"mana": 0}, in_combat=True,
            combat_extra={"known_spells": {75: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)
        machine.step(now=0)
        machine.step(now=3)
        self.assertEqual(adapter.named("cast"), [(75, 9)])

    def test_non_sustained_ranged_cast_stops_movement_before_casting(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=10.0)], powers={"mana": 100}, in_combat=True,
            combat_extra={"known_spells": {686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("stop_movement"), [()])
        self.assertEqual(adapter.named("cast"), [(686, 9)])
        self.assertLess(adapter.calls.index(("stop_movement", ())),
                        adapter.calls.index(("cast", (686, 9))))

    def test_dot_uses_filler_until_its_per_target_reapply_gate_expires(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=10.0)], powers={"mana": 200}, in_combat=True,
            combat_extra={"known_spells": {172: False, 686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)
        adapter.view = make_view(
            units=[unit(9, health=80, x=10.0)], powers={"mana": 200},
            in_combat=True,
            combat_extra={"known_spells": {172: False, 686: False}})
        machine.step(now=4)
        adapter.view = make_view(
            units=[unit(9, health=60, x=10.0)], powers={"mana": 200},
            in_combat=True,
            combat_extra={"known_spells": {172: False, 686: False}})
        machine.step(now=16)

        self.assertEqual(adapter.named("cast"), [(172, 9), (686, 9), (172, 9)])

    def test_ranged_caster_uses_melee_instead_of_casting_at_close_range(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=2.0)], powers={"mana": 100}, in_combat=True,
            combat_extra={"known_spells": {686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("attack"), [(9,)])
        self.assertEqual(adapter.named("cast"), [])

    def test_missing_target_is_blacklisted_after_consecutive_snapshot_losses(self):
        adapter = FakeAdapter(make_view(units=[unit(9)], in_combat=True))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        machine.step(now=0)
        adapter.view = make_view(units=[], in_combat=True)

        machine.step(now=1)
        self.assertEqual(machine.target_guid, 9)
        self.assertNotIn(9, machine._blacklist)
        machine.step(now=2)
        self.assertEqual(machine.target_guid, 9)
        self.assertNotIn(9, machine._blacklist)
        recovered = machine.step(now=3)

        self.assertEqual(recovered.state, GrowthState.STUCK)
        self.assertIsNone(machine.target_guid)
        self.assertIn(9, machine._blacklist)

    def test_line_of_sight_failure_repositions_before_the_next_cast(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=10.0)], powers={"mana": 100}, in_combat=True,
            combat_extra={"known_spells": {686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), ROUTE, adapter=adapter)
        machine.step(now=0)
        adapter.calls.clear()
        adapter.view = make_view(
            units=[unit(9, x=10.0)], powers={"mana": 100}, in_combat=True,
            cast_failures=1,
            combat_extra={"known_spells": {686: False},
                          "last_error": "cast failed: spell 686 result 47"})

        repositioned = machine.step(now=1)

        self.assertEqual(repositioned.action, "move_to")
        self.assertEqual(len(adapter.named("move_to")), 1)
        self.assertEqual(adapter.named("cast"), [])
        adapter.calls.clear()
        machine.step(now=2)
        self.assertEqual(adapter.named("move_to"), [],
                         "an already-observed failure must not move every tick")

        adapter.view = make_view(
            units=[unit(9, x=10.0)], powers={"mana": 100}, in_combat=True,
            cast_failures=2,
            combat_extra={"known_spells": {686: False},
                          "last_error": "cast failed: spell 686 result 47"})
        abandoned = machine.step(now=3)
        self.assertEqual(abandoned.state, GrowthState.ACQUIRE)
        self.assertIsNone(machine.target_guid)
        self.assertIn(9, machine._blacklist)

    def test_hunter_opens_with_auto_shot_at_ranged_distance(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0)], powers={"mana": 100},
            position={"x": 0.0, "y": 0.0, "z": 0.0, "orientation": math.pi},
            combat_extra={"known_spells": {75: False, 2973: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("cast"), [(75, 9)])
        self.assertEqual(adapter.named("set_ammo"), [(2512,)])
        self.assertEqual(adapter.named("move_to"), [])
        self.assertEqual(adapter.named("turn_to"), [(0.0,)])
        self.assertEqual(adapter.named("stop_movement"), [()])

    def test_hunter_does_not_cast_melee_skill_at_ranged_distance(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0)], powers={"mana": 100},
            combat_extra={"known_spells": {75: True, 2973: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("cast"), [])

    def test_hunter_does_not_cast_auto_shot_inside_its_minimum_range(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=6.0)], powers={"mana": 100},
            combat_extra={"known_spells": {75: False, 2973: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("cast"), [])
        self.assertEqual(adapter.named("attack"), [])

    def test_hunter_uses_melee_skill_after_the_target_closes(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9)], powers={"mana": 100},
            combat_extra={"known_spells": {75: True, 2973: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("cast"), [(2973, 9)])
        self.assertEqual(adapter.named("attack"), [(9,)])

    def test_hunter_small_height_difference_does_not_block_ranged_cast(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=15.0, z=0.0)], powers={"mana": 100},
            position={"x": 0.0, "y": 0.0, "z": -4.5, "orientation": 0.0},
            combat_extra={"known_spells": {75: False, 2973: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(adapter.named("cast"), [(75, 9)])
        self.assertEqual(adapter.named("move_to"), [])

    def test_refused_sustained_cast_is_retried_after_its_cooldown(self):
        view = make_view(
            units=[unit(9, x=15.0)], powers={"mana": 100}, cast_failures=0,
            combat_extra={"known_spells": {75: False}})
        adapter = FakeAdapter(view)
        machine = SoloGrowthMachine(
            with_route(load_profile("hunter"), ROUTE), ROUTE, adapter=adapter)
        machine.step(now=0)
        adapter.view = make_view(
            units=[unit(9, x=15.0)], powers={"mana": 100}, cast_failures=1,
            combat_extra={"known_spells": {75: False}})

        machine.step(now=1)
        self.assertEqual(adapter.named("cast"), [(75, 9)])
        machine.step(now=10)
        self.assertEqual(adapter.named("cast"), [(75, 9), (75, 9)])

    def test_blacklisted_quest_target_allows_only_a_local_grind_fallback(self):
        class QuestHints:
            def observe_level(self, level):
                pass

            def current(self):
                return None

            def target_entries(self):
                return frozenset({3098})

            def target_position(self, position):
                return None

        quest_mob = dict(unit(9, name="Wolf", x=2.0), entry=3098)
        grind_mob = dict(unit(10, name="Wolf", x=3.0), entry=3124)
        adapter = FakeAdapter(make_view(units=[quest_mob, grind_mob]))
        machine = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, quests=QuestHints())
        machine.step(now=0)
        self.assertEqual(machine.target_guid, 9)

        machine._blacklist[9] = 100.0
        machine._clear_target()
        machine.step(now=1)
        self.assertEqual(machine.target_guid, 10)

        adapter = FakeAdapter(make_view(units=[grind_mob]))
        traveling = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, quests=QuestHints())
        traveling.step(now=0)
        self.assertIsNone(
            traveling.target_guid,
            "an ordinary mob without a blocked quest target must not replace travel")

    def test_target_selection_skips_a_mob_fighting_another_player(self):
        contested = dict(unit(9), in_combat=True, target_guid=2727222)
        adapter = FakeAdapter(make_view(
            units=[contested, unit(10, x=10.0)]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        machine.step(now=0)
        self.assertEqual(machine.target_guid, 10)

    def test_attacker_beyond_engage_radius_precedes_idle_quest_targets(self):
        class QuestHints:
            def observe_level(self, level):
                pass

            def current(self):
                return None

            def target_entries(self):
                return frozenset({3098})

            def target_position(self, position):
                return None

        quest_mob = dict(unit(9, x=5.0), entry=3098)
        attacker = dict(
            unit(10, name="Unlisted Attacker", x=36.0, faction=123),
            entry=3124, in_combat=True, target_guid=1)
        adapter = FakeAdapter(make_view(
            units=[quest_mob, attacker], in_combat=True))
        machine = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, quests=QuestHints())

        machine.step(now=0)

        self.assertEqual(machine.target_guid, 10)
        self.assertTrue(adapter.named("move_to"))

    def test_far_reset_attacker_does_not_replace_a_local_grind_target(self):
        reset_attacker = dict(
            unit(9, x=120.0), in_combat=True, target_guid=1)
        adapter = FakeAdapter(make_view(
            units=[reset_attacker, unit(10, x=5.0)], in_combat=True))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        machine.step(now=0)

        self.assertEqual(machine.target_guid, 10)


class TerrainNavigationTests(unittest.TestCase):
    def test_direct_movement_retries_rejected_segments_at_shorter_distances(self):
        class CliffAdapter(FakeAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                if len(self.named("move_to")) < 3:
                    raise AdapterError(
                        "terrain height 63.27 differs from estimated 41.59 "
                        "by more than 8 yards")
                return {}

        view = make_view(position={"x": 0.0, "y": 0.0, "z": 41.59})
        adapter = CliffAdapter(view)
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        moved = machine._move_toward(
            view, {"x": 100.0, "y": 0.0, "z": 90.0}, 20.0)

        self.assertTrue(moved)
        self.assertEqual([math.hypot(call[0], call[1])
                          for call in adapter.named("move_to")],
                         [12.0, 8.0, 4.0])
        self.assertTrue(all(call[2] == 41.59
                            for call in adapter.named("move_to")))

    def test_non_terrain_movement_error_is_not_retried(self):
        class FailedAdapter(FakeAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                raise AdapterError("world rpc unavailable")

        view = make_view()
        adapter = FailedAdapter(view)
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        self.assertFalse(machine._move_toward(
            view, {"x": 100.0, "y": 0.0, "z": 0.0}, 20.0))
        self.assertEqual(len(adapter.named("move_to")), 1)


class StuckRecoveryTests(unittest.TestCase):
    def test_unreachable_target_is_blacklisted_with_a_side_step(self):
        profile = with_combat(WARRIOR, max_approach_seconds=3.0)
        adapter = FakeAdapter(make_view(units=[unit(9, x=20.0, y=0.0)]))
        machine = SoloGrowthMachine(profile, ROUTE, adapter=adapter)

        first = machine.step(now=0)
        self.assertEqual(first.state, GrowthState.COMBAT)
        self.assertTrue(adapter.named("move_to"), "an out-of-range target is approached")

        stuck = machine.step(now=4.0)
        self.assertEqual(stuck.state, GrowthState.STUCK)
        self.assertTrue(adapter.named("stop_attack"))
        self.assertTrue(adapter.named("move_to"), "stuck recovery side-steps")

        adapter.calls.clear()
        machine.step(now=4.5)
        self.assertNotIn(("attack", (9,)), adapter.calls,
                         "the blacklisted target must not be attacked again")

    def test_long_approach_does_not_stall_before_the_first_attack(self):
        profile = with_combat(WARRIOR, stall_seconds=5.0)
        adapter = FakeAdapter(make_view(units=[unit(9, x=20.0, y=0.0)]))
        machine = SoloGrowthMachine(profile, ROUTE, adapter=adapter)

        machine.step(now=0)
        self.assertEqual(adapter.named("attack"), [])
        adapter.view = make_view(units=[unit(9, x=2.0, y=0.0)])
        result = machine.step(now=6.0)

        self.assertEqual(result.state, GrowthState.COMBAT)
        self.assertEqual(adapter.named("attack"), [(9,)])
        self.assertIsNone(machine._blacklist.get(9))

    def test_blocked_grind_waypoint_enters_stuck_recovery(self):
        class BlockedAdapter(FakeAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                raise AdapterError(
                    "terrain height 63.70 differs from estimated 48.47 "
                    "by more than 8 yards")

            def navigate(self, x, y, z):
                super().navigate(x, y, z)
                raise AdapterError("no terrain-safe navigation segment")

        adapter = BlockedAdapter(make_view(units=[]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        before = machine.waypoint_index

        result = machine.step(now=0)

        self.assertEqual(result.state, GrowthState.STUCK)
        self.assertIn("waypoint movement blocked", result.detail)
        self.assertNotEqual(machine.waypoint_index, before)
        self.assertTrue(adapter.named("stop_attack"))

    def test_blocked_grind_waypoint_uses_terrain_navigation(self):
        class CliffAdapter(FakeAdapter):
            def move_to(self, x, y, z):
                super().move_to(x, y, z)
                raise AdapterError(
                    "terrain height 63.70 differs from estimated 48.47 "
                    "by more than 8 yards")

        adapter = CliffAdapter(make_view(units=[]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        before = machine.waypoint_index

        result = machine.step(now=0)

        self.assertEqual(result.state, GrowthState.TRAVEL)
        self.assertEqual(len(adapter.named("navigate")), 1)
        self.assertEqual(machine.waypoint_index, before)

    def test_no_damage_stall_blacklists_and_changes_the_route(self):
        profile = with_combat(WARRIOR, stall_seconds=5.0)
        adapter = FakeAdapter(make_view(units=[unit(9, x=2.0, y=0.0)]))
        machine = SoloGrowthMachine(profile, ROUTE, adapter=adapter)

        machine.step(now=0)
        before = machine.waypoint_index
        stuck = machine.step(now=6.0)
        self.assertEqual(stuck.state, GrowthState.STUCK)
        self.assertNotEqual(machine.waypoint_index, before,
                            "stuck recovery changes the route waypoint")
        self.assertTrue(adapter.named("stop_attack"))

        adapter.calls.clear()
        machine.step(now=6.5)
        self.assertNotIn(("attack", (9,)), adapter.calls)

    def test_low_health_flees_instead_of_attacking(self):
        adapter = FakeAdapter(make_view(
            units=[unit(9, x=2.0, y=0.0)], health=20, max_health=100))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.SAFE_RECOVERY)
        self.assertTrue(adapter.named("stop_attack"))
        self.assertEqual(adapter.named("attack"), [])
    def test_low_health_flees_an_attacker_missing_from_route_hints(self):
        attacker = dict(unit(
            9, name="Kul Tiras Sailor", faction=0, x=2.0),
            entry=3128, in_combat=True, target_guid=1)
        adapter = FakeAdapter(make_view(
            units=[attacker], health=20, max_health=100, in_combat=True))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        result = machine.step(now=0)

        self.assertEqual(result.state, GrowthState.SAFE_RECOVERY)
        self.assertTrue(adapter.named("stop_attack"))


class DeathRecoveryTests(unittest.TestCase):
    def test_dead_character_releases_the_spirit_once(self):
        adapter = FakeAdapter(make_view(dead=True, ghost=False))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        first = machine.step(now=0)
        self.assertEqual(first.state, GrowthState.DEAD)
        self.assertEqual(first.action, "release_spirit")
        self.assertEqual(adapter.named("release_spirit"), [()])

        second = machine.step(now=1)
        self.assertEqual(second.action, None,
                         "release must not be re-sent every tick")
        self.assertEqual(adapter.named("release_spirit"), [()])

    def test_dead_wait_has_a_bounded_recovery_outcome(self):
        adapter = FakeAdapter(make_view(dead=True, ghost=False))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        machine.step(now=0)
        result = machine.step(now=ROUTE.recovery.ghost_search_seconds + 1)
        self.assertEqual(result.state, GrowthState.SAFE_RECOVERY)
        self.assertEqual(adapter.named("release_spirit"), [()])

    def test_ghost_resurrects_at_a_nearby_spirit_healer(self):
        healer = unit(77, name="Spirit Healer", faction=0, x=2.0, y=0.0)
        adapter = FakeAdapter(make_view(dead=True, ghost=True, units=[healer]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.RESURRECT)
        self.assertEqual(result.action, "resurrect_at_spirit_healer")
        self.assertEqual(adapter.named("resurrect_at_spirit_healer"), [(77,)])

    def test_ghost_reclaims_a_reclaimable_corpse_before_seeking_a_healer(self):
        adapter = FakeAdapter(make_view(
            dead=True, ghost=True, corpse_reclaimable=True))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.CORPSE)
        self.assertEqual(result.action, "reclaim_corpse")
        self.assertEqual(adapter.named("reclaim_corpse"), [()])

    def test_ghost_without_a_healer_waits_instead_of_acting_blindly(self):
        adapter = FakeAdapter(make_view(dead=True, ghost=True))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.GHOST)
        self.assertIsNone(result.action)
        self.assertEqual(adapter.named("resurrect_at_spirit_healer"), [])


class MetricRestoreTests(unittest.TestCase):
    def test_restored_counters_do_not_create_negative_rates(self):
        metrics = MetricsWindow(window_seconds=3600, max_gap_seconds=120)
        metrics.observe(0, level=5, xp=0, next_level_xp=100,
                        kills=2, deaths=1, stuck_events=1)
        metrics.observe(60, level=5, xp=60, next_level_xp=100,
                        kills=2, deaths=1, stuck_events=1)
        restored = MetricsWindow.from_dict(metrics.to_dict())
        adapter = FakeAdapter(make_view(units=[]))
        machine = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, metrics=restored)

        machine.step(now=120)
        result = machine.step(now=180)
        self.assertEqual(result.metrics["death_rate"], 0.0)
        self.assertEqual(result.metrics["kills_per_hour"], 0.0)
        self.assertEqual(result.metrics["stuck_rate"], 0.0)


class ColdStartTests(unittest.TestCase):
    def test_ready_without_an_epoch_does_not_authorize_actions(self):
        adapter = FakeAdapter(make_view(ready=True, epoch=None, units=[unit(9)]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.RECONCILE)
        self.assertEqual(adapter.calls, [])

    def test_reconnect_returns_actionable_only_after_fresh_ready(self):
        adapter = FakeAdapter(make_view(ready=False, epoch="2"))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        waiting = machine.step(now=0)
        self.assertEqual(waiting.state, GrowthState.RECONCILE)
        self.assertIsNone(waiting.action)
        self.assertEqual(adapter.calls, [], "no action while the session is not ready")

        adapter.view = make_view(ready=True, epoch="2")
        actionable = machine.step(now=1)
        self.assertIn(actionable.state, {
            GrowthState.TRAVEL, GrowthState.ACQUIRE, GrowthState.COMBAT,
            GrowthState.REST,
        })
        self.assertTrue(adapter.calls, "moves only after a fresh ready snapshot")

    def test_map_switch_drops_the_old_target_and_never_attacks_the_stale_guid(self):
        adapter = FakeAdapter(make_view(map_id=0, units=[unit(50)]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)

        machine.step(now=0)
        self.assertEqual(machine.target_guid, 50)
        self.assertIn(("attack", (50,)), adapter.calls)

        adapter.calls.clear()
        adapter.view = make_view(
            map_id=1, epoch="1", units=[unit(50)],
            position={"x": 500.0, "y": 500.0, "z": 10.0, "orientation": 0.0})
        result = machine.step(now=1)

        self.assertIsNone(machine.target_guid)
        self.assertEqual(machine.position.get("x"), 500.0)
        self.assertNotIn(("attack", (50,)), adapter.calls)
        self.assertEqual(result.state, GrowthState.TOWN,
                         "no route content for the new map is a safe placeholder")

    def test_epoch_change_clears_the_old_target(self):
        adapter = FakeAdapter(make_view(epoch="1", units=[unit(50)]))
        machine = SoloGrowthMachine(WARRIOR, ROUTE, adapter=adapter)
        machine.step(now=0)
        self.assertEqual(machine.target_guid, 50)

        adapter.calls.clear()
        adapter.view = make_view(epoch="2", units=[])
        machine.step(now=1)
        self.assertIsNone(machine.target_guid)
        self.assertNotIn(("attack", (50,)), adapter.calls)


class CharacterTrainingAttachmentTests(unittest.TestCase):
    def test_missing_class_skill_uses_measured_vendor_detour_before_quest_step(self):
        class Quests:
            phase = "accept"

            def observe_level(self, level):
                pass

            def current(self):
                return object()

            def ready_to_turn_in(self, view):
                return False

            def step(self, now, view):
                raise AssertionError("quest route must wait for the training detour")

        training = TrainingRoute(
            position={"x": 100.0, "y": 0.0, "z": 0.0},
            waypoints=({"x": 60.0, "y": 0.0, "z": 0.0},),
            vendor_position={"x": 50.0, "y": 0.0, "z": 0.0},
            vendor_waypoints=({"x": 5.0, "y": 0.0, "z": 0.0},))
        route = replace(ROUTE, training={"warlock": training})
        adapter = FakeAdapter(make_view(
            combat_extra={"known_spells": {686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), route,
            adapter=adapter, quests=Quests())

        result = machine.step(now=0)

        self.assertEqual(result.state, GrowthState.TRAVEL)
        self.assertIn("sell before training", result.detail)
        self.assertEqual(adapter.named("move_to"), [(5.0, 0.0, 0.0)])

    def test_training_detour_leaves_vendor_for_class_trainer(self):
        training = TrainingRoute(
            position={"x": 100.0, "y": 0.0, "z": 0.0},
            waypoints=({"x": 60.0, "y": 0.0, "z": 0.0},),
            vendor_position={"x": 50.0, "y": 0.0, "z": 0.0})
        route = replace(ROUTE, training={"warlock": training})
        adapter = FakeAdapter(make_view(
            position={"x": 50.0, "y": 0.0, "z": 0.0, "orientation": 0.0},
            combat_extra={"known_spells": {686: False}}))
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), route, adapter=adapter)

        result = machine.step(now=0)

        self.assertEqual(result.state, GrowthState.TRAVEL)
        self.assertIn("missing class skill", result.detail)
        self.assertEqual(adapter.named("move_to"), [(60.0, 0.0, 0.0)])

    def test_unaffordable_training_returns_to_questing_after_selling(self):
        class Quests:
            phase = "accept"

            def __init__(self):
                self.calls = []

            def observe_level(self, level):
                pass

            def current(self):
                return object()

            def ready_to_turn_in(self, view):
                return False

            def target_entries(self):
                return frozenset()

            def target_position(self, position):
                return None

            def step(self, now, view):
                self.calls.append(now)
                return None

        training = TrainingRoute(
            position={"x": 100.0, "y": 0.0, "z": 0.0},
            waypoints=({"x": 60.0, "y": 0.0, "z": 0.0},),
            vendor_position={"x": 50.0, "y": 0.0, "z": 0.0},
            minimum_money=150)
        route = replace(ROUTE, training={"warlock": training})
        adapter = FakeAdapter(make_view(
            position={"x": 50.0, "y": 0.0, "z": 0.0, "orientation": 0.0},
            combat_extra={"known_spells": {686: False}}))
        quests = Quests()
        machine = SoloGrowthMachine(
            with_route(load_profile("warlock"), ROUTE), route,
            adapter=adapter, quests=quests)

        machine.step(now=0)
        machine.step(now=1)

        self.assertEqual(quests.calls, [0, 1])
        self.assertNotIn((60.0, 0.0, 0.0), adapter.named("move_to"))
        self.assertFalse(machine._training_vendor_done)

    def test_active_quest_uses_a_nearby_class_trainer_without_field_rpc(self):
        class Quests:
            phase = "accept"

            def observe_level(self, level):
                pass

            def current(self):
                return object()

            def ready_to_turn_in(self, view):
                return False

            def target_entries(self):
                return frozenset()

            def target_position(self, position):
                return None

            def step(self, now, view):
                return None

        class Growth:
            def __init__(self):
                self.calls = []

            def step(self, now, view, role):
                self.calls.append((now, role))
                return RuntimeAction("trainer_buy", "confirmed")

        growth = Growth()
        trainer = dict(unit(50, unit_type=7), npc_flags=0x30)
        adapter = FakeAdapter(make_view(units=[trainer]))
        machine = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, quests=Quests(),
            character_growth=growth)

        trained = machine.step(now=0)

        self.assertEqual(trained.state, GrowthState.TOWN)
        self.assertEqual(trained.action, "trainer_buy")
        self.assertEqual(growth.calls, [(0, "dps")])
        growth.calls.clear()
        adapter.view = make_view(units=[])
        machine.step(now=1)
        self.assertEqual(growth.calls, [])


class CooperationAttachmentTests(unittest.TestCase):
    def test_cooperation_action_uses_negotiate_state_without_attacking(self):
        class Cooperation:
            def step(self, now, view, class_mask, *, questing=False):
                return RuntimeAction("roll", "need: confirmed")

        adapter = FakeAdapter(make_view(units=[unit(50)]))
        machine = SoloGrowthMachine(
            WARRIOR, ROUTE, adapter=adapter, cooperation=Cooperation())
        result = machine.step(now=0)
        self.assertEqual(result.state, GrowthState.NEGOTIATE)
        self.assertEqual(result.action, "roll")
        self.assertFalse(adapter.named("attack"))


if __name__ == "__main__":
    unittest.main()
