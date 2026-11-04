# Quest reward full-bag reconciliation (2026-11-04)

## Live finding

腰果牧师 could not finish Cutting Teeth (788) even though the server confirmed
ALREADY_ON (invalid reason 13). Empty session-local objectives and turn-in
timeouts were not completion evidence.

The decisive live symptom was an `OFFER_REWARD -> CHOOSE_REWARD` loop: 258 and
later 932 reward choices were sent during repeated 15-second turn-in windows.
The priest's 16-slot backpack was full (`freeSlots` omitted because it was 0).
AzerothCore could not deliver the reward and re-offered the reward page; the
quest module answered every copy, creating the packet loop.

## Fixes

- `modules/quest/src/service/service.go`: respond to request-items and
  offer-reward at most once per `TurnIn` call. A repeated offer no longer
  creates a packet loop. Invalid accepts also reconcile optimistic active rows.
- `scripts/growth/inventory_runtime.py`: quest reward preparation reserves one
  free bag slot, suppresses purchases/equipment in that mode, approaches vendors
  in 20-yard steps, and remains blocking if movement fails. When no vendor is
  visible it walks toward the ordered route's turn-in NPC anchor until a vendor
  is sensed. Gornek to Duokna is about 44.6 yards, beyond the old 40-yard search
  and single-move limits.
- `scripts/growth/machine.py`: perform reward bag preparation before the
  `quest_turn_in` RPC, not after it.
- `scripts/growth/quest_runtime.py`: empty objectives plus either reason 13 or
  an earlier inconclusive turn-in timeout enters the turn-in reconciliation
  probe after a worker restart; neither case is treated as 0/8 or completion.

## Verification

- 235 Python growth tests pass.
- All `modules/quest` Go tests pass, including repeated-offer loop protection.
- `git diff --check` passes.
- Priest sold one item at Duokna (`freeSlots=1`), returned toward Gornek, and
  received `SMSG_QUESTGIVER_QUEST_COMPLETE`: active 788 removed, `completed=1`,
  XP 295 -> 515 (+220), local `quest:durotar:index=2`.
- Final online sample: 腰果战士 level 3 / 954 XP, 腰果萨满 level 4 / 238 XP,
  腰果猎人 level 4 / 572 XP, 腰果牧师 level 4 / 515 XP, 腰果术士 level 2 / 562
  XP. Every route index was 2 and every selected character was online.

Other characters' live `completed` counters may be zero after session restarts;
those counters are session-local. Their earlier reward/XP evidence and route
index 2 remain the acceptance record. The fixed Go quest image was deployed to
doumoboy4 only; other sessions were left online without a disruptive rebuild.

No GM commands, server quest edits, credential changes, commits, or pushes were
used. All code changes remain uncommitted in the host workspace.
