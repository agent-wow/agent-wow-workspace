# U3 local verification — 2026-11-03

## Scope

U3 adds server-backed item, equipment, container, buyback and vendor facts. The
new `inventory` module accumulates sparse update fields, queries item templates
asynchronously, and exposes bounded buy/sell/repair/equip/buyback packets. It
does not treat a successful send as proof that the server applied an action.

The growth runtime scores equipment from class stat weights, armor and weapon
DPS without a quality bonus. Two-handed weapons are compared with the current
main-hand plus off-hand score. Unknown templates are not equipped or sold.
Bag-capacity purchases precede supplies, and a bag is not repurchased when the
character already owns equal or greater capacity.

Every growth inventory mutation uses a stable checkpoint intent and dispatches
at most once. Lost responses are reconciled from fresh revision, item count,
money, durability, equipment-slot and failure facts. Ambiguous results expire
as abandoned rather than triggering a second purchase, sale, repair or equip.

Loot now exposes raw slot/type facts and selective `loot.take`. Solo auto-loot
accepts only allow-loot/owner rows; roll, master and locked rows are excluded.
Disabling auto-loot preserves the window for manual selection. A new manual
window no longer inherits the pending flag of an older automatic worker.

## Test-first and correction evidence

- A purchase fixture initially expected a 25-byte `CMSG_BUY_ITEM`; the actual
  request is 21 bytes. The test was corrected to the server layout rather than
  padding the wire packet.
- Initial inventory snapshots exposed missing GUID high halves and a bag/slot
  type mismatch. Fixtures gained complete update fields; production GUID
  validation remained strict.
- Manual loot initially obeyed the global auto-loot switch and could release
  without taking selected rows. Jobs now distinguish automatic and manual takes.
- A disabled loot window arriving while an older automatic job was pending could
  remain permanently blocked. The new-window pending assignment now depends on
  that window's own mode, with a regression test.
- Worker fakes initially lacked the new inventory read, producing two errors in
  the full Python suite. The shared fake now returns an empty server-fact state.
- Direct tests cover equal-capacity bag suppression, unknown-template protection,
  two-handed comparison, full-bag sale protection, one-based vendor slots,
  response-loss read-back, bounded retry suppression and equipment refusal.

## Final local verification

- `python3 -I -m unittest discover -s scripts/growth/tests -p 'test_*.py'`
  - **148 tests passed**.
- `go -C lib/wow test -count=1 -race ./...` — passed.
- `go -C modules/inventory test -count=1 -race ./...` — passed.
- `go -C modules/loot test -count=1 -race ./...` — passed, including the
  superseded-pending-window regression.
- `make -C modules/inventory generate` and `make -C modules/loot generate` —
  protobuf bindings and descriptors regenerated successfully.
- `./scripts/lint-modules.sh` — `modules OK`.
- `gofmt` check/formatting on changed Go sources — clean.
- `python3 -I -m compileall -q scripts/growth` — passed.
- `git diff --check` — passed.
- Item, inventory and vendor layouts were cross-checked against AzerothCore
  commit `d80ce1d87720e6b6a0b9adc952f21a658b1c245e`.

## Not verified

No game session, automatic login, VM sync, container build or live gameplay was
run. Live buy/sell/equip/full-bag recovery remains open for every final character;
selective/group-safe loot has not had a live group test. The current nearby-NPC
policy is not a mapped vendor route, and no route claims unattended full-bag
recovery. The three current profiles and Elwynn content are not the final
five-class roster or complete level-20 route. Local tests do not satisfy AE4,
capacity, dungeon or five-character acceptance.
