# sense

Read-only perception. Parses object updates into a snapshot of the character,
visible units/players, game objects, the current selection, and the class
resources needed by a leveling worker.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `sense.get_self` | — | `SelfResponse` | Self unit, map, epoch/readiness, skills, auras, cooldowns, combo points |
| `sense.get_skills` | — | `SkillsResponse` | Skill lines without the rest of the snapshot |
| `sense.get_nearby` | `radius` (0 = all), `limit` (0 = 32, max 200), `include_game_objects` | `NearbyResponse` | Units/players and optionally game objects, each sorted by 2D distance |
| `sense.get_target` | — | `TargetResponse` | Selection tracked locally; returns a `Unit` or a `GameObject` |
| `sense.select_target` | `guid` | — | Sends `CMSG_SET_SELECTION`; never echoed by the server |
| `sense.clear_target` | — | — | Sends GUID 0 |
| `sense.query_name` | `guid` | — | Sends `CMSG_NAME_QUERY`; answer is asynchronous |
| `sense.get_played_time` | — | `PlayedTimeResponse` | Server `/played` counters plus this session's length; never blocks |
| `sense.status` | — | counters | Packet/parse counters, epoch, readiness, map, aura/cooldown counters |
| `sense.debug_monster_moves` | — | payload hex | Diagnostic only |
| `sense.debug_fields` | — | raw field slots | Diagnostic only |

Eight production RPCs plus two diagnostic aliases. There is no high-level
gameplay action in this module.

## Session, epoch and readiness

- `map_id` and the initial position come from `SMSG_LOGIN_VERIFY_WORLD`;
  `has_map` distinguishes map 0 (Eastern Kingdoms) from "no map known yet".
- `epoch` increments on every `SMSG_LOGIN_VERIFY_WORLD` and every
  `SMSG_NEW_WORLD`. A map transition therefore invalidates cursors, tracked
  GUIDs, the selected target and any position assumption from the old epoch.
  `map_changes` counts transitions (not the initial login).
- `ready` is the cold-start gate for the current epoch. It becomes true only
  after a real update packet has supplied self health, max health, level,
  class/power type and both current and maximum primary power. It is never set
  by a timer or by the caller.
- `SMSG_NEW_WORLD` clears units, target, skills and cooldown state and reseeds
  the self position; no state is carried across maps.

## Units, power and conventions

- Positions are WoW world coordinates in yards. Orientation uses the server
  convention: 0 faces +X and the angle grows toward +Y, so forward is
  `(cos o, sin o)` and facing a target is `atan2(dy, dx)`.
- `type` is the update-block `TypeID`: 3 = unit, 4 = player, 5 = game object.
- `entry` is `OBJECT_FIELD_ENTRY`. Creature names are queried automatically
  from `SMSG_CREATURE_QUERY_RESPONSE`; player names from
  `SMSG_NAME_QUERY_RESPONSE`.
- `power_type` is `UNIT_FIELD_BYTES_0` byte 3 (0 mana, 1 rage, 2 focus,
  3 energy, 4 happiness, 5 rune, 6 runic power). `power` and `max_power` read
  `UNIT_FIELD_POWER1 + power_type` and `UNIT_FIELD_MAXPOWER1 + power_type`, so
  warrior rage is `POWER2` and rogue energy is `POWER4`. A zero current value
  is reported as zero, not hidden, so low-resource decisions can see it.
- `display_id`, `dynamic_flags` (lootable/tagged/skinnable) and `npc_flags`
  (gossip, questgiver, vendor, trainer, spirit healer) come from their update
  fields.
- `auras` are the visible aura slots learned from `SMSG_AURA_UPDATE*`, with
  spell id, flags, stacks, caster, remaining/max duration and positive/negative
  classification. Aura packet for self before a normal update does not erase
  the world-entry position.
- `cooldowns` list server-reported spell cooldowns that have not expired.
  `cooldown_ms` is the server duration; `remaining_ms` is computed from the
  module's receipt time. A zero-duration update removes the cooldown.
- `combo_points` / `combo_target_guid` come from
  `SMSG_UPDATE_COMBO_POINTS`.
- `dead` is true when max health is known and current health is zero, or when
  self has the ghost `PLAYER_FLAGS` bit.
- `in_combat` is `UNIT_FIELD_FLAGS & 0x80000`.

## Played time

`sense.get_played_time` exposes the same counters the client's `/played`
command prints. `total_seconds` is cumulative across every session and
`level_seconds` is time spent at the current level; both come straight from
`SMSG_PLAYED_TIME`, so they are the server's numbers, not the module's guess.

The module sends `CMSG_PLAYED_TIME` once after `SMSG_LOGIN_VERIFY_WORLD` and
again whenever a read sees a sample older than 30 seconds. Reads never wait for
the reply: `has_data` stays false (and `sampled_unix_ms` zero) until the first
answer arrives. `session_seconds` is measured locally from the login that
started this client process and keeps advancing between samples; a map
transition does not reset it, a new login does.

## Game objects

`get_nearby` keeps unit/player and game-object lists separate. Game objects are
included only when `include_game_objects` is set, and both lists are capped
independently by `limit`; `known_units`/`known_game_objects` report the
pre-cap counts. A game object carries entry, type id, display id, state,
animation progress, flags, level and faction from `EGameObjectFields`.
`get_target` returns a `GameObject` when the selection is a game object.
Selecting a game object sends `CMSG_SET_SELECTION` like any other GUID.

## Subscribed opcodes

`SMSG_LOGIN_VERIFY_WORLD`, `SMSG_NEW_WORLD`, `SMSG_UPDATE_OBJECT`,
`SMSG_COMPRESSED_UPDATE_OBJECT`, `SMSG_DESTROY_OBJECT`,
`SMSG_NAME_QUERY_RESPONSE`, `SMSG_CREATURE_QUERY_RESPONSE`,
`SMSG_MONSTER_MOVE`, `SMSG_AURA_UPDATE`, `SMSG_AURA_UPDATE_ALL`,
`SMSG_SPELL_COOLDOWN`, `SMSG_COOLDOWN_EVENT`, `SMSG_UPDATE_COMBO_POINTS`,
`SMSG_PLAYED_TIME`.

Malformed packets are counted and dropped; `OnPacket` never returns an error,
because that would take down the session owner loop. Name/creature queries are
queued to a background worker because `SendPacket` waits on that loop.

## Preconditions and limits

- The character must be in world. Before `ready` is true, callers must not act
  on self resources, skills or the map.
- Self position is a snapshot; use `move.get_position` for the live position
  while walking. `sense` does not subscribe to self movement echoes.
- Update parsing covers movement blocks and field masks; values-only blocks
  infer player-vs-unit from GUID high bits.
- Destroying the selected GUID clears the local target instead of retaining a
  stale GUID.

## Verified

Unit tests only (`go test ./...` in this module): class-specific power slots
(warrior rage, priest mana, rogue energy), readiness before/after login and
incremental self fields, map-transition invalidation of target/units/position,
game objects in nearby/target, aura add/remove, cooldown remaining time, combo
points, played-time sample replacement and destroyed-target clearing. No
live-server verification is claimed
for this revision.
