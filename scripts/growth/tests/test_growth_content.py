from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import ROOT  # noqa: F401  (sets sys.path)

from scripts.growth.profiles import ProfileError, load_profile
from scripts.growth.routes import RouteError, load_route


class ClassProfileContentTests(unittest.TestCase):
    def test_warrior_uses_rage_and_has_a_bounded_skill_priority(self):
        profile = load_profile("warrior")
        self.assertEqual(profile.resource, "rage")
        self.assertEqual(profile.resource_scale, 10)
        self.assertIn("tank", profile.roles)
        self.assertTrue(profile.source)
        self.assertTrue(profile.skills)
        costs = {skill.name: skill.cost for skill in profile.skills}
        self.assertEqual(costs["Heroic Strike"], 15)

    def test_mana_healer_and_rogue_energy_profiles_exist(self):
        priest = load_profile("priest")
        rogue = load_profile("rogue")
        self.assertEqual(priest.resource, "mana")
        self.assertEqual(rogue.resource, "energy")
        self.assertFalse(priest.combat.auto_attack)
        self.assertTrue(rogue.combat.auto_attack)
        self.assertEqual(
            [skill.name for skill in rogue.skills], ["Sinister Strike"],
            "combo finishers stay out of the initial loop until combo facts gate them")
        # Role/benefit-driven heal: only when the character is hurt.
        heal = next(s for s in priest.skills if s.name == "Lesser Heal")
        self.assertIsNotNone(heal.when_health_below_pct)
        self.assertGreater(heal.priority, 0)

    def test_unknown_class_profile_fails_closed(self):
        with self.assertRaises(ProfileError):
            load_profile("not-a-class")

    def test_profile_missing_required_facts_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            data = {"version": 1, "class": "warrior", "source": "test"}
            (directory / "warrior.json").write_text(
                json.dumps(data), encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_profile("warrior", directory=directory)


class RouteContentTests(unittest.TestCase):
    def test_route_is_versioned_and_keyed_by_map(self):
        route = load_route("elwynn-forest")
        self.assertEqual(route.map_id, 0)
        self.assertGreaterEqual(route.version, 1)
        self.assertTrue(route.source)

    def test_level_band_selects_measured_waypoints(self):
        route = load_route("elwynn-forest")
        band = route.band_for(5)
        self.assertIsNotNone(band)
        self.assertGreaterEqual(band.min_level, 5)
        self.assertTrue(band.waypoints)
        self.assertTrue(band.source)

    def test_target_hints_include_hostile_faction_and_critter_guard(self):
        route = load_route("elwynn-forest")
        self.assertIn(32, route.targets.hostile_factions)
        self.assertIn("turkey", route.targets.critter_names)
        self.assertIn(3, route.targets.unit_types)

    def test_unknown_route_fails_closed(self):
        with self.assertRaises(RouteError):
            load_route("not-a-zone")


if __name__ == "__main__":
    unittest.main()
