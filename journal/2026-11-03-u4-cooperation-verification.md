# U4 local verification — 2026-11-03

## Scope

The party module no longer accepts invitations automatically. A pending invite
only changes local state; `party.respond_invite` must explicitly match the
invite ID, inviter and local TTL. Party chat identity comes from the server
sender GUID, never from message text.

The growth runtime adds roster-only contacts, invitations and AGW1 activity
agreements. Contacts must contain every other roster character before new
cooperation starts; backfill is bounded per peer and has an overall deadline.
Non-roster groups are left, and refused, expired or completed agreements return
the character to solo growth. Chat parsing is strict, rate-limited and reduced
from checkpointed server events. The worker invites only compatible online
roster contacts with a persistent cooldown, and a group leader can propose a
temporary two-character grind. An ambiguous invitation is reconciled in the
background without pausing solo growth or inviting a different peer.

Group loot is set only by the leader and must read back as group loot before a
roll is voted. Roll decisions use known item templates and the U3 equipment
policy. Unknown templates near the deadline result in pass. Each roll vote and
trade mutation uses a durable intent, with read-back instead of redispatch when
a response is lost.

Trade plans are explicit, roster-only and revision-guarded. Accept rechecks the
original bag, slot, item GUID, equipped state and quantity. Changed offers are
cancelled. A partial expected peer offer waits until the plan deadline rather
than cancelling before both sides finish. Lost completion packets are reconciled only when all outgoing GUIDs
have left, expected incoming counts are present and the exact money delta
matches; otherwise the outcome remains unknown or abandoned. An abandoned accept
blocks later attempts under the same plan, and a cancel/disconnect notification
cannot override inventory evidence of a completed transfer.

## Protocol corrections

- `CMSG_MESSAGECHAT` encodes type and language as two `uint32` fields. The
  one-byte type belongs to the server message parser, not the client request.
- Friend result values follow the pinned AzerothCore definitions: online `2`,
  offline `3`, removed `5`, added online `6`, added offline `7`. Only added
  online and later online updates carry presence details.
- `GROUPTYPE_LFG` is `0x08`; the previous `0x01` test fixture represented the
  battleground flag instead. The group-loot method is `3`, not master loot `2`.
- Roll client votes are pass `0`, need `1`, greed `2`; server roll result types
  use need `0`, greed `1`, pass `2`.
- Inventory item-template queries reject item ID zero, deduplicate outstanding
  requests and expose known templates in deterministic snapshots.

## Independent review corrections

A separate read-only review traced the production paths and found twelve gaps.
Corrections added the client chat width, pending-trade cancel, partial-offer
waiting, stale-terminal isolation, server-confirmed roll votes, plan-stable
accept keys, non-optimistic trade initiation, worker invitation/proposal policy,
echo-confirmed agreement responses, cancel read-back and safe roster-edit replay.
The permanent 180-second contact initialization failure remains intentional:
the plan requires a failed initialization gate, so later contact completion does
not silently unlock cooperation without a fresh worker initialization.

## Final local verification

- `python3 -I -m unittest discover -s scripts/growth/tests` — **176 tests passed**.
- Focused U4 cooperation suite — **27 tests passed**, including missing-contact
  invite refusal, agreement-timeout departure, group-loot read-back, forged or
  injected chat, response-loss reconciliation and bounded friend backfill.
- `go -C lib/wow test -count=1 -race ./...` — passed.
- Party, loot and inventory module Go race suites — passed.
- Inventory protobuf bindings and descriptor regenerated successfully.
- `./scripts/lint-modules.sh` — `modules OK`.
- `gofmt` check on changed Go sources — clean.
- `python3 -I -m compileall -q scripts/growth` — passed.
- `bash -n scripts/session.sh scripts/account.sh` — passed.
- Source `git diff --check`, excluding generated protobuf descriptors — passed.
- Protocol layouts and constants were checked against AzerothCore commit
  `d80ce1d87720e6b6a0b9adc952f21a658b1c245e`.

## Not verified

No game session, automatic login, VM sync, container build or live gameplay was
run. Live AE1 social facts, AE2 refusal, AE3 cooperation, AE5 resource transfer,
group-loot roll and trade-disconnection reconciliation remain open. The final
five characters have not been created, and these local tests do not establish
four/five-session capacity, dungeon completion or the level-20 acceptance gate.
