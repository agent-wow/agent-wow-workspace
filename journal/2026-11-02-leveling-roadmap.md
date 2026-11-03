# 2026-11-02 — Solo/party leveling roadmap and quest foundation

## Priority order

Optimize experience per hour, not module count. Reuse the working grind loop as
the fallback at every stage.

1. **P0 metrics**: record XP/hour, kills/hour, deaths, combat stalls and time
   since the last kill so later routes can be compared against grinding.
2. **P1 quest**: implement the accept/progress/turn-in/reward protocol loop.
   Start with one verified Northshire chain; no global quest database.
3. **P2 routes**: keep waypoint routes and stuck recovery in the agent. Extend
   `move` only for missing movement facts; do not start with a navmesh.
4. **P3 party**: extend `party` with group list/member state and invite/leave.
   A multi-account agent owns leader/follower, assist and regroup strategy.
5. **P4 roles/sustain**: generalize power and spell state in `combat`/`sense`,
   then add `inventory` for consumables, quest items and equipment.
6. **P5 long runs**: add `vendor` and `trainer` only when unattended leveling
   beyond the starter zones requires selling, repairs and skill training.

Follow, assist, rotations, rest thresholds, route selection and reward scoring
belong to agents, not modules. Each account keeps its own session and worldrpc.

## P0/P1 implementation

- Added shared WotLK quest packet codecs and tests in `lib/wow/quest.go`.
- Added `modules/quest` with `gossip`, `accept`, `turn_in`, `get_state` and
  `status`. Turn-in runs asynchronously through a worker; packet callbacks do
  not synchronously send packets.
- Menu rows retain the server's dialog icon. The agent, not the module, decides
  which quests to accept and which reward to choose.
- Added per-process XP/hour, kills/hour, death, stall and no-kill metrics to
  `scripts/leveling-agent.py`.
- Module lint, shared tests, quest tests, race-detector tests and an isolated
  Colima Docker build passed.

## Review corrections

Independent review caught a real packet-layout mismatch that the initial tests
had mirrored in their fixtures. The WotLK quest list is one greeting string,
emote delay/type and a `uint8` count, not two strings and a `uint32` count.
Corrected both codec and fixtures. Also added:

- A 15-second turn-in timeout and failure cleanup so silent server refusals or
  reconnects cannot permanently block later turn-ins.
- GUID/quest-ID matching for request-items and offer-reward responses.
- Pre-send menu clearing to avoid losing a fast gossip response.
- Separate request-reward and choose-reward counters; failed initial sends do
  not increment turn-ins.
- Removal of the incorrect item-update quest-ID assumption.

Regression tests cover mismatched reward responses, timeout/retry and send
failure. Lint, unit tests, race detector and Docker build pass after the fixes.

## Verification boundary and next step

A live smoke session on `核桃战` loaded the module and successfully called
`quest.status` and `quest.gossip`. The nearby NPC did not return a standard
quest list, so this was not a full in-game quest-chain verification. The test
session was stopped afterward; the existing Reachbot and Reachgirl sessions
were not interrupted.

Next, when a Northshire session is available, verify a real NPC quest list,
then one complete accept/objective/turn-in chain before enabling a quest route
in the unattended agent. Active quest state is currently session-local; a
restart must re-read NPC menus until persistent quest-log reconstruction is
implemented. Item objectives, game objects, escorts and POIs remain out of the
first increment.
