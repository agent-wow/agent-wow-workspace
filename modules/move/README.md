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
| `move.stop` | — | `PositionResponse` | Sends `MSG_MOVE_STOP` |
| `move.set_run_speed` | `yards_per_second` (1–20) | `StatusResponse` | Movement pacing only; the server has its own speed |
| `move.status` | — | `StatusResponse` | Position, moving, speed, counters |

## Behaviour

- `forward`/`backward` send `MSG_MOVE_START_*`, then a `MSG_MOVE_HEARTBEAT`
  every 500 ms with an interpolated position, then `MSG_MOVE_STOP` with the
  final position. The RPC blocks for `yards / run_speed` seconds (capped at 8 s).
- Calls are serialized. A second movement RPC while one is in flight returns
  `FailedPrecondition`.
- Orientation semantics: 0 faces north (+Y), increasing clockwise. Forward is
  `(sin o, cos o)`.
- Z is held constant; terrain elevation is not sampled. Short steps on slopes
  may drift in Z.
- Position seeds from `SMSG_LOGIN_VERIFY_WORLD`. A self movement echo received
  in world updates the estimate only while not moving.

## Subscribed opcodes

`SMSG_LOGIN_VERIFY_WORLD`, `MSG_MOVE_START_FORWARD`,
`MSG_MOVE_START_BACKWARD`, `MSG_MOVE_STOP`, `MSG_MOVE_HEARTBEAT`,
`MSG_MOVE_SET_FACING`.

## Preconditions

- Character must be in world; before that every movement RPC returns
  `FailedPrecondition` ("position unknown").
- The server accepts client-side movement; this module does not pathfind and
  does not avoid obstacles.

## Verified

Live on the private 3.3.5a realm (2026-11-02):

- From login position `(-8949.95, -132.49, 83.53, o=0)`, `forward 5` moved to
  `(-8949.95, -127.49)`.
- `turn_left 1.5708` then `forward 3` moved to `(-8946.95, -127.49)`.
- Both moves and the final orientation persisted across logout/login, proving
  the server accepted the packets (re-login `SMSG_LOGIN_VERIFY_WORLD` returned
  the new position and `o=1.5708`).
