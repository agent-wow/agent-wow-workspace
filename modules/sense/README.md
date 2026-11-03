# sense

Read-only perception. Parses object updates into a snapshot of the character
and every visible player/unit.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `sense.get_self` | — | `SelfResponse` | GUID/name come from the container env; map and position from `SMSG_LOGIN_VERIFY_WORLD`; fields from the self create block |
| `sense.get_nearby` | `radius` (0 = all), `limit` (0 = 32, max 200) | `NearbyResponse` | Players and units, sorted by 2D distance from self; includes GUID, entry, level, health |
| `sense.get_target` | — | `TargetResponse` | The character's own selection, tracked locally |
| `sense.select_target` | `guid` | — | Sends `CMSG_SET_SELECTION`; the server never echoes the client's own selection |
| `sense.clear_target` | — | — | Sends GUID 0 |
| `sense.query_name` | `guid` | — | Sends `CMSG_NAME_QUERY`; answer is asynchronous and lands in `get_nearby` |
| `sense.status` | — | counters | `packets`, `parse_errors`, `known_units`, `name_queries` |

## Units and conventions

- Positions are WoW world coordinates in yards; orientation is radians, 0 =
  north (+Y), growing clockwise.
- `type` is the `TypeID`: 3 = unit, 4 = player. Game objects are not exposed yet.
- `entry` is `OBJECT_FIELD_ENTRY`; creatures can be identified by it even before
  a creature-name query exists.

## Subscribed opcodes

`SMSG_LOGIN_VERIFY_WORLD`, `SMSG_UPDATE_OBJECT`,
`SMSG_COMPRESSED_UPDATE_OBJECT`, `SMSG_DESTROY_OBJECT`,
`SMSG_NAME_QUERY_RESPONSE`.

Malformed packets are counted and dropped; `OnPacket` never returns an error,
because that would take down the session owner loop.

## Preconditions and limits

- The character must be in world before `get_nearby` has useful data.
- Player names are fetched automatically on create. Creature names are not yet
  queried (`SMSG_CREATURE_QUERY_RESPONSE` is a separate format).
- Self position is a snapshot. Use `move.get_position` for the live position
  while walking; `sense` does not see self movement echoes.
- Update-field parsing covers movement blocks and field masks; values-only
  blocks infer player-vs-unit from GUID high bits.

## Verified

Live on the private 3.3.5a realm (2026-11-02):

- `get_self` returned GUID `2727151`, map 1, position `(-8949.95, -132.49, 83.53)`,
  health 60/60, level 1.
- `get_nearby` returned 129–134 known units with positions, health and entries.
- `select_target` + `get_target` round-tripped a creature GUID; `clear_target`
  cleared it.
- 58 packets processed with zero parse errors.
