#!/usr/bin/env python3
"""Autonomous grinding agent for the agent-wow workspace.

Runs inside the Colima VM next to `agent-wow char play`. It reads the live
world through the sense/move/combat modules over the local JSON-RPC endpoint
and drives the character toward TARGET_LEVEL. It never handles credentials and
only talks to localhost.

Usage (inside the VM):
    python3 ~/aw/scripts/leveling-agent.py [target_level]
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

RPC_URL = os.environ.get("AGENT_WOW_RPC", "http://127.0.0.1:8086/rpc")
CHARACTER = "Reachbot"
TARGET_LEVEL = int(sys.argv[1]) if len(sys.argv) > 1 else 10
HOSTILE_FACTION = 32
# Creature names worth attacking in Elwynn Forest. Faction alone is not enough:
# Defias thugs share faction 7 with harmless critters such as the wild turkey.
HOSTILE_NAMES = (
    "wolf",
    "diseased",
    "defias",
    "kobold",
    "murloc",
    "gnoll",
    "boar",
    "prowler",
    "bear",
    "spider",
    "harvest watcher",
)
CRITTER_NAMES = ("turkey", "rabbit", "deer", "sheep", "frog", "chicken", "cat", "dog")
ENGAGE_RADIUS = 40.0  # beyond this, walking costs more than the kill pays
MELEE_RANGE = 4.0
APPROACH_STEP = 12.0
LOOP_SLEEP = 0.6
HEROIC_STRIKE = 78
RAGE_COST = 150  # UNIT_FIELD_POWER1 stores rage x10
FLEE_HP = 0.45  # run away below this fraction of max health
REGEN_HP = 0.65  # do not start a new fight below this fraction

# Where to look for mobs, banded by character level. Within a band the agent
# walks to the nearest anchor and scans; outside it, mobs pay too little XP
# (a level-1 wolf is worth 14 XP to a level 5 character, a level-3 Defias Thug
# about 30). Verified in-game with a sense sweep down the Northshire valley.
HUNT_GROUNDS = [
    (
        1,
        [
            (-8950.0, -80.0, 88.0),
            (-8975.0, -65.0, 91.0),
            (-8920.0, -80.0, 88.0),
            (-8960.0, -50.0, 91.5),
            (-8900.0, -100.0, 82.5),
            (-8990.0, -120.0, 84.0),
            (-9000.0, -160.0, 82.0),
            (-8920.0, -150.0, 82.0),
        ],
    ),
    (
        5,
        [
            (-9028.0, -271.0, 74.0),
            (-9039.0, -273.0, 74.0),
            (-9030.0, -289.0, 75.0),
            (-9050.0, -300.0, 73.0),
            (-9005.0, -250.0, 73.0),
        ],
    ),
]
# Mobs this far below the character pay almost nothing.
LEVEL_FLOOR = 3


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S ") + message, flush=True)


class Client:
    def __init__(self, url: str):
        self.url = url
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 90.0):
        self._id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": method,
                "params": params or {},
            },
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        # The worldserver drops sessions on its own schedule. Keep retrying
        # instead of exiting: an unattended run should resume when the session
        # is back rather than needing a human to restart it.
        next_log = 0.0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError) as err:
                if time.time() >= next_log:
                    log(f"{method}: {err}; retrying")
                    next_log = time.time() + 60
                time.sleep(5)
                continue
            error = payload.get("error")
            if error is None:
                break
            message = error.get("message") or ""
            # A dropped session surfaces as a canceled gRPC call, not a bad
            # request; treat it like a disconnected transport.
            if any(word in message for word in ("Canceled", "Unavailable", "DeadlineExceeded")):
                if time.time() >= next_log:
                    log(f"{method}: {message}; retrying")
                    next_log = time.time() + 60
                time.sleep(5)
                continue
            raise RuntimeError(f"{method}: {error.get('code')} {message}")
        return payload.get("result", {})

    def state(self) -> dict:
        return self.call("combat.get_state")

    def position(self) -> dict:
        return self.call("move.get_position").get("position", {})

    def nearby(self) -> list[dict]:
        return self.call("sense.get_nearby", {"radius": 0, "limit": 200}).get(
            "units", []
        )

    def move_to(self, x: float, y: float, z: float) -> dict:
        return self.call(
            "move.move_to", {"x": x, "y": y, "z": z}, timeout=45
        )

    def turn_to(self, orientation: float) -> None:
        self.call("move.turn_to", {"orientation": orientation}, timeout=15)

    def attack(self, guid: int) -> dict:
        return self.call("combat.attack", {"guid": str(guid)}, timeout=20)

    def stop_attack(self) -> None:
        self.call("combat.stop_attack", {}, timeout=15)

    def cast(self, spell_id: int, target: int) -> dict:
        return self.call(
            "combat.cast",
            {"spell_id": spell_id, "targetGuid": str(target)},
            timeout=15,
        )

    def release_spirit(self) -> dict:
        return self.call("combat.release_spirit", {}, timeout=20)

    def reclaim_corpse(self) -> dict:
        return self.call("combat.reclaim_corpse", {}, timeout=20)

    def resurrect_at_spirit_healer(self, guid: int) -> dict:
        return self.call(
            "combat.resurrect_at_spirit_healer",
            {"healerGuid": str(guid)},
            timeout=25,
        )


def distance2d(a: dict, b: dict) -> float:
    return math.hypot(a.get("x", 0) - b.get("x", 0), a.get("y", 0) - b.get("y", 0))


def distance3d(a: dict, b: dict) -> float:
    return math.sqrt(
        (a.get("x", 0) - b.get("x", 0)) ** 2
        + (a.get("y", 0) - b.get("y", 0)) ** 2
        + (a.get("z", 0) - b.get("z", 0)) ** 2
    )


def bearing(a: dict, b: dict) -> float:
    # The server compares facing with atan2(dy, dx). The other way round
    # (atan2(dx, dy)) is up to 90 degrees off, which is why swings were
    # refused as "behind the character" while the character faced a target.
    return math.atan2(b.get("y", 0) - a.get("y", 0), b.get("x", 0) - a.get("x", 0))


def angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def unit_alive(u: dict | None) -> bool:
    return bool(
        u
        and u.get("hasPosition")
        and u.get("health", 0) > 0
        and not u.get("dead")
        and u.get("type") == 3
    )


def is_hostile(u: dict, level: int) -> bool:
    if not unit_alive(u):
        return False
    if u.get("level", 0) > level + 1:
        return False
    if u.get("level", 0) < max(1, level - LEVEL_FLOOR):
        return False
    name = (u.get("name") or "").lower()
    if any(critter in name for critter in CRITTER_NAMES):
        return False
    if u.get("faction") == HOSTILE_FACTION:
        return True
    return any(hint in name for hint in HOSTILE_NAMES)


def choose_target(units: list[dict], pos: dict, level: int) -> dict | None:
    hostiles = [u for u in units if is_hostile(u, level)]
    best = None
    best_score = None
    for u in hostiles:
        d = distance2d(pos, u.get("position", {}))
        if d > ENGAGE_RADIUS:
            continue
        # Prefer isolated mobs; a second hostile within 8 yards means adds.
        crowd = 0
        for other in hostiles:
            if other["guid"] == u["guid"]:
                continue
            if distance2d(u["position"], other["position"]) < 8:
                crowd += 1
        # Same-level mobs pay the most XP; grey ones are not worth the trip.
        level_delta = u.get("level", 1) - level
        level_score = -level_delta * 6 if level_delta else 0
        score = d + min(crowd, 2) * 14 + level_score
        if best_score is None or score < best_score:
            best, best_score = u, score
    return best


def find_unit(units: list[dict], guid: int) -> dict | None:
    for u in units:
        if int(u.get("guid", 0)) == guid:
            return u
    return None


def nearest_hostile(units: list[dict], pos: dict, level: int, radius: float = 12.0) -> dict | None:
    best = None
    best_d = radius
    for u in units:
        if not is_hostile(u, level):
            continue
        d = distance2d(pos, u.get("position", {}))
        if d < best_d:
            best, best_d = u, d
    return best


def wait_for_health(client: Client, minimum: int, deadline: float) -> dict:
    state = client.state()
    while time.time() < deadline:
        if state.get("selfDead"):
            return state
        if state.get("selfHealth", 0) >= minimum:
            return state
        time.sleep(0.8)
        state = client.state()
    return state


def travel_to(client: Client, dest: dict, step: float = 25.0, max_steps: int = 12) -> dict:
    """Walk to a point that may be farther than one MoveTo allows.

    MoveTo rejects distances beyond 40 yards, and a ghost can land farther than
    that from the spirit healer, so long trips are split into steps.
    """
    here = client.position()
    for _ in range(max_steps):
        here = client.position()
        dx = dest.get("x", 0) - here.get("x", 0)
        dy = dest.get("y", 0) - here.get("y", 0)
        d = math.hypot(dx, dy)
        if d <= 2.5:
            return here
        take = min(step, d)
        result = client.move_to(
            here.get("x", 0) + dx / d * take,
            here.get("y", 0) + dy / d * take,
            dest.get("z", here.get("z", 0)),
        )
        here = result.get("position", here)
    return here


def grounds_for(level: int) -> list[tuple[float, float, float]]:
    """Anchors for the character's level band, highest band that is unlocked."""
    chosen = HUNT_GROUNDS[0][1]
    for min_level, anchors in HUNT_GROUNDS:
        if level >= min_level:
            chosen = anchors
    return chosen


