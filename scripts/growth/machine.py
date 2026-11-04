"""Deterministic solo growth state machine.

The loop is intentionally small and rule-driven (KTD1). It reads live facts
through :class:`scripts.growth.adapter.WorldAdapter`, walks measured route
waypoints, fights with the class profile's initial skill priorities, recovers
from death/ghost/corpse states, blacklists targets that are unreachable or make
no progress, and records sampled metrics.

Explicit states exist for the whole MVP so later units attach to named
boundaries instead of rewriting the loop:

- ``TOWN`` covers out-of-combat equipment, vendor and training actions.
  ``TASK`` executes the ordered quest route when present and reports levels
  outside every route band. Quest accept/objective/turn-in reconciliation lives
  in :mod:`scripts.growth.quest_runtime`; unsupported content falls back to
  grinding rather than pretending the full U6 quest matrix is implemented.
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
from .routes import Route, TrainingRoute

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
        character_growth=None,
        quests=None,
        role: str = "dps",
        route_offset: int = 0,
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
        self.character_growth = character_growth
        self.quests = quests
        self.role = role
        self.inventory = InventoryRuntime(
            InventoryPolicy(profile.inventory), adapter, store,
            clock=clock, logger=self.log)

        self.epoch: str | None = None
        self.map_id: int | None = None
        self.position: dict = {}
        self.target_guid: int | None = None
        self.route_offset = int(route_offset)
        self.waypoint_index = self.route_offset
        self._training_path_key: str | None = None
        self._training_waypoint: int | None = None
        self._training_vendor_done = False
        self._training_retry_at = 0.0

        self._now = 0.0
        self._last_metrics: dict | None = None
        self._blacklist: dict[int, float] = {}
        self._looted: dict[int, float] = {}
        self._disabled_spells: dict[int, float] = {}
        self._sustained_casts: set[int] = set()
        self._sustained_pending: dict[int, tuple[int, float]] = {}
        self._observed_cast_failures: int | None = None
        self._line_of_sight_failures = 0
        self._target_since: float | None = None
        self._target_health: int | None = None
        self._last_damage_at: float | None = None
        self._last_attack_at: float | None = None
        self._last_cast_at: float | None = None
        self._target_spell_casts: dict[int, float] = {}
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

        if self.quests is not None:
            self.quests.observe_level(view.level)
        questing = (self.quests is not None
                    and self.quests.current() is not None)
        turn_in_pending = questing and (
            self.quests.phase in ("hunt", "turn_in")
            or self.quests.ready_to_turn_in(view))
        objectives_complete = questing and self.quests.ready_to_turn_in(view)
        # Incomplete hunting never walks through a live attacker. A genuinely
        # completed quest keeps the existing bounded turn-in recovery behavior.
        quest_may_run = (not questing or not view.in_combat
                         or self.quests.phase == "turn_in" or objectives_complete)
        if turn_in_pending and quest_may_run:
            starting_turn_in = self.quests.phase != "turn_in"
            if (not starting_turn_in and not view.in_combat
                    and self.target_guid is None):
                inventory = self.inventory.step(
                    now, view, self.profile.class_mask, reserve_slots=1,
                    fallback_position=self.quests.current().turn_in.position,
                    fallback_waypoints=self.quests.current().turn_in.waypoints,
                    vendor_position=self.quests.current().turn_in.vendor_position,
                    vendor_waypoints=self.quests.current().turn_in.vendor_waypoints)
                if inventory is not None:
                    return self._result(
                        GrowthState.TOWN, inventory.action, inventory.detail)
            quest = self.quests.step(now, view)
            if quest is not None and self.quests.phase == "turn_in":
                if starting_turn_in:
                    self._safe_action("stop_attack", self.adapter.stop_attack)
                    self._clear_target()
                return self._result(GrowthState.TASK, quest.action, quest.detail)

        recovery = self._handle_recovery(now, view)
        if recovery is not None:
            return recovery

        if self.cooperation is not None:
            cooperation = self.cooperation.step(
                now, view, self.profile.class_mask, questing=questing)
            if cooperation is not None:
                return self._result(
                    GrowthState.NEGOTIATE, cooperation.action, cooperation.detail)

        if not self.position:
            # Never attack from an assumed origin.
            return self._result(
                GrowthState.ACQUIRE, detail="waiting for a live position")

        if not view.in_combat and self.target_guid is None:
            current_quest = self.quests.current() if questing else None
            inventory_checked = False
            if (questing and (self.profile.inventory.supplies
                              or self.profile.inventory.repair_position is not None)):
                # A depleted combat supply is a hard blocker, not routine town
                # maintenance. Service it before the next acceptor can pull an
                # empty-ammunition character into another zone.
                inventory = self.inventory.step(
                    now, view, self.profile.class_mask,
                    fallback_position=(current_quest.turn_in.position
                                       if current_quest is not None else None),
                    fallback_waypoints=(current_quest.turn_in.waypoints
                                        if current_quest is not None else ()),
                    vendor_position=(current_quest.turn_in.vendor_position
                                     if current_quest is not None else None),
                    vendor_waypoints=(current_quest.turn_in.vendor_waypoints
                                      if current_quest is not None else ()))
                inventory_checked = True
                if inventory is not None:
                    return self._result(
                        GrowthState.TOWN, inventory.action, inventory.detail)
            training = self._training_route(now, view)
            if training is not None:
                trained = self._training_step(now, view, training)
                if trained is not None:
                    return trained
            else:
                self._reset_training_route()
            if questing:
                quest = self.quests.step(now, view)
                if quest is not None:
                    return self._result(GrowthState.TASK, quest.action, quest.detail)
            # Profiles with explicit supplies (for example hunter ammunition)
            # may service them during a quest route. Other classes keep the
            # original rule: routine town maintenance never delays a quest.
            if ((not questing or self.profile.inventory.supplies
                 or self.profile.inventory.repair_position is not None)
                    and not inventory_checked):
                inventory = self.inventory.step(
                    now, view, self.profile.class_mask,
                    fallback_position=(current_quest.turn_in.position
                                       if current_quest is not None else None),
                    fallback_waypoints=(current_quest.turn_in.waypoints
                                        if current_quest is not None else ()),
                    vendor_position=(current_quest.turn_in.vendor_position
                                     if current_quest is not None else None),
                    vendor_waypoints=(current_quest.turn_in.vendor_waypoints
                                      if current_quest is not None else ()))
                if inventory is not None:
                    return self._result(
                        GrowthState.TOWN, inventory.action, inventory.detail)
            nearby_class_trainer = any(
                int(npc.get("npc_flags") or 0) & 0x30 == 0x30
                for npc in view.nearby)
            if ((not questing or nearby_class_trainer)
                    and self.character_growth is not None):
                character = self.character_growth.step(now, view, self.role)
                if character is not None:
                    return self._result(
                        GrowthState.TOWN, character.action, character.detail)

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
        self.waypoint_index = self.route_offset
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
        self.waypoint_index = self.route_offset
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
        self._lost_targets = 0
        if target.get("dead") or int(target.get("health") or 0) <= 0:
            return self._loot(now, view, self.target_guid)

        health = int(target.get("health") or 0)
        if self._target_health is not None and health < self._target_health:
            self._last_damage_at = now
        self._target_health = health

        line_of_sight_failed = self._observe_cast_failures(now, view)
        target_position = target.get("position") or {}
        distance = distance2d(self.position, target_position)
        dz = abs(float(self.position.get("z", 0.0))
                 - float(target_position.get("z", 0.0)))
        preferred_range = (self.profile.combat.preferred_range
                           or self.profile.combat.melee_range)

        if line_of_sight_failed and distance > self.profile.combat.melee_range:
            self._line_of_sight_failures += 1
            if self._line_of_sight_failures >= 2:
                self._blacklist[int(self.target_guid)] = (
                    now + self.profile.combat.blacklist_seconds)
                blocked_guid = self.target_guid
                self._clear_target()
                return self._result(
                    GrowthState.ACQUIRE,
                    detail=f"line-of-sight blocked target {blocked_guid}; switching")
            self._move_toward(view, target_position,
                              self.profile.combat.approach_step)
            return self._result(
                GrowthState.COMBAT, "move_to",
                "repositioning after a line-of-sight cast failure")

        vertical_blocked = (dz > self.profile.combat.dz_tolerance
                            and preferred_range <= self.profile.combat.melee_range)
        if distance > preferred_range or vertical_blocked:
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

        if ((self._last_attack_at is not None
                or self._last_cast_at is not None)
                and self._last_damage_at is not None
                and now - self._last_damage_at
                > self.profile.combat.stall_seconds):
            return self._stuck_recover(
                now, view, "combat stall: target took no damage", target)

        desired = bearing(self.position, target_position)
        if abs(angle_diff(desired, view.orientation)) > 0.05:
            self._safe_action(
                "turn_to", lambda: self.adapter.turn_to(desired))
        melee_fallback = (
            self.profile.combat.preferred_range is not None
            and preferred_range > self.profile.combat.melee_range
            and distance <= self.profile.combat.melee_range)
        if (self.profile.combat.auto_attack or melee_fallback) and (
                self._last_attack_at is None
                or now - self._last_attack_at
                >= self.profile.combat.attack_reissue_seconds):
            self._safe_action(
                "attack", lambda: self.adapter.attack(self.target_guid))
            self._last_attack_at = now

        skill = self._choose_skill(view, now)
        cast_due = skill is not None and (
            self._last_cast_at is None
            or now - self._last_cast_at
            >= self.profile.combat.cast_interval_seconds)
        if (cast_due
                and preferred_range > self.profile.combat.melee_range
                and not self._safe_action(
                    "stop_movement", self.adapter.stop_movement)):
            return self._result(
                GrowthState.COMBAT,
                detail="ranged cast is waiting for a confirmed movement stop")
        if cast_due:
            cast_target = self.target_guid if skill.requires_target else view.guid
            if cast_target is not None:
                if (skill.ammo_item_id is not None
                        and not self._safe_action(
                            "set_ammo",
                            lambda: self.adapter.set_ammo(skill.ammo_item_id))):
                    return self._result(
                        GrowthState.COMBAT,
                        detail="ranged cast is waiting for ammunition selection")
                ok = self._safe_action(
                    "cast",
                    lambda: self.adapter.cast(skill.spell_id, cast_target))
                if ok:
                    self._last_cast_at = now
                    self._target_spell_casts[skill.spell_id] = now
                    if skill.sustained:
                        failures = int(view.combat.get("cast_failures") or 0)
                        self._sustained_pending[skill.spell_id] = (failures, now)
                else:
                    self._disabled_spells[skill.spell_id] = (
                        now + self.profile.combat.spell_disable_seconds)
        detail = f"fight {target.get('name') or self.target_guid}"
        if skill is not None:
            detail += f" skill={skill.name}"
        return self._result(GrowthState.COMBAT, "attack", detail)

    def _observe_cast_failures(self, now: float, view: WorldView) -> bool:
        """Track server cast replies and report each new line-of-sight failure."""
        failures = int(view.combat.get("cast_failures") or 0)
        previous_failures = self._observed_cast_failures
        self._observed_cast_failures = failures
        last_error = str(view.combat.get("last_error") or "")
        line_of_sight_failed = (
            previous_failures is not None
            and failures > previous_failures
            and last_error.endswith("result 47"))
        for spell_id, (baseline, sent_at) in list(self._sustained_pending.items()):
            if failures > baseline:
                del self._sustained_pending[spell_id]
                self._sustained_casts.discard(spell_id)
                self._disabled_spells[spell_id] = (
                    now + self.profile.combat.spell_disable_seconds)
            elif now - sent_at >= 1.0:
                del self._sustained_pending[spell_id]
                self._sustained_casts.add(spell_id)
        return line_of_sight_failed

    def _choose_skill(self, view: WorldView, now: float) -> Skill | None:
        fraction = (view.health / view.max_health
                    if view.max_health > 0 else 1.0)
        available = view.power(self.profile.resource)
        known_spells = view.combat.get("known_spells")
        target = self._current_target_unit(view)
        target_distance = (distance2d(
            self.position, target.get("position") or {})
            if target is not None else None)
        for skill in self.profile.skill_order(view.level):
            last_cast = self._target_spell_casts.get(skill.spell_id)
            if (skill.reapply_seconds is not None and last_cast is not None
                    and now - last_cast < skill.reapply_seconds):
                continue
            if skill.sustained and skill.spell_id in self._sustained_casts:
                continue
            if (known_spells is not None
                    and (skill.spell_id not in known_spells
                         or known_spells[skill.spell_id])):
                continue
            if skill.requires_target and self.target_guid is None:
                continue
            if not skill.requires_target and view.guid is None:
                continue
            if (target_distance is not None
                    and skill.min_range is not None
                    and target_distance < skill.min_range):
                continue
            if (target_distance is not None
                    and skill.max_range is not None
                    and target_distance > skill.max_range):
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
        if self._lost_targets < self.profile.combat.lost_target_limit:
            return self._result(
                GrowthState.ACQUIRE,
                detail=(f"target {guid} missing from nearby snapshot "
                        f"({self._lost_targets}/{self.profile.combat.lost_target_limit})"))
        if guid is not None:
            self._blacklist[int(guid)] = (
                now + self.profile.combat.blacklist_seconds)
        return self._stuck_recover(
            now, view, "target repeatedly lost")

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
        required_entries = (self.quests.target_entries()
                            if self.quests is not None else frozenset())
        best = None
        best_score = None
        fallback_best = None
        fallback_score = None
        blocked_quest_target = False
        quest_candidates = []
        attackers = []
        for candidate in view.nearby:
            guid = candidate.get("guid")
            entry = int(candidate.get("entry") or 0)
            if guid is None:
                continue
            if int(guid) in self._blacklist:
                if entry in required_entries:
                    blocked_quest_target = True
                continue
            if view.guid is not None and int(guid) == int(view.guid):
                continue
            attacking_self = (
                candidate.get("in_combat")
                and candidate.get("target_guid") is not None
                and view.guid is not None
                and int(candidate["target_guid"]) == int(view.guid))
            if candidate.get("in_combat") and not attacking_self:
                continue
            if attacking_self:
                if (candidate.get("dead")
                        or int(candidate.get("health") or 0) <= 0
                        or (self.route.targets.unit_types
                            and int(candidate.get("type") or 0)
                            not in self.route.targets.unit_types)):
                    continue
            elif not self._is_hostile(candidate, view.level, required_entries):
                continue
            if not candidate.get("has_position"):
                continue
            position = candidate.get("position") or {}
            distance = distance2d(self.position, position)
            max_distance = self.profile.combat.engage_radius * (
                2 if attacking_self else 1)
            if distance > max_distance:
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
            if attacking_self:
                attackers.append((distance, int(guid), candidate))
                continue
            if required_entries and entry not in required_entries:
                if fallback_score is None or score < fallback_score:
                    fallback_best, fallback_score = candidate, score
                continue
            if required_entries:
                quest_candidates.append((score, int(guid), candidate))
            if best_score is None or score < best_score:
                best, best_score = candidate, score
        if attackers:
            attackers.sort(key=lambda row: (row[0], row[1]))
            return attackers[0][2]
        if quest_candidates:
            quest_candidates.sort(key=lambda row: (row[0], row[1]))
            return quest_candidates[self.route_offset % len(quest_candidates)][2]
        if required_entries and blocked_quest_target:
            return fallback_best
        return best

    def _nearest_quest_target(
            self, view: WorldView, required_entries: frozenset[int]) -> dict | None:
        candidates = []
        for candidate in view.nearby:
            guid = candidate.get("guid")
            if guid is None or int(guid) in self._blacklist:
                continue
            if int(candidate.get("entry") or 0) not in required_entries:
                continue
            if candidate.get("in_combat") or not self._is_hostile(
                    candidate, view.level, required_entries):
                continue
            distance = distance2d(self.position, candidate.get("position") or {})
            candidates.append((distance, int(guid), candidate))
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[self.route_offset % len(candidates)][2]

    def _is_hostile(self, candidate: dict, level: int,
                    required_entries: frozenset[int] = frozenset()) -> bool:
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
        if int(candidate.get("entry") or 0) in required_entries:
            # The versioned world-DB quest row is an explicit hostility source;
            # route name/faction hints are only the open-grinding fallback.
            return True
        return any(hint in name for hint in hints.hostile_names)

    def _nearest_hostile(self, view: WorldView) -> dict | None:
        best = None
        best_distance = self.profile.combat.engage_radius
        for candidate in view.nearby:
            attacking_self = (
                candidate.get("in_combat")
                and candidate.get("target_guid") is not None
                and view.guid is not None
                and int(candidate["target_guid"]) == int(view.guid))
            if attacking_self:
                if (candidate.get("dead")
                        or int(candidate.get("health") or 0) <= 0
                        or not candidate.get("has_position")):
                    continue
            elif not self._is_hostile(candidate, view.level):
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
        self._target_spell_casts.clear()
        self._sustained_casts.clear()
        self._sustained_pending.clear()
        self._line_of_sight_failures = 0
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

    # --------------------------------------------------------- training route

    def _training_route(self, now: float, view: WorldView) -> TrainingRoute | None:
        if now < self._training_retry_at:
            return None
        known = view.combat.get("known_spells")
        if known is None:
            return None
        missing = any(
            skill.min_level <= view.level and skill.spell_id not in known
            for skill in self.profile.skills)
        return self.route.training_for(self.profile.class_name) if missing else None

    def _training_step(self, now: float, view: WorldView,
                       training: TrainingRoute) -> StepResult | None:
        if training.vendor_position is not None and not self._training_vendor_done:
            inventory = self.inventory.step(
                now, view, self.profile.class_mask,
                fallback_position=training.vendor_position,
                fallback_waypoints=training.vendor_waypoints,
                vendor_position=training.vendor_position,
                vendor_waypoints=training.vendor_waypoints)
            if inventory is not None:
                return self._result(
                    GrowthState.TOWN, inventory.action, inventory.detail)
            if distance2d(view.position, training.vendor_position) <= 8.0:
                if training.minimum_money > 0:
                    try:
                        money = self.adapter.get_inventory().money
                    except AdapterError as exc:
                        return self._result(
                            GrowthState.TOWN,
                            detail=f"training funds are unavailable: {exc}")
                    if money < training.minimum_money:
                        # Selling everything still may not cover the spell plus
                        # reserve. Resume grinding briefly instead of walking to
                        # a trainer and waiting there with unaffordable rows.
                        self._training_retry_at = now + 120.0
                        self._reset_training_route()
                        return None
                self._training_vendor_done = True
                self._training_path_key = None
                self._training_waypoint = None
            else:
                return self._travel_training(
                    view, "vendor", training.vendor_position,
                    training.vendor_waypoints, "returning to sell before training")

        if self.character_growth is not None:
            character = self.character_growth.step(now, view, self.role)
            if character is not None:
                return self._result(
                    GrowthState.TOWN, character.action, character.detail)
        if distance2d(view.position, training.position) <= 8.0:
            return self._result(
                GrowthState.TOWN, detail="waiting for the configured class trainer")
        return self._travel_training(
            view, "trainer", training.position, training.waypoints,
            "returning for a missing class skill")

    def _travel_training(self, view: WorldView, key: str, position: dict,
                         waypoints: tuple[dict, ...], detail: str) -> StepResult:
        if key != self._training_path_key:
            self._training_path_key = key
            self._training_waypoint = (min(
                range(len(waypoints)),
                key=lambda index: distance2d(view.position, waypoints[index]))
                if waypoints else None)
        destination = position
        while self._training_waypoint is not None and \
                self._training_waypoint < len(waypoints):
            waypoint = waypoints[self._training_waypoint]
            if distance2d(view.position, waypoint) > 4.0:
                destination = waypoint
                break
            self._training_waypoint += 1
        if not self._move_toward(
                view, destination, self.profile.combat.approach_step):
            return self._result(
                GrowthState.STUCK, detail=f"training route blocked: {detail}")
        return self._result(GrowthState.TRAVEL, "move_to", detail)

    def _reset_training_route(self) -> None:
        self._training_path_key = None
        self._training_waypoint = None
        self._training_vendor_done = False

    # --------------------------------------------------------- navigation

    def _no_target(self, now: float, view: WorldView) -> StepResult:
        if self._no_target_since is None:
            self._no_target_since = now

        anchors = self.route.anchors_for(view.level)
        if not anchors:
            return self._result(
                GrowthState.TASK, detail="route band has no waypoints")
        if not self.position:
            return self._result(
                GrowthState.ACQUIRE, detail="waiting for a live position")

        required_entries = (
            self.quests.target_entries()
            if self.quests is not None else frozenset())
        if required_entries:
            target = self._nearest_quest_target(view, required_entries)
            if target is not None:
                self._move_toward(
                    view, target.get("position") or {},
                    self.profile.combat.approach_step)
                return self._result(
                    GrowthState.TRAVEL, "move_to",
                    f"seeking quest target {target.get('name')}")
            anchor = self.quests.target_position(self.position)
            if anchor is not None:
                if distance2d(self.position, anchor) > \
                        self.route.recovery.waypoint_arrival_radius:
                    self._move_toward(
                        view, anchor, self.profile.combat.approach_step)
                    return self._result(
                        GrowthState.TRAVEL, "move_to",
                        "returning to the quest objective area")
                return self._result(
                    GrowthState.ACQUIRE,
                    detail="scanning the quest objective area; no physical stuck inferred")

        # Lack of kills while travelling to a quest area is not a physical
        # stuck event. Keep the generic grind watchdog, but do not fire it for
        # an active objective whose targets may simply be out of sensing range.
        if (self._last_kill_at is not None
                and now - self._last_kill_at
                >= self.profile.combat.no_kill_seconds):
            return self._stuck_recover(now, view, "no kill within the deadline")

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

        if not self._move_toward(
                view, target, self.profile.combat.approach_step):
            return self._stuck_recover(now, view, "waypoint movement blocked")
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
        dx = float(destination.get("x", 0.0)) - float(view.position.get("x", 0.0))
        dy = float(destination.get("y", 0.0)) - float(view.position.get("y", 0.0))
        length = math.hypot(dx, dy) or 1.0
        # Each short segment is grounded by the move module. A distant target's
        # Z is not a valid estimate on a terrace, cliff or sloped transition.
        z = float(view.position.get("z", destination.get("z", 0.0)))
        steps = []
        for candidate in (12.0, 8.0, 4.0):
            step = min(float(maximum_step), candidate, distance)
            if step > 0.01 and step not in steps:
                steps.append(step)
        for index, step in enumerate(steps):
            x = float(view.position.get("x", 0.0)) + dx / length * step
            y = float(view.position.get("y", 0.0)) + dy / length * step
            try:
                self.adapter.move_to(x, y, z)
                return True
            except AdapterError as exc:
                self.log(f"move_to failed: {exc}")
                error = str(exc)
                retryable = ("terrain height" in error
                             and "differs from estimated" in error)
                if not retryable:
                    break
                if index == len(steps) - 1:
                    try:
                        self.adapter.navigate(
                            float(destination.get("x", 0.0)),
                            float(destination.get("y", 0.0)),
                            float(destination.get("z", z)))
                        self.log("move_to blocked; using terrain navigation")
                        return True
                    except AdapterError as nav_exc:
                        self.log(f"navigate failed: {nav_exc}")
                        return False
        return False

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
        self._target_spell_casts.clear()

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
