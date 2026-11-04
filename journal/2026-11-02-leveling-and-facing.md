# 2026-11-02 — Leveling Reachbot and the facing bug

## Goal

Drive the test character `Reachbot` from level 1 to level 10 on the private
3.3.5a realm, using `scripts/leveling-agent.py` plus the sense/move/combat
modules.

## The facing convention (important)

Melee swings were being refused as `target is behind the character` even when
the character was standing on the mob. The bug was an orientation convention
mismatch, not a range problem.

AzerothCore compares facing with `HasInArc(2π/3, victim)` where the angle is
`atan2(dy, dx)` (`WorldObject::GetAngle`). `SetFacingToObject` stores the same
`atan2(dy, dx)` into the orientation field. So:

- orientation `0` points along **+X**, `π/2` along **+Y**
- forward vector is `(cos o, sin o)`
- facing a target is `atan2(ty - y, tx - x)`

The workspace used `atan2(dx, dy)` everywhere (`scripts/leveling-agent.py`,
`modules/move` `MoveTo`, `lib/wow.ForwardPosition`), which is up to 90° off and
outside the 120° frontal arc. Verified live against the server: with
`atan2(dy, dx)` four seconds of swinging produced 0 refusals and damage; with
`atan2(dx, dy)` the target took 0–8 damage and refusals were sent.

Side effect: movement still worked because the move module injects explicit
positions in each `MSG_MOVE_*` packet, so the wrong facing only affected
attacks (and the walk-start facing the client displayed).

Server-side swing rules (AzerothCore `PlayerUpdates.cpp` ~line 160):

- not within melee range → `setAttackTimer(100)` + one `SMSG_ATTACKSWING_NOTINRANGE`
- outside the 120° frontal arc and outside the victim's boundary radius →
  `SMSG_ATTACKSWING_BADFACING`, again only once
- `IsWithinMeleeRange` uses **3D** distance (`GetCombatReach + reach + 4/3`,
  at least `NOMINAL_MELEE_RANGE`), hence the `dz` check in the agent

The once-only error messages (`m_swingErrorMsg` guard) mean silence does not
prove the swing is landing; measure target health instead.

## Leveling observations

- XP per kill at level 5: level-1 wolf 14, level-2 wolf 28, level-3 Defias Thug
  ~30. Northshire wolves stop being worth it around level 5.
- Northshire → Defias camp is ~245 yards of walkable ground; the camp at
  `(-9028, -271, 74)` holds level-3 Defias Thugs.
- The worldserver drops the session (`worldserver disconnected: EOF`) every
  15–30 minutes. `scripts/leveling-supervisor.sh` restarts the session and the
  agent when either dies.

## Agent loop lessons

- A "combat stall" (target HP unchanged for ~10 s) is almost always a leashed
  or evaded mob, or a second mob attacking while the first is unreachable. Drop
  and blacklist the target instead of backing off and re-attacking.
- Reset the stall timer while walking; a long approach is not a stall.
- Do not `stop_attack` on every approach step; re-issuing `Attack` is
  idempotent server-side and stopping resets the swing timer.
- Loot immediately after the kill, while the corpse is underfoot. Opening a
  creature the server still sees as alive returns `didnt_kill`; that is benign
  (the row simply stays on the corpse), so the agent only logs the first new
  error instead of retrying.

## Copying a module

Copying a module directory is not enough: `go.mod`, `cmd/main.go`,
`src/server/server.go`, the proto package, **and both `Dockerfile` and
`compose.yaml`** carry the old name. `compose.yaml` pointing at
`modules/party/Dockerfile` builds the party binary into the new image, and
agent-wow reports the confusing `unknown service <new>.module.v1.Module` at
RPC time. Grep the whole new directory for the old name before building.
