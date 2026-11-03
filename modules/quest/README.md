# quest

Quest-giver interaction and session-local quest progress. The module executes
requests from an agent; it does not choose routes, quests or reward quality.

`quest.turn_in` is asynchronous. It sends the complete-quest request, then the
worker answers the server's item check and reward offer with the requested
reward index. `OnPacket` never calls `SendPacket` synchronously because it runs
on the session owner loop.

## RPCs

| RPC | Input | Output | Notes |
|---|---|---|---|
| `quest.gossip` | `guid` | `QuestState` | Opens an NPC quest menu; rows arrive asynchronously |
| `quest.accept` | `guid`, `quest_id` | `QuestState` | Sends accept and adds a session-local active row |
| `quest.turn_in` | `guid`, `quest_id`, `reward_index` | `QuestState` | Starts complete/request-reward/choose-reward sequence |
| `quest.get_state` | — | `QuestState` | Latest NPC menu, active quests, progress and errors |
| `quest.status` | — | `StatusResponse` | Packet, parser and operation counters |

`accepts` counts requests sent, not server-side acceptance. `completed` only
increments on `SMSG_QUESTGIVER_QUEST_COMPLETE`. Menu rows retain the server's
`icon` dialog status so the agent can decide which quests to accept or turn in.

## Wire formats (3.3.5a)

- Quest-giver requests use an unpacked little-endian GUID followed by quest ID.
  Accept has one trailing zero `uint32`; choose-reward has the reward index.
- `SMSG_QUESTGIVER_QUEST_LIST` contains one greeting string, emote delay/type,
  a `uint8` count, then rows of quest ID, icon, level, flags, repeatable byte
  and title.
- `SMSG_QUESTUPDATE_ADD_KILL` contains quest ID, creature entry, current count,
  required count and one unused `uint32`.
- Completion and failure notices carry a quest ID where applicable.

See `lib/wow/quest.go` for the shared packet codecs and their tests.

## Preconditions and limits

- The character must be alive, in world and within NPC interaction range.
- Only one turn-in sequence may be open at a time. It expires after 15 seconds
  when state/status is polled, and failures release it for retry. Gossip and
  accept are rejected while a turn-in is active.
- Reward responses must match the pending NPC GUID and quest ID; delayed or
  unrelated responses are ignored.
- Reward indexes are zero-based; use zero for a fixed-reward quest.
- Active quests are session-local. Persistent quest-log reconstruction after a
  module restart is not implemented yet, so the agent must re-read NPC menus.
- Kill progress is decoded. Item objectives, game objects, escorts and quest
  POIs are not implemented yet.
- A quest marked `complete` by `SMSG_QUESTUPDATE_COMPLETE` has not necessarily
  been turned in; turn-in removes it from `active`.

## Verified

- Unit tests cover quest-list parsing, accept, kill progress, completion, the
  asynchronous reward sequence, concurrent turn-in rejection and failures.
- Live smoke on `核桃战` (2026-11-02): the module loaded and `quest.status` /
  `quest.gossip` succeeded. The nearby NPC returned no standard quest list, so
  accept, progress and turn-in have **not** been verified in game yet.

## Example

```bash
scripts/session.sh rpc quest.gossip '{"guid":"17379368034959360"}'
scripts/session.sh rpc quest.get_state
scripts/session.sh rpc quest.accept '{"guid":"17379368034959360","quest_id":783}'
scripts/session.sh rpc quest.turn_in '{"guid":"17379368034959360","quest_id":783,"reward_index":0}'
```