def roam(client: Client, pos: dict, level: int) -> None:
    """Walk toward the nearest anchor that is not already underfoot.

    A MoveTo step is capped at 40 yards, so a distant anchor is approached in
    chunks of at most 30.
    """
    candidates = sorted(
        grounds_for(level),
        key=lambda h: math.hypot(h[0] - pos.get("x", 0), h[1] - pos.get("y", 0)),
    )
    for x, y, z in candidates:
        d = math.hypot(x - pos.get("x", 0), y - pos.get("y", 0))
        if d < 6:
            continue
        if d > 30:
            x = pos.get("x", 0) + (x - pos.get("x", 0)) / d * 30
            y = pos.get("y", 0) + (y - pos.get("y", 0)) / d * 30
            z = pos.get("z", z)
        log(f"roam -> ({x:.0f}, {y:.0f})")
        client.move_to(x, y, z)
        return
    time.sleep(2)


def loot_corpse(client: Client, guid: int, errors_seen: int) -> int:
    """Ask the loot module to empty a corpse we just killed.

    The module opens the window and takes every row asynchronously, so this
    only reports refusals; the items and copper show up in loot.get_state.
    Returns the cumulative error count so the caller can log only new ones.
    """
    try:
        state = client.call("loot.loot", {"guid": str(guid)})
    except RuntimeError as err:
        # An older session may not have the loot module loaded. That is not
        # worth failing the run for; keep grinding and loot nothing.
        if "Unimplemented" in str(err) or "not found" in str(err):
            return errors_seen
        log(f"loot guid={guid}: {err}")
        return errors_seen
    errors = int(state.get("errors", 0))
    if errors > errors_seen:
        log(f"loot refused ({state.get('lastError') or 'unknown'}) guid={guid}")
    return errors


