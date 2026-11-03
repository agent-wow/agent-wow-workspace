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
| `move.stop` | — | `PositionResponse` | Sends `MSG_MOVE_STOP` |
| `move.set_run_speed` | `yards_per_second` (1–20) | `StatusResponse` | Movement pacing only; the server has its own speed |
| `move.status` | — | `StatusResponse` | Position, moving, speed, epoch, map, counters |

## Behaviour

- `forward`/`backward` send `MSG_MOVE_START_*`, then a `MSG_MOVE_HEARTBEAT`
  every 500 ms with an interpolated position, then `MSG_MOVE_STOP` with the
  final position. The RPC blocks for `yards / run_speed` seconds (capped at 8 s).
- `move_to` turns toward the destination first, interpolates Z, and caps one
  step at 40 yards. Distance 0 is a no-op. It walks in a straight line: no
  pathfinding and no obstacle avoidance.
- Calls are serialized. A second movement RPC while one is in flight returns
  `FailedPrecondition`.
- Orientation uses the server convention: 0 faces +X and the angle grows
  toward +Y. The forward vector is `(cos o, sin o)`, and facing a target is
  `atan2(dy, dx)`. The module does not flip to a north/clockwise convention.
- Z is interpolated toward the requested destination but terrain elevation is
  not sampled. Short steps on slopes may drift in Z.
- Position seeds from `SMSG_LOGIN_VERIFY_WORLD` and is replaced on every
  `SMSG_NEW_WORLD`. A self movement echo received in world updates the estimate
  only while not moving.

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
  `FailedPrecondition` ("position unknown").
- The server accepts client-side movement; this module does not pathfind and
  does not avoid obstacles.

## Verified

Unit tests only (`go test ./...` in this module). No live-server verification is
claimed for movement, map transitions or orientation in this revision.
