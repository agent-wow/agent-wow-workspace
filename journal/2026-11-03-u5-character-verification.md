# U5 local verification — 2026-11-03

## Scope

The `character` module exposes trainer-list/open/buy, talent learning and
snapshots, and game-object use. `combat` owns the packet-derived spellbook and
reports `initialized=false` until `SMSG_INITIAL_SPELLS`; a fresh login clears
it, while a map transition retains it. Crafting can preserve an empty cast
target instead of inheriting the current combat target.

The growth worker selects class spells, role talents and profession recipes
from versioned profiles and server facts. Trainer, talent, craft and gather
mutations use durable intents. Lost replies are reconciled from spellbook,
trainer result, talent rank, skill, inventory, epoch and nearby-object facts;
a timeout becomes `abandoned`, never an automatic second dispatch. Retry
cooldowns are checkpointed so a process restart cannot immediately repeat an
abandoned craft or gather. `sense.get_skills` remains the only skill/profession
snapshot.

Herbalism casts its gathering spell at the game object and leaves the resulting
loot window to the existing auto-loot worker. Trainer and gathering transport
failures and three unanswered trainer opens enter bounded cooldowns rather than
escaping the current growth step or retrying forever. Open trainer snapshots
are refreshed so level- or skill-dependent rows do not remain stale.

## Protocol corrections

Layouts were checked against AzerothCore commit
`d80ce1d87720e6b6a0b9adc952f21a658b1c245e`.

- `SMSG_TRAINER_LIST` has a 16-byte header (GUID, trainer type, spell count),
  38-byte spell rows and a NUL-terminated greeting. `ReqSkillLine` and
  `ReqSkillRank` are `uint32`. An earlier 20-byte-header/34-byte-row
  interpretation was wrong and was corrected in both parser and fixtures.
- Trainer-buy success is 12 bytes; failure is 16 bytes with a `uint32` reason.
  Failure reason zero (`Unavailable`) is not success; the module exposes an
  explicit result-known bit.
- `SMSG_LEARNED_SPELL` is six bytes: spell ID plus a zero `uint16`.
- Player `SMSG_TALENTS_INFO` uses byte-sized group/talent/glyph counts and no
  preview rows. Talent ranks are zero-based. Pet snapshots do not overwrite the
  player's talent state.
- Trainer requests and game-object use carry an unpacked `uint64` GUID.
- A gathering cast uses target mask `0x0800` and the node's packed GUID. Herb
  nodes are chest game objects; plain `CMSG_GAMEOBJ_USE` does not run the core's
  open-lock loot/skill-up path.

## Review corrections

Read-only reviews identified cold-start spellbook ambiguity, unbounded trainer
opens, escaping transport errors, process-local retry cooldowns, and false
read-back confirmations. Corrections now prevent an existing profession skill
line from confirming a recipe purchase, require exact one-craft material
consumption when skill-up evidence is absent, and reject node disappearance as
proof across different session/map epochs. Tests cover cooldown persistence
across runtime reconstruction, enabled/disabled/unknown spellbook entries,
training-budget boundaries, and the learn-talent packet.

The dedicated `ce-code-review` run did not produce a completed review receipt.
All eight dedicated branches reached terminal outcomes: project standards,
agent surface, maintainability, testing, reliability, API contracts,
correctness and adversarial. Correctness returned a compact result rather than
writing its artifact. A harness-native review completed as the interactive
fallback (`Code review: harness-native fallback`). Its valid findings and the
late dedicated findings were checked against the live tree. Fixes included the
game-object gathering cast, craft material key normalization, trainer refresh
and category selection, specific gathering evidence, correct trainer-state
constants, the trainer-buy result reset ordering, and the default first-rank
talent prerequisite. Cooldown metadata pruning remains a future capacity
improvement; ambiguous mutations remain bounded and are never redispatched
automatically. No live server verification has been performed.

Simplification removed unused imports and the unused `WorldView.game_objects`
field, eliminated the duplicate in-memory cooldown copy, replaced a test's
private target mutation with the public attack RPC, and added the map-transition
spellbook proof before retaining the corresponding behavior comment. Broader
cross-layer view refactors and removal of the planned trainer-money read were
skipped. Existing growth proof tests were retained; only the fake adapter was
extended to model the corrected gathering cast.

## Local verification

- `python3 -I -m unittest discover -s scripts/growth/tests` — **203 tests passed**.
- `go -C lib/wow test -count=1 -race ./...` — passed.
- `go -C modules/character test -count=1 -race ./...` — passed.
- `go -C modules/combat test -count=1 -race ./...` — passed.
- `go vet` for `lib/wow`, `modules/character` and `modules/combat` — passed.
- All non-example module Go suites — passed. The all-module run also exposed
  the unrelated pre-existing `modules/example/src/server/TestModulePackage`
  failure: it expects exactly two public RPCs. The example module was not
  changed in U5.
- `./scripts/lint-modules.sh` — `modules OK`.
- `python3 -m compileall -q scripts/growth` — passed.
- Source `git diff --check -- . ':(exclude)**/*.pb'` — passed.
- Unfiltered `git diff --check` treats arbitrary bytes in the generated binary
  `modules/combat/module.pb` descriptor as text trailing whitespace; generated
  descriptors are therefore excluded from the source whitespace check.

## Not verified

No game session, automatic login, VM sync, container build or live gameplay was
run. Trainer/talent/craft/gather live reconciliation and the five-character AE6
profession-growth acceptance remain open. The final five characters have not
been created, and these tests do not establish four/five-session capacity,
full dungeon completion or the level-20 acceptance gate.