def main() -> int:
    client = Client(RPC_URL)

    identity = client.call("sense.get_self").get("unit", {})
    if identity.get("name") != CHARACTER:
        log(f"refusing to run: character is {identity.get('name')!r}, expected {CHARACTER!r}")
        return 2

    log(
        f"agent start: level={identity.get('level')} "
        f"hp={identity.get('health')}/{identity.get('maxHealth')} "
        f"xp={identity.get('xp')}/{identity.get('nextLevelXp')} target_level={TARGET_LEVEL}"
    )

    started_at = time.time()
    earned_xp = 0
    metric_level = int(identity.get("level", 0))
    metric_xp = int(identity.get("xp", 0))
    metric_next_xp = int(identity.get("nextLevelXp", 0))
    combat_stalls = 0

    target_guid: int | None = None
    target = None
    kills_seen = 0
    kills_this_level = 0
    last_level = 0
    last_health = None
    last_log = 0.0
    last_spell = 0.0
    last_attack_at = 0.0
    refusals_seen: int | None = None
    spell_known = True
    last_kill_at = time.time()
    last_target_hp = None
    stall_since = 0.0
    kill_times: dict[int, float] = {}
    blocked: dict[int, float] = {}
    loot_errors = 0

    while True:
        now = time.time()
        state = client.state()
        level = state.get("selfLevel", 0)
        current_xp = int(state.get("selfXp", 0))
        current_next_xp = int(state.get("selfNextLevelXp", 0))
        if level == metric_level and current_xp >= metric_xp:
            earned_xp += current_xp - metric_xp
        elif level == metric_level + 1:
            earned_xp += max(0, metric_next_xp - metric_xp) + current_xp
        elif level != metric_level:
            log(f"metrics: level jump {metric_level} -> {level}; XP delta unavailable")
        metric_level = level
        metric_xp = current_xp
        metric_next_xp = current_next_xp
        elapsed_hours = max((now - started_at) / 3600, 1 / 3600)
        xp_per_hour = earned_xp / elapsed_hours
        kills_per_hour = kills_seen / elapsed_hours
        if level >= TARGET_LEVEL:
            log(
                f"target level reached: {level} xp_h={xp_per_hour:.0f} "
                f"kills_h={kills_per_hour:.1f} deaths={state.get('deaths', 0)} "
                f"stalls={combat_stalls}"
            )
            client.stop_attack()
            return 0
        if state.get("selfDead"):
            log("character died; releasing spirit")
            try:
                client.release_spirit()
            except RuntimeError as err:
                log(f"release failed: {err}")
                return 3
            time.sleep(3)

            # The ghost lands at the graveyard; its units may take a moment to
            # stream in, so keep looking before giving up.
            healer = None
            deadline = time.time() + 30
            while time.time() < deadline and healer is None:
                healer = next(
                    (
                        u
                        for u in client.nearby()
                        if (u.get("name") or "").lower() in ("spirit healer", "幽灵医者")
                    ),
                    None,
                )
                if healer is None:
                    time.sleep(1)
            if healer is None:
                log("no spirit healer appeared at the graveyard; stopping")
                return 3

            # Gossip needs the ghost within interaction range (~5 yards).
            healer_pos = healer.get("position", {})
            here = client.position()
            if distance2d(here, healer_pos) > 3.5:
                log(
                    f"walking ghost to spirit healer "
                    f"({healer_pos.get('x', 0):.0f}, {healer_pos.get('y', 0):.0f}) "
                    f"from {distance2d(here, healer_pos):.0f} yd"
                )
                travel_to(client, healer_pos)
            for attempt in range(3):
                log(
                    f"resurrecting at spirit healer "
                    f"{healer.get('guid')} (attempt {attempt + 1})"
                )
                try:
                    client.resurrect_at_spirit_healer(int(healer["guid"]))
                except RuntimeError as err:
                    log(f"spirit healer call failed: {err}")
                wait_deadline = time.time() + 6
                while time.time() < wait_deadline:
                    time.sleep(1)
                    state = client.state()
                    if not state.get("selfDead") and state.get("selfHealth", 0) > 0:
                        break
                if not state.get("selfDead") and state.get("selfHealth", 0) > 0:
                    break
            if state.get("selfDead"):
                log("still dead after resurrection attempts; stopping for inspection")
                return 3
            log(
                f"revived hp={state.get('selfHealth')}/{state.get('selfMaxHealth')}; "
                "retreating to regen"
            )
            pos = client.position()
            roam(client, pos, level)
            wait_for_health(client, state.get("selfMaxHealth", 60) * 0.9, time.time() + 40)
            target_guid = None
            target = None
            continue

        if last_level and level > last_level:
            log(
                f"LEVEL UP {last_level} -> {level} "
                f"xp={state.get('selfXp')}/{state.get('selfNextLevelXp')} "
                f"hp={state.get('selfHealth')}/{state.get('selfMaxHealth')}"
            )
            kills_this_level = 0
        last_level = level

        if state.get("kills", 0) > kills_seen:
            kills_seen = state["kills"]
            kills_this_level += 1
            last_kill_at = now
            log(
                f"kill #{kills_seen} (level {kills_this_level} this level) "
                f"xp={state.get('selfXp')}/{state.get('selfNextLevelXp')} "
                f"lastXp={state.get('lastXp')} hp={state.get('selfHealth')}/{state.get('selfMaxHealth')}"
            )
            if target_guid is not None:
                guid = int(target_guid)
                kill_times[guid] = time.time()
                loot_errors = loot_corpse(client, guid, loot_errors)
            target_guid = None
            target = None
            last_target_hp = None
            stall_since = time.time()

        if refusals_seen is None:
            refusals_seen = state.get("refusals", 0)
        elif state.get("refusals", 0) > refusals_seen:
            refusals_seen = state.get("refusals", 0)
            reason = state.get("lastError") or "refused"
            log(f"attack refused: {reason}")
            if target is not None:
                tp = target.get("position", {})
                if "range" in reason:
                    client.move_to(tp.get("x", 0), tp.get("y", 0), tp.get("z", 0))
                if "behind" in reason:
                    client.turn_to(bearing(client.position(), tp))
                last_attack_at = 0.0

        pos = client.position()
        units = client.nearby()
        # A just-killed corpse lingers in the snapshot for a tick or two; do not
        # "engage" it again and burn a cycle waiting for it to be declared dead.
        units = [
            u
            for u in units
            if not 0 <= now - kill_times.get(int(u.get("guid", 0)), -1e9) < 20
            and not 0 <= now - blocked.get(int(u.get("guid", 0)), -1e9) < 45
        ]

        # Refresh the engaged target from the current snapshot.
        if target_guid is not None:
            target = find_unit(units, target_guid)
            if target is None or not unit_alive(target):
                if state.get("targetDead") or (target and target.get("health", 0) == 0):
                    log(f"target {target_guid} is dead")
                else:
                    log(f"target {target_guid} lost")
                target_guid = None
                target = None
                last_target_hp = None
                stall_since = time.time()
                if state.get("attacking"):
                    client.stop_attack()
                time.sleep(0.5)
                continue

        # A stall is combat where the target's health never moves. Reset the
        # swing, and drop the target if two resets fail to produce damage. The
        # sense snapshot is authoritative: the combat module's target health
        # stays at zero until the target takes its first damage.
        target_hp = target.get("health") if target is not None else None
        if target is not None and target_hp is not None and target_hp != last_target_hp:
            last_target_hp = target_hp
            stall_since = time.time()

        # Flee when badly hurt. The starting wolves hit for little, but a
        # second mob can still end an unattended run.
        low_health = state.get("selfMaxHealth", 0) > 0 and state.get("selfHealth", 0) <= max(
            12, state.get("selfMaxHealth", 0) * FLEE_HP
        )
        if low_health and target is not None:
            log(f"low health {state.get('selfHealth')}/{state.get('selfMaxHealth')}; fleeing")
            client.stop_attack()
            away = target.get("position", {})
            dx = pos.get("x", 0) - away.get("x", 0)
            dy = pos.get("y", 0) - away.get("y", 0)
            length = math.hypot(dx, dy) or 1.0
            x = pos.get("x", 0) + dx / length * 25
            y = pos.get("y", 0) + dy / length * 25
            client.move_to(x, y, pos.get("z", 0))
            # Do not wait here: the next low-health iteration keeps kiting if
            # the mob is still pursuing, and rests only when it is safe.
            target_guid = None
            target = None
            last_target_hp = None
            stall_since = time.time()
            continue

        if target is None:
            max_hp = state.get("selfMaxHealth", 0)
            health = state.get("selfHealth", 0)
            if max_hp and health < max_hp * REGEN_HP:
                threat = nearest_hostile(units, pos, level)
                if threat is not None or state.get("inCombat"):
                    # Something is still chewing on us: standing still to regen
                    # only feeds it. Walk away until the attacker leashes.
                    if threat is not None:
                        away = threat.get("position", {})
                        dx = pos.get("x", 0) - away.get("x", 0)
                        dy = pos.get("y", 0) - away.get("y", 0)
                        length = math.hypot(dx, dy) or 1.0
                        log(f"resting under attack from {threat.get('name')}; kiting")
                        client.move_to(
                            pos.get("x", 0) + dx / length * 20,
                            pos.get("y", 0) + dy / length * 20,
                            pos.get("z", 0),
                        )
                    else:
                        roam(client, pos, level)
                    continue
                # Out-of-combat regen is slow but free. Starting another fight
                # below the gate is how the previous runs died.
                if now - last_log > 10:
                    log(f"resting hp={health}/{max_hp} before next fight")
                    last_log = now
                time.sleep(1.5)
                continue

            candidate = choose_target(units, pos, level)
            if candidate is None:
                if now - last_log > 5:
                    log("no hostile in range; roaming")
                    last_log = now
                roam(client, pos, level)
                time.sleep(1.0)
                continue
            target = candidate
            target_guid = int(candidate["guid"])
            last_target_hp = candidate.get("health")
            stall_since = time.time()
            log(
                f"engage {candidate.get('name')} guid={target_guid} "
                f"lvl={candidate.get('level')} hp={candidate.get('health')} "
                f"dist={distance2d(pos, candidate['position']):.1f}"
            )

        # The position at the top of the loop predates any walking done below,
        # so read it again before deciding range and facing.
        pos = client.position()
        target_pos = target.get("position", {})
        d = distance2d(pos, target_pos)
        dz = abs(pos.get("z", 0) - target_pos.get("z", 0))

        if d > MELEE_RANGE or dz > 3.0:
            if d <= 0.2:
                # Standing on the target but the height is off; nudge forward.
                step = 2.0
                x = pos.get("x", 0) + math.cos(pos.get("orientation", 0)) * step
                y = pos.get("y", 0) + math.sin(pos.get("orientation", 0)) * step
            else:
                step = min(d - 1.2, APPROACH_STEP)
                x = pos.get("x", 0) + (target_pos.get("x", 0) - pos.get("x", 0)) / d * step
                y = pos.get("y", 0) + (target_pos.get("y", 0) - pos.get("y", 0)) / d * step
            client.move_to(x, y, target_pos.get("z", pos.get("z", 0)))
            # Walking is not fighting: a long approach must not look like a
            # stalled attack.
            stall_since = time.time()
        else:
            desired = bearing(pos, target_pos)
            # Ten seconds without a single point of damage is not a slow fight;
            # the target has leashed, evaded, or its position is a ghost. Chasing
            # it only walks us into reach of whatever else is nearby. Drop it,
            # blacklist it briefly, and step out of melee.
            if target_hp is not None and time.time() - stall_since > 10:
                log(
                    f"combat stall guid={target_guid} hp={target_hp} "
                    f"for {time.time() - stall_since:.0f}s self="
                    f"{state.get('selfHealth')}/{state.get('selfMaxHealth')}; dropping"
                )
                combat_stalls += 1
                client.stop_attack()
                blocked[int(target_guid)] = time.time()
                dx = pos.get("x", 0) - target_pos.get("x", 0)
                dy = pos.get("y", 0) - target_pos.get("y", 0)
                length = math.hypot(dx, dy) or 1.0
                client.move_to(
                    pos.get("x", 0) + dx / length * 20,
                    pos.get("y", 0) + dy / length * 20,
                    pos.get("z", 0),
                )
                target_guid = None
                target = None
                last_target_hp = None
                stall_since = time.time()
                continue

            # Re-issue the swing periodically. The server rejects swings from
            # outside a 120-degree frontal arc, so face the target first; the
            # module's optimistic "attacking" flag hides refused swings.
            if time.time() - last_attack_at > 2.0:
                if abs(angle_diff(desired, pos.get("orientation", 0))) > 0.05:
                    client.turn_to(desired)
                client.attack(target_guid)
                last_attack_at = time.time()

            # Heroic Strike costs 15 rage (stored x10). Use it when available;
            # if the spell is unknown, disable it after the first failure.
            if (
                spell_known
                and state.get("selfRage", 0) >= RAGE_COST
                and now - last_spell > 5
                and state.get("attacking")
            ):
                failures = state.get("castFailures", 0)
                try:
                    after = client.cast(HEROIC_STRIKE, target_guid)
                except RuntimeError as err:
                    log(f"heroic strike error: {err}")
                else:
                    last_spell = now
                    if after.get("castFailures", failures) > failures:
                        log(f"heroic strike unavailable: {after.get('lastError')}")
                        spell_known = False

        if now - last_log > 15:
            health = state.get("selfHealth")
            if health != last_health or now - last_log > 30:
                log(
                    f"status lvl={level} xp={state.get('selfXp')}/{state.get('selfNextLevelXp')} "
                    f"hp={health}/{state.get('selfMaxHealth')} rage={state.get('selfRage')} "
                    f"kills={state.get('kills')} target_hp={target_hp} "
                    f"dist={d:.1f} attacking={state.get('attacking')} "
                    f"xp_h={xp_per_hour:.0f} kills_h={kills_per_hour:.1f} "
                    f"deaths={state.get('deaths', 0)} stalls={combat_stalls} "
                    f"no_kill={now - last_kill_at:.0f}s"
                )
                last_health = health
                last_log = now
                if now - last_kill_at > 120 and state.get("kills", 0) == kills_seen:
                    log("warning: no kill in 120s")

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
    except Exception as err:  # noqa: BLE001 - top-level run log
        log(f"fatal: {err}")
        raise
