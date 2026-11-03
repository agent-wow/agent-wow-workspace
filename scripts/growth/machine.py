"""Deterministic solo growth state machine.

The loop is intentionally small and rule-driven (KTD1). It reads live facts
through :class:`scripts.growth.adapter.WorldAdapter`, walks measured route
waypoints, fights with the class profile's initial skill priorities, recovers
from death/ghost/corpse states, blacklists targets that are unreachable or make
no progress, and records sampled metrics.

Explicit states exist for the whole MVP so later units attach to named
boundaries instead of rewriting the loop:

- ``TOWN`` and ``TASK`` are safe placeholders. ``TOWN`` is used when the
  current map has no route content; ``TASK`` when the level is outside every
  route band. Neither claims to implement U6 quest behavior.
- ``NEGOTIATE`` is reserved for U4 cooperation and is never entered by the
  solo loop.
- ``LOOT`` opens a corpse. ``TOWN`` also covers out-of-combat equipment and
  nearby-vendor actions through the durable inventory runtime.

Boundedness: every target has an approach deadline and a no-damage stall
deadline, every ghost search has a deadline, and every stuck recovery advances
the route and blacklists the target for a bounded interval.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .adapter import AdapterError, WorldAdapter, WorldView
from .inventory_runtime import InventoryRuntime
from .items import InventoryPolicy
from .metrics import MetricsWindow
from .profiles import ClassProfile, Skill
from .routes import Route

__all__ = ["GrowthState", "SoloGrowthMachine", "StepResult"]


class GrowthState(str, Enum):
    OFFLINE = "offline"
    RECONCILE = "reconcile"
    SAFE_RECOVERY = "safe_recovery"
    REST = "rest"
    TOWN = "town"
    TASK = "task"
    TRAVEL = "travel"
    ACQUIRE = "acquire"
    COMBAT = "combat"
    LOOT = "loot"
    STUCK = "stuck"
    DEAD = "dead"
    GHOST = "ghost"
    RESURRECT = "resurrect"
    CORPSE = "corpse"
    NEGOTIATE = "negotiate"


@dataclass(frozen=True)
class StepResult:
    state: GrowthState
    action: str | None = None
    detail: str | None = None
    metrics: dict | None = None


def distance2d(a: dict, b: dict) -> float:
    return math.hypot(
        float(a.get("x", 0.0)) - float(b.get("x", 0.0)),
        float(a.get("y", 0.0)) - float(b.get("y", 0.0)))


def bearing(origin: dict, destination: dict) -> float:
    """Server-facing angle: AzerothCore uses ``atan2(dy, dx)`` (0 = +X)."""
    return math.atan2(
        float(destination.get("y", 0.0)) - float(origin.get("y", 0.0)),
        float(destination.get("x", 0.0)) - float(origin.get("x", 0.0)))


def angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


class SoloGrowthMachine:
    def __init__(
        self,
        profile: ClassProfile,
        route: Route,
        *,
        adapter: WorldAdapter,
        store=None,
        clock: Callable[[], float] = time.time,
        logger: Callable[[str], None] | None = None,
        epoch_hint: int | str = 0,
        metrics: MetricsWindow | None = None,
        cooperation=None,
    ):
        if int(profile.route.map_id) != int(route.map_id):
            raise ValueError(
                f"profile route map {profile.route.map_id} does not match "
                f"route {route.zone} map {route.map_id}")
        self.profile = profile
        self.route = route
        self.adapter = adapter
        self.store = store
        self.clock = clock
        self.log = logger or (lambda message: None)
        self.epoch_hint = epoch_hint

        stored = store.load_metrics() if store is not None else None
        self.metrics = metrics or MetricsWindow.from_dict(stored)
        self.cooperation = cooperation
        self.inventory = InventoryRuntime(
            InventoryPolicy(profile.inventory), adapter, store,
            clock=clock, logger=self.log)

        self.epoch: str | None = None
        self.map_id: int | None = None
        self.position: dict = {}
        self.target_guid: int | None = None
        self.waypoint_index = 0

        self._now = 0.0
        self._last_metrics: dict | None = None
        self._blacklist: dict[int, float] = {}
        self._looted: dict[int, float] = {}
        self._disabled_spells: dict[int, float] = {}
        self._target_since: float | None = None
        self._target_health: int | None = None
        self._last_damage_at: float | None = None
        self._last_attack_at: float | None = None
        self._last_cast_at: float | None = None
        self._lost_targets = 0
        self._last_kill_at: float | None = None
        self._last_stuck_at: float | None = None
        self._no_target_since: float | None = None
        self._rest_since: float | None = None
        self._rest_health: int | None = None

        self._ever_ready = False
        self._dead_counted = False
        self._released_this_death = False
        self._reclaim_tried = False
        self._resurrect_attempts = 0
        self._ghost_since: float | None = None
        self._dead_since: float | None = None

        self._kills = 0
        self._deaths = 0
        self._stuck_events = 0
        if self.metrics.observations:
            latest = self.metrics.observations[-1]
            self._kills = int(latest.get("kills") or 0)
            self._deaths = int(latest.get("deaths") or 0)
            self._stuck_events = int(latest.get("stuck") or 0)
        self._combat_kills = 0
        self._combat_kills_at_engage = 0
        self._state = GrowthState.RECONCILE

    # ------------------------------------------------------------- entry

    def step(
        self,
        *,
        now: float | None = None,
        view: WorldView | None = None,
        epoch_hint: int | str | None = None,
        before_action: Callable[[WorldView], None] | None = None,
    ) -> StepResult:
        now = float(self.clock() if now is None else now)
        self._now = now
        if view is None:
            try:
                view = self.adapter.read_view()
            except AdapterError as exc:
                return self._result(
                    GrowthState.OFFLINE, detail=f"world read failed: {exc}")

        hint = self.epoch_hint if epoch_hint is None else epoch_hint
        epoch = view.epoch if view.epoch is not None else hint
        epoch_key = str(epoch) if epoch is not None else ""
        if not epoch_key.strip() or epoch_key == "0":
            self._clear_target()
            self.position = {}
            self.epoch = None
            self._ever_ready = False
            return self._result(
                GrowthState.RECONCILE, detail="server did not report an epoch")
        if self.epoch is None or epoch_key != self.epoch:
            self._cold_start(epoch_key)
        self.epoch = epoch_key

        if not view.ready:
            # Reconnect: drop the old target and do not act until a fresh ready
            # snapshot arrives. No nearby/position facts are trusted here.
            self._clear_target()
            self._ever_ready = False
            return self._result(
                GrowthState.RECONCILE, detail="session is not ready")

        self._ever_ready = True
        if before_action is not None:
            # The worker verifies roster identity for this live epoch before
            # any movement, combat, loot, or recovery RPC can be dispatched.
            before_action(view)
        if view.map_id is None:
            # Do not keep using a previously observed map after the server
            # stops reporting one.
            self.map_id = None
        else:
            if self.map_id is not None and int(view.map_id) != int(self.map_id):
                self._on_map_change()
            self.map_id = int(view.map_id)
        if view.position:
            self.position = dict(view.position)

        self._combat_kills = int(view.combat.get("kills") or 0)
        if view.dead and not self._dead_counted:
            self._deaths += 1
            self._dead_counted = True
        self.metrics.observe(
            now, level=view.level, xp=view.xp,
            next_level_xp=view.next_level_xp, kills=self._kills,
            deaths=self._deaths, stuck_events=self._stuck_events)
        if self._last_kill_at is None:
            self._last_kill_at = now

        if view.dead:
            return self._handle_death(now, view)
        self._reset_death_episode()

        if self.map_id is None:
            self._clear_target()
            return self._result(
                GrowthState.TOWN, detail="server did not report a map id")
        if int(self.map_id) != int(self.route.map_id):
            self._clear_target()
            return self._result(
                GrowthState.TOWN,
                detail=f"no route content for map {self.map_id}")
        if self.route.band_for(view.level) is None:
            self._clear_target()
            return self._result(
                GrowthState.TASK,
                detail=f"no route band covers level {view.level}")

        recovery = self._handle_recovery(now, view)
        if recovery is not None:
            return recovery

        if self.cooperation is not None:
            cooperation = self.cooperation.step(now, view, self.profile.class_mask)
            if cooperation is not None:
                return self._result(
                    GrowthState.NEGOTIATE, cooperation.action, cooperation.detail)

        if not self.position:
            # Never attack from an assumed origin.
            return self._result(
                GrowthState.ACQUIRE, detail="waiting for a live position")

        if not view.in_combat and self.target_guid is None:
            inventory = self.inventory.step(now, view, self.profile.class_mask)
            if inventory is not None:
                return self._result(
                    GrowthState.TOWN, inventory.action, inventory.detail)

        if self.target_guid is not None:
            return self._continue_combat(now, view)

        candidate = self._choose_target(view, now)
        if candidate is None:
            return self._no_target(now, view)

        self._engage(candidate, now)
        return self._continue_combat(now, view)

    # ----------------------------------------------------------- lifecycle

    def _cold_start(self, epoch_key: str) -> None:
        self._clear_target()
        self.position = {}
        self._blacklist.clear()
        self.waypoint_index = 0
        self._lost_targets = 0
        self._no_target_since = None
        self._last_kill_at = None
        self._ever_ready = False
        self._released_this_death = False
        self._reclaim_tried = False
        self._resurrect_attempts = 0
        self._ghost_since = None
        self._dead_since = None
        self.log(f"new session epoch {epoch_key}: cold start")

    def _on_map_change(self) -> None:
        self._clear_target()
        self.position = {}
        self._blacklist.clear()
        self.waypoint_index = 0
        self._lost_targets = 0
        self._no_target_since = None
        self.log("map change: dropped stale position and target")

    def _reset_death_episode(self) -> None:
        self._dead_counted = False
        self._released_this_death = False
        self._reclaim_tried = False
        self._resurrect_attempts = 0
        self._ghost_since = None
        self._dead_since = None

    # ------------------------------------------------------------ recovery

    def _handle_death(self, now: float, view: WorldView) -> StepResult:
        self._clear_target()
        if self._dead_since is None:
            self._dead_since = now
        if not view.ghost:
            if now - self._dead_since > self.route.recovery.ghost_search_seconds:
                return self._result(
                    GrowthState.SAFE_RECOVERY,
                    detail="ghost did not appear within the recovery window")
            if not self._released_this_death:
                released = self._safe_action(
                    "release_spirit", self.adapter.release_spirit)
                self._released_this_death = released
                return self._result(
                    GrowthState.DEAD, "release_spirit",
                    "releasing spirit" if released else "release failed; retrying")
            return self._result(
                GrowthState.DEAD, detail="waiting for the ghost to appear")

        if view.self_state.get("corpse_reclaimable") and not self._reclaim_tried:
            self._reclaim_tried = True
            self._safe_action("reclaim_corpse", self.adapter.reclaim_corpse)
            return self._result(
                GrowthState.CORPSE, "reclaim_corpse", "reclaiming corpse")

        healer = self._find_spirit_healer(view)
        if healer is not None:
            healer_position = healer.get("position") or {}
            if (view.position
                    and distance2d(view.position, healer_position)
                    > self.route.recovery.spirit_healer_interact_range):
                self._move_toward(view, healer_position,
                                  self.profile.combat.approach_step)
                return self._result(
                    GrowthState.GHOST, "move_to", "walking to spirit healer")
            if self._resurrect_attempts < 3:
                self._resurrect_attempts += 1
                guid = healer.get("guid")
                if guid is not None:
                    self._safe_action(
                        "resurrect_at_spirit_healer",
                        lambda: self.adapter.resurrect_at_spirit_healer(guid))
                    return self._result(
                        GrowthState.RESURRECT, "resurrect_at_spirit_healer",
                        "resurrecting at spirit healer")
            return self._result(
                GrowthState.SAFE_RECOVERY, detail="spirit healer is unavailable")

        if self._ghost_since is None:
            self._ghost_since = now
        if now - self._ghost_since <= self.route.recovery.ghost_search_seconds:
            return self._result(
                GrowthState.GHOST, detail="waiting for a spirit healer")
        return self._result(
            GrowthState.SAFE_RECOVERY,
            detail="no spirit healer within the search window")

    def _handle_recovery(self, now: float, view: WorldView) -> StepResult | None:
        maximum = view.max_health
        if maximum <= 0:
            return None
        fraction = view.health / maximum
        recovery = self.profile.recovery

        if fraction >= recovery.rest_until_pct:
            self._rest_since = None
            self._rest_health = None
            return None
        # Start resting below rest_below_pct, but keep resting until
        # rest_until_pct so the character does not re-enter combat at 66%.
        if (self._rest_since is None
                and fraction >= recovery.rest_below_pct):
            return None

        threat = self._current_target_unit(view) or self._nearest_hostile(view)
        must_retreat = (
            view.in_combat or threat is not None
            or fraction < recovery.flee_below_pct)
        if must_retreat and (threat is not None or self.target_guid is not None):
            skill = self._choose_skill(view, now)
            can_heal = (
                fraction > recovery.flee_below_pct
                and skill is not None
                and not skill.requires_target
                and skill.when_health_below_pct is not None)
            if can_heal:
                # Let the combat loop cast its profiled self-heal instead of
                # fleeing at the same health threshold every time.
                return None
            self._safe_action("stop_attack", self.adapter.stop_attack)
            away_from = (threat or {}).get("position") or {}
            if not away_from and self.target_guid is not None:
                away_from = self._current_target_unit(view) or {}
                away_from = away_from.get("position") or {}
            self._move_away(view, away_from)
            self._clear_target()
            return self._result(
                GrowthState.SAFE_RECOVERY,
                detail=f"low health {view.health}/{maximum}")

        if fraction < recovery.rest_until_pct:
            if self._rest_since is None:
                self._rest_since = now
                self._rest_health = view.health
            elif now - self._rest_since > recovery.max_rest_seconds:
                if view.health <= (self._rest_health or 0):
                    return self._stuck_recover(
                        now, view, "rest made no health progress")
                self._rest_since = now
                self._rest_health = view.health
            return self._result(
                GrowthState.REST,
                detail=f"resting {view.health}/{maximum}")
        return None

    # -------------------------------------------------------------- combat

    def _continue_combat(self, now: float, view: WorldView) -> StepResult:
        target = self._current_target_unit(view)
        if target is None:
            return self._target_missing(now, view)
        if target.get("dead") or int(target.get("health") or 0) <= 0:
            return self._loot(now, view, self.target_guid)

        health = int(target.get("health") or 0)
        if self._target_health is not None and health < self._target_health:
            self._last_damage_at = now
        self._target_health = health

        target_position = target.get("position") or {}
        distance = distance2d(self.position, target_position)
        dz = abs(float(self.position.get("z", 0.0))
                 - float(target_position.get("z", 0.0)))

        if distance > self.profile.combat.melee_range or \
                dz > self.profile.combat.dz_tolerance:
            if (self._target_since is not None
                    and now - self._target_since
                    > self.profile.combat.max_approach_seconds):
                return self._stuck_recover(
                    now, view, "target unreachable", target)
            self._move_toward(view, target_position,
                              self.profile.combat.approach_step)
            return self._result(
                GrowthState.COMBAT, "move_to",
                f"approaching target at {distance:.1f} yd")

        if (self._last_damage_at is not None
                and now - self._last_damage_at
                > self.profile.combat.stall_seconds):
            return self._stuck_recover(
                now, view, "combat stall: target took no damage", target)

        if self.profile.combat.auto_attack and (
                self._last_attack_at is None
                or now - self._last_attack_at
                >= self.profile.combat.attack_reissue_seconds):
            desired = bearing(self.position, target_position)
            if abs(angle_diff(desired, view.orientation)) > 0.05:
                self._safe_action(
                    "turn_to", lambda: self.adapter.turn_to(desired))
            self._safe_action(
                "attack", lambda: self.adapter.attack(self.target_guid))
            self._last_attack_at = now

        skill = self._choose_skill(view, now)
        if skill is not None and (
                self._last_cast_at is None
                or now - self._last_cast_at
                >= self.profile.combat.cast_interval_seconds):
            cast_target = self.target_guid if skill.requires_target else view.guid
            if cast_target is not None:
                ok = self._safe_action(
                    "cast",
                    lambda: self.adapter.cast(skill.spell_id, cast_target))
                if ok:
                    self._last_cast_at = now
                else:
                    self._disabled_spells[skill.spell_id] = (
                        now + self.profile.combat.spell_disable_seconds)
        detail = f"fight {target.get('name') or self.target_guid}"
        if skill is not None:
            detail += f" skill={skill.name}"
        return self._result(GrowthState.COMBAT, "attack", detail)

    def _choose_skill(self, view: WorldView, now: float) -> Skill | None:
        fraction = (view.health / view.max_health
                    if view.max_health > 0 else 1.0)
        available = view.power(self.profile.resource)
        for skill in self.profile.skill_order(view.level):
            if skill.requires_target and self.target_guid is None:
                continue
            if not skill.requires_target and view.guid is None:
                continue
            if skill.in_combat_only and not view.in_combat:
                continue
            if (skill.when_health_below_pct is not None
                    and fraction >= skill.when_health_below_pct):
                continue
            if (skill.when_health_above_pct is not None
                    and fraction <= skill.when_health_above_pct):
                continue
            disabled_until = self._disabled_spells.get(skill.spell_id)
            if disabled_until is not None and disabled_until > now:
                continue
            required = int(skill.cost) * int(self.profile.resource_scale)
            if required > 0 and (available is None or available < required):
                continue
            return skill
        return None

    def _target_missing(self, now: float, view: WorldView) -> StepResult:
        guid = self.target_guid
        combat = view.combat or {}
        victim = combat.get("last_victim")
        if combat.get("target_dead") or (
                victim is not None and int(victim) == int(guid or -1)
                and self._combat_kills > self._combat_kills_at_engage):
            return self._loot(now, view, guid)
        self._lost_targets += 1
        self._clear_target()
        if self._lost_targets >= self.profile.combat.lost_target_limit:
            if guid is not None:
                self._blacklist[int(guid)] = (
                    now + self.profile.combat.blacklist_seconds)
            return self._stuck_recover(
                now, view, "target repeatedly lost")
        return self._result(
            GrowthState.ACQUIRE, detail=f"target {guid} left the nearby snapshot")

    def _loot(self, now: float, view: WorldView, guid: int | None) -> StepResult:
        self._kills += 1
        if self._target_since is not None:
            self.metrics.record_kill(now, now - self._target_since)
        self._last_kill_at = now
        if guid is not None:
            looted_at = self._looted.get(int(guid))
            if looted_at is None or now - looted_at > 60.0:
                self._safe_action(
                    "loot", lambda: self.adapter.loot(int(guid)))
                self._looted[int(guid)] = now
        self._clear_target()
        self._no_target_since = None
        self._prune_memories(now)
        return self._result(
            GrowthState.LOOT, "loot", f"looted target {guid}")

    # ----------------------------------------------------------- selection

    def _choose_target(self, view: WorldView, now: float) -> dict | None:
        self._prune_memories(now)
        hints = self.route.targets
        best = None
        best_score = None
        for candidate in view.nearby:
            guid = candidate.get("guid")
            if guid is None or int(guid) in self._blacklist:
                continue
            if view.guid is not None and int(guid) == int(view.guid):
                continue
            if not self._is_hostile(candidate, view.level):
                continue
            if not candidate.get("has_position"):
                continue
            position = candidate.get("position") or {}
            distance = distance2d(self.position, position)
            if distance > self.profile.combat.engage_radius:
                continue
            crowd = 0
            for other in view.nearby:
                if other is candidate or other.get("guid") == guid:
                    continue
                if not self._is_hostile(other, view.level):
                    continue
                if distance2d(position, other.get("position") or {}) < \
                        self.profile.combat.crowd_radius:
                    crowd += 1
            level_delta = int(candidate.get("level") or 0) - view.level
            level_score = -level_delta * 6 if level_delta else 0
            score = (distance
                     + min(crowd, self.profile.combat.max_crowd) * 14
                     + level_score)
            if best_score is None or score < best_score:
                best, best_score = candidate, score
        return best

    def _is_hostile(self, candidate: dict, level: int) -> bool:
        hints = self.route.targets
        if candidate.get("dead") or int(candidate.get("health") or 0) <= 0:
            return False
        if not candidate.get("has_position"):
            return False
        if hints.unit_types and int(candidate.get("type") or 0) not in \
                hints.unit_types:
            return False
        name = str(candidate.get("name") or "").lower()
        if any(critter in name for critter in hints.critter_names):
            return False
        target_level = int(candidate.get("level") or 0)
        if target_level > level + hints.level_ceiling_delta:
            return False
        if target_level < max(1, level - hints.level_floor_delta):
            return False
        faction = int(candidate.get("faction") or 0)
        if faction in hints.hostile_factions:
            return True
        return any(hint in name for hint in hints.hostile_names)

    def _nearest_hostile(self, view: WorldView) -> dict | None:
        best = None
        best_distance = self.profile.combat.engage_radius
        for candidate in view.nearby:
            if not self._is_hostile(candidate, view.level):
                continue
            distance = distance2d(self.position, candidate.get("position") or {})
            if distance < best_distance:
                best, best_distance = candidate, distance
        return best

    def _engage(self, candidate: dict, now: float) -> None:
        self.target_guid = int(candidate["guid"])
        self._target_since = now
        self._target_health = int(candidate.get("health") or 0)
        self._last_damage_at = now
        self._last_attack_at = None
        self._last_cast_at = None
        self._lost_targets = 0
        self._no_target_since = None
        self._combat_kills_at_engage = self._combat_kills
        self.log(f"engage {candidate.get('name')} guid={self.target_guid}")

    def _current_target_unit(self, view: WorldView) -> dict | None:
        if self.target_guid is None:
            return None
        for candidate in view.nearby:
            if candidate.get("guid") is not None and \
                    int(candidate["guid"]) == int(self.target_guid):
                return candidate
        if view.target and view.target.get("guid") is not None and \
                int(view.target["guid"]) == int(self.target_guid):
            return view.target
        return None

    # --------------------------------------------------------- navigation

    def _no_target(self, now: float, view: WorldView) -> StepResult:
        if self._no_target_since is None:
            self._no_target_since = now
        if (self._last_kill_at is not None
                and now - self._last_kill_at
                >= self.profile.combat.no_kill_seconds):
            return self._stuck_recover(now, view, "no kill within the deadline")

        anchors = self.route.anchors_for(view.level)
        if not anchors:
            return self._result(
                GrowthState.TASK, detail="route band has no waypoints")
        if not self.position:
            return self._result(
                GrowthState.ACQUIRE, detail="waiting for a live position")

        waypoint = anchors[self.waypoint_index % len(anchors)]
        target = {"x": waypoint.x, "y": waypoint.y, "z": waypoint.z}
        distance = distance2d(self.position, target)
        if distance <= self.route.recovery.waypoint_arrival_radius:
            if (now - self._no_target_since
                    >= self.route.recovery.waypoint_dwell_seconds):
                self._advance_waypoint(view.level)
                waypoint = anchors[self.waypoint_index % len(anchors)]
                target = {"x": waypoint.x, "y": waypoint.y, "z": waypoint.z}
            else:
                return self._result(
                    GrowthState.ACQUIRE, detail="scanning from a waypoint")

        self._move_toward(view, target, self.profile.combat.approach_step)
        return self._result(
            GrowthState.TRAVEL, "move_to",
            f"waypoint {self.waypoint_index % len(anchors)}")

    def _move_toward(self, view: WorldView, destination: dict,
                     maximum_step: float) -> bool:
        if not view.position:
            return False
        distance = distance2d(view.position, destination)
        if distance <= 0.01:
            return False
        step = min(float(maximum_step), distance)
        dx = float(destination.get("x", 0.0)) - float(view.position.get("x", 0.0))
        dy = float(destination.get("y", 0.0)) - float(view.position.get("y", 0.0))
        length = math.hypot(dx, dy) or 1.0
        x = float(view.position.get("x", 0.0)) + dx / length * step
        y = float(view.position.get("y", 0.0)) + dy / length * step
        z = float(destination.get("z", view.position.get("z", 0.0)))
        return self._safe_action(
            "move_to", lambda: self.adapter.move_to(x, y, z))

    def _move_away(self, view: WorldView, away_from: dict) -> bool:
        if not view.position:
            return False
        if away_from:
            dx = (float(view.position.get("x", 0.0))
                  - float(away_from.get("x", 0.0)))
            dy = (float(view.position.get("y", 0.0))
                  - float(away_from.get("y", 0.0)))
        else:
            dx = -math.cos(view.orientation)
            dy = -math.sin(view.orientation)
        length = math.hypot(dx, dy) or 1.0
        x = (float(view.position.get("x", 0.0))
             + dx / length * self.profile.combat.side_step_distance)
        y = (float(view.position.get("y", 0.0))
             + dy / length * self.profile.combat.side_step_distance)
        z = float(view.position.get("z", 0.0))
        return self._safe_action(
            "move_to", lambda: self.adapter.move_to(x, y, z))

    def _stuck_recover(self, now: float, view: WorldView, reason: str,
                       target: dict | None = None) -> StepResult:
        self._stuck_events += 1
        self._last_stuck_at = now
        self._last_kill_at = now
        self._safe_action("stop_attack", self.adapter.stop_attack)

        if view.position:
            if target and target.get("position"):
                away = target["position"]
                dx = (float(view.position.get("x", 0.0))
                      - float(away.get("x", 0.0)))
                dy = (float(view.position.get("y", 0.0))
                      - float(away.get("y", 0.0)))
            else:
                dx = math.cos(view.orientation + math.pi / 2)
                dy = math.sin(view.orientation + math.pi / 2)
            length = math.hypot(dx, dy) or 1.0
            x = (float(view.position.get("x", 0.0))
                 + dx / length * self.profile.combat.side_step_distance)
            y = (float(view.position.get("y", 0.0))
                 + dy / length * self.profile.combat.side_step_distance)
            z = float(view.position.get("z", 0.0))
            self._safe_action(
                "move_to", lambda: self.adapter.move_to(x, y, z))

        if target is not None and target.get("guid") is not None:
            self._blacklist[int(target["guid"])] = (
                now + self.profile.combat.blacklist_seconds)
        self._advance_waypoint(view.level)
        self._clear_target()
        self._no_target_since = now
        self.log(f"stuck recovery: {reason}")
        return self._result(GrowthState.STUCK, "move_to", reason)

    def _advance_waypoint(self, level: int) -> None:
        anchors = self.route.anchors_for(level)
        if not anchors:
            return
        self.waypoint_index = (
            self.waypoint_index + self.route.recovery.route_change_waypoints
        ) % len(anchors)

    # ------------------------------------------------------------ helpers

    def _clear_target(self) -> None:
        self.target_guid = None
        self._target_since = None
        self._target_health = None
        self._last_damage_at = None
        self._last_attack_at = None
        self._last_cast_at = None

    def _safe_action(self, name: str, action: Callable[[], object]) -> bool:
        try:
            action()
            return True
        except AdapterError as exc:
            self.log(f"{name} failed: {exc}")
            return False

    def _prune_memories(self, now: float) -> None:
        self._blacklist = {
            guid: expiry for guid, expiry in self._blacklist.items()
            if expiry > now}
        if len(self._blacklist) > 256:
            for guid in list(self._blacklist)[:len(self._blacklist) - 256]:
                del self._blacklist[guid]
        self._looted = {
            guid: at for guid, at in self._looted.items() if now - at <= 60.0}
        if len(self._looted) > 256:
            for guid in list(self._looted)[:len(self._looted) - 256]:
                del self._looted[guid]
        self._disabled_spells = {
            spell: until for spell, until in self._disabled_spells.items()
            if until > now}

    def _find_spirit_healer(self, view: WorldView) -> dict | None:
        names = self.route.recovery.spirit_healer_names
        for candidate in view.nearby:
            name = str(candidate.get("name") or "").lower()
            if any(healer in name for healer in names):
                return candidate
        return None

    def _result(self, state: GrowthState, action: str | None = None,
                detail: str | None = None) -> StepResult:
        self._state = state
        self._last_metrics = self.metrics.snapshot(self._now)
        if self.store is not None:
            document = self.metrics.to_dict()
            document["snapshot"] = self._last_metrics
            self.store.save_metrics(document)
        return StepResult(state, action, detail, self._last_metrics)
