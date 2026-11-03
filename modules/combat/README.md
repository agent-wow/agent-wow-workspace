# combat

Melee attack control, spell casts, and the server facts a worker needs to
fight, recover from death, and avoid stale targets. It exposes packet-derived
facts and actions only; it contains no combat strategy.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `combat.attack` | `guid` | `CombatState` | Selects the GUID, sends `CMSG_ATTACKSWING` |
| `combat.stop_attack` | — | `CombatState` | Sends `CMSG_ATTACKSTOP` |
| `combat.cast` | `spell_id`, optional `target_guid` | `CombatState` | Sends `CMSG_CAST_SPELL`; zero target means the tracked target |
| `combat.release_spirit` | — | `CombatState` | Sends `CMSG_REPOP_REQUEST` |
| `combat.reclaim_corpse` | — | `CombatState` | Sends `CMSG_RECLAIM_CORPSE` with the character GUID |
| `combat.resurrect_at_spirit_healer` | `healer_guid`, `packed_guid` | `CombatState` | Opens gossip, waits 400 ms, sends `CMSG_SPIRIT_HEALER_ACTIVATE` |
| `combat.get_state` | — | `CombatState` | Current combat/death/progression snapshot |
| `combat.status` | — | `StatusResponse` | Packet counters and parse errors |

## State facts

- **Primary power**: `self_power_type` comes from `UNIT_FIELD_BYTES_0` byte 3.
  The live value is read from `UNIT_FIELD_POWER1 + power_type`, so a warrior's
  rage is `POWER2`, a priest's mana is `POWER1`, and a rogue's energy is
  `POWER4`. `self_power` / `self_max_power` report that slot. The legacy
  `self_rage` field remains as an alias for the rage slot for older callers.
- **Target facts**: `target_health`, `target_max_health`, `target_level` and
  `target_entry` come from the tracked GUID's update fields. A zero target
  health marks the target dead and stops the local attacking flag.
- **Death and ghost**: `self_dead` is true when self health is zero or
  `PLAYER_FLAGS` has the ghost bit. `self_ghost` distinguishes a released
  spirit from a dead-but-not-released character. `deaths` increments on the
  dead transition, not on every packet.
- **Corpse reclaim**: `SMSG_CORPSE_RECLAIM_DELAY` sets
  `corpse_reclaim_delay_ms`; `corpse_reclaim_remaining_ms` is computed from the
  module's receipt time. Zeroing the reported delay clears the timer.
- **Spirit-healer location**: `SMSG_DEATH_RELEASE_LOC` exposes
  `spirit_healer_map_id`, `spirit_healer_position` and
  `has_spirit_healer_location`. A negative map id clears them.

## World transitions and stale targets

`SMSG_LOGIN_VERIFY_WORLD` and `SMSG_NEW_WORLD` clear the tracked target, target
facts, attacking/in-combat flags, self health/power/death facts, corpse timer
and healer location. GUIDs from the old map are never retained. Progression
counters (`kills`, `deaths`, `last_xp`, `level_ups`) and the last known level
survive because the character keeps them across maps. `status.world_resets`
counts these resets.

## Subscribed opcodes

`SMSG_LOGIN_VERIFY_WORLD`, `SMSG_NEW_WORLD`, `SMSG_ATTACKSTART`,
`SMSG_ATTACKSTOP`, `SMSG_ATTACKSWING_NOTINRANGE`, `SMSG_ATTACKSWING_BADFACING`,
`SMSG_ATTACKSWING_DEADTARGET`, `SMSG_ATTACKSWING_CANT_ATTACK`,
`SMSG_LOG_XPGAIN`, `SMSG_LEVELUP_INFO`, `SMSG_CAST_FAILED`, `SMSG_GOSSIP_MESSAGE`,
`SMSG_SPIRIT_HEALER_CONFIRM`, `SMSG_CORPSE_RECLAIM_DELAY`,
`SMSG_DEATH_RELEASE_LOC`, `SMSG_UPDATE_OBJECT`,
`SMSG_COMPRESSED_UPDATE_OBJECT`, `SMSG_DESTROY_OBJECT`.

## Preconditions

- Attack, cast, and recovery RPCs send packets only; the caller must wait for
  the server's `SMSG_ATTACKSTART` / update fields before treating the action as
  accepted. Refusal opcodes set `last_error` and increment `refusals`.
- `combat.resurrect_at_spirit_healer` requires the GUID of a nearby spirit
  healer the character is eligible to use. The module does not find healers or
  decide when to recover.
- Combat does not track map identity or readiness; `sense.get_self` is the
  session/skill/map snapshot owner.

## Verified

Unit tests only (`go test ./...` in this module): class power slots, ghost and
death transitions, corpse reclaim remaining time, death-release location,
target fields, and world-reset behavior. No live-server verification is
claimed for this revision.
