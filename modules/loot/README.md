# loot

Empties corpses. `loot.loot` opens a creature's loot window with `CMSG_LOOT`;
when `SMSG_LOOT_RESPONSE` arrives the module takes every row it is allowed to
take, collects the money, and releases the window. Taking is queued to a worker
because `OnPacket` runs on the session owner loop and must never call
`SendPacket` synchronously.

By default `auto_loot` is on: any window the server opens (including one a
human opened) is emptied. Turn it off with `loot.set_auto_loot`.

## Wire formats (3.3.5a)

- `CMSG_LOOT`: raw 64-bit GUID of the creature (`HandleLootOpcode`,
  `ObjectGuid` streams as an unpacked `uint64`).
- `SMSG_LOOT_RESPONSE`: raw `uint64` guid, `uint8` loot type, then either
  `uint8` error (type 0) or `uint32` gold, `uint8` count, and rows of
  `{uint8 slot, uint32 itemId, uint32 count, uint32 displayInfoId,
  uint32 randomSuffix, uint32 randomPropertyId, uint8 slotType}`
  (`Player::SendLoot` + `LootView` in `LootMgr.cpp`).
- `CMSG_AUTOSTORE_LOOT_ITEM`: the slot byte as received. Slots are indexes in
  the loot's item array, so they are **not** contiguous when rows are hidden.
- `CMSG_LOOT_MONEY`: empty body.
- `CMSG_LOOT_RELEASE`: raw 64-bit GUID; ignored unless it matches the window
  currently open.

`slotType` is 0 (allow loot), 1 (roll ongoing), 2 (master loot), 3 (locked),
4 (owner) or 5 (none). Solo play reports 4. The module autostores 0, 2 and 4
and skips the rest, so rolls and quest-locked rows are left alone.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `loot.get_state` | — | `LootState` | `opened`, `items`, `money` (copper), `errors`, `auto_loot`, `last_error`, `last_items` |
| `loot.loot` | `guid` | `LootState` | Send `CMSG_LOOT`; the take happens asynchronously |
| `loot.set_auto_loot` | `enabled` | `LootState` | Stop the automatic take |

## Preconditions

- The character must be alive and within `INTERACTION_DISTANCE` (5 yd) of the
  corpse, and must own the loot rights (it killed the creature).
- The GUID is the creature's, not a corpse object's.

## Verified

- Live on Reachbot (2026-11-02): 8 opens produced 4 items and 6 copper; two
  opens were refused with `didnt_kill` when the agent asked to loot a target
  the server had not registered as dead yet. Both counters behave as designed.
- `loot.get_state` reported `lastItems` `[1376 x1, 2070 x1]` after a Defias
  Thug kill, matching the two rows the server offered.

## Example

```bash
scripts/session.sh rpc loot.get_state
scripts/session.sh rpc loot.loot '{"guid":"17379368034959360"}'
```
