# move

Locomotion. Emits WotLK movement packets and keeps the character's position.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `move.get_position` | — | `PositionResponse` | Live local estimate |
| `move.forward` | `yards` (0 < n <= 40) | `PositionResponse` | Walks forward, blocks until finished |
| `move.backward` | `yards` | `PositionResponse` | Walks backward |
| `move.turn_left` | `radians` | `PositionResponse` | Positive radians; sends `MSG_MOVE_SET_FACING` |
| `move.turn_right` | `radians` | `PositionResponse` | Sends the negative rotation |
| `move.turn_to` | `orientation` | `PositionResponse` | Absolute facing |
| `move.move_to` | `x`, `y`, `z` | `PositionResponse` | Walks straight toward a world position; no pathfinding |
| `move.navigate` | `x`, `y`, `z` | `PositionResponse` | Bounded terrain-aware A* detour; walks at most 12 yards per call |
| `move.stop` | — | `PositionResponse` | Sends `MSG_MOVE_STOP` |
| `move.recover_ground` | — | `PositionResponse` | Manual recovery from a confirmed bad saved Z; bypasses the 8-yard guard |
| `move.set_run_speed` | `yards_per_second` (1–20) | `StatusResponse` | Movement pacing only; the server has its own speed |
| `move.status` | — | `StatusResponse` | Position, moving, speed, epoch, map, counters |

## Behaviour

- `forward`/`backward` send `MSG_MOVE_START_*`, then a `MSG_MOVE_HEARTBEAT`
  every 500 ms with an interpolated position, then `MSG_MOVE_STOP` with the
  final position. The RPC blocks for `yards / run_speed` seconds (capped at 8 s).
- `move_to` turns toward the destination first, samples the destination terrain,
  and caps one step at 40 yards. Distance 0 is a no-op. It walks in a straight
  line: no pathfinding and no obstacle avoidance. The supplied Z remains an
  estimate; emitted positions use the sampled terrain height.
- `navigate` runs bounded A* over four-yard outdoor-terrain samples, rejects
  unsafe height transitions and walks at most 12 yards along the result. If the
  goal exceeds the search budget it returns a safe partial detour. It still does
  not see VMap walls or indoor floors; route strategy remains with the caller.
- Calls are serialized. A second movement RPC while one is in flight returns
  `FailedPrecondition`.
- Orientation uses the server convention: 0 faces +X and the angle grows
  toward +Y. The forward vector is `(cos o, sin o)`, and facing a target is
  `atan2(dy, dx)`. The module does not flip to a north/clockwise convention.
- Every start, heartbeat and final position samples AzerothCore map-v9 outdoor
  terrain for the current map/X/Y. Z is not held constant or merely
  interpolated. Missing tiles, holes and corrections over 8 yards stop the move
  instead of emitting an invented height. The 8-yard guard rejects likely
  cliffs, bridges and indoor areas; VMap objects are not sampled yet.
- `move.recover_ground` is an explicit operator action for a character already
  saved below or above the terrain by the old locomotion code. It sends one
  `MSG_MOVE_STOP` at the sampled height without applying the 8-yard guard. Use
  it only after confirming the character is on open outdoor terrain; it can
  otherwise move a legitimate bridge or indoor position onto the ground.
- Position seeds from `SMSG_LOGIN_VERIFY_WORLD` and is replaced on every
  `SMSG_NEW_WORLD`. Authoritative self positions in object updates and self
  movement echoes replace the estimate only while not driving a move. This also
  resynchronizes a move container restarted without restarting the world session.

### Local terrain data

Run `scripts/fetch-client-data.sh --terrain` once. It downloads the pinned
wowgaming/client-data v20.0 archive and extracts only `maps/` into
`data/client-data/terrain-v20/maps`. The compose file mounts that directory
read-only at `/data/maps`; set `AGENT_WOW_MAPS_DIR` on the Compose host to use a
different directory. `scripts/sync-to-vm.sh` carries an extracted map directory
into the Colima VM and preserves it across later syncs.

For a targeted setup, `scripts/fetch-terrain-tiles.py --dest <new-empty-dir>
0013240.map ...` fetches individual tiles over HTTP Range and verifies their
ZIP CRCs. This is useful when the full archive downloads slowly. Partial map
coverage is safe but limited: entering an uncovered tile stops locomotion.

### Map transitions

`SMSG_LOGIN_VERIFY_WORLD` and `SMSG_NEW_WORLD` are authoritative world entries.
Each one increments `world_epoch`, sets `map_id`/`has_map`, replaces the
position and clears `moving`. An in-flight walk captures the epoch and a reset
channel when it starts; a world entry closes that channel and the walk returns
`codes.Aborted` without sending a heartbeat or stop with old-map coordinates
and without storing the stale position. `move.status` exposes the epoch so a
caller can discard cursors and targets captured before the change.

## Subscribed opcodes

`SMSG_LOGIN_VERIFY_WORLD`, `SMSG_NEW_WORLD`, `MSG_MOVE_START_FORWARD`,
`MSG_MOVE_START_BACKWARD`, `MSG_MOVE_STOP`, `MSG_MOVE_HEARTBEAT`,
`MSG_MOVE_SET_FACING`, `MSG_MOVE_TELEPORT_ACK`.

## Preconditions

- Character must be in world; before that every movement RPC returns
  `FailedPrecondition` ("position and map unknown").
- Outdoor map tiles must be mounted. Without a reliable terrain height,
  locomotion returns `Unavailable` before sending an unsafe position.
- The server accepts client-side movement; this module does not pathfind and
  does not avoid obstacles.

## Verified

Unit tests cover movement packets, terrain failures, map transitions and explicit
ground recovery. On 2026-11-04, live RPC checks also verified forward/backward
terrain heights on all five authorized AI characters and recovered one priest
from a saved underground Z. No human-client visual verification or VMap/object
height validation is claimed.
