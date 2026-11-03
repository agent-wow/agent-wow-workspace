from __future__ import annotations

import unittest

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
from scripts.growth.machine import GrowthState, SoloGrowthMachine
from scripts.growth.metrics import MetricsWindow
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
        adapter, _ = self.run_step(PRIEST, {"mana": 20})
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
        adapter, _ = self.run_step(PRIEST, {"mana": 100})
        self.assertEqual(adapter.named("cast"), [(585, 9)])

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


class CooperationAttachmentTests(unittest.TestCase):
    def test_cooperation_action_uses_negotiate_state_without_attacking(self):
        class Cooperation:
            def step(self, now, view, class_mask):
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
