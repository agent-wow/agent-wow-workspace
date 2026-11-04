# U2 local verification — 2026-11-03

## Scope

U2 adds server-derived sense/move/combat facts and a class-profile-driven solo
growth loop. The Python worker discovers the live `sense` epoch, verifies the
roster identity before the first action of each epoch, and remains fail-closed
when `ready` or epoch is absent. The state machine covers target selection,
initial skill use, loot, rest/flee, stuck recovery, death/ghost/corpse recovery,
map/session invalidation, and bounded metrics.

The committed content is an initial warrior/priest/rogue and Elwynn sample, not
the final five-class roster or a complete level-20 route. Priest starts as a
caster without ranged melee auto-attack; rogue uses only Sinister Strike until
combo-point conditions gate a finisher.

## Test-first evidence

New regression tests failed before the corresponding fixes for:

- inferring `ready` from legacy `in_world`/position fields;
- acting with no live epoch and skipping the dynamic per-epoch identity gate;
- casting `in_combat_only` skills outside combat;
- fleeing instead of self-healing, and targeting a self-heal at the enemy;
- waiting forever for a ghost after release;
- ranged priest melee auto-attack and restored counters producing negative rates;
- stale combat zero values overwriting a fresh ready sense snapshot.

Protocol tests also observed compile-time red before parser implementation and
a malformed `SMSG_DEATH_RELEASE_LOC` fixture before correcting its 16-byte
layout.

## Final local verification

- `python3 -I -m unittest discover -s scripts/growth/tests -p 'test_*.py'`
  - **130 tests passed**.
- `go -C lib/wow test -count=1 -race ./...` — passed.
- `go -C lib/events test -count=1 -race ./...` — passed.
- `go -C modules/sense test -count=1 -race ./...` — passed.
- `go -C modules/move test -count=1 -race ./...` — passed.
- `go -C modules/combat test -count=1 -race ./...` — passed.
- `./scripts/lint-modules.sh` — `modules OK`.
- `scripts/regen-proto.sh sense`, `move`, and `combat` — generated bindings and
  descriptors from the committed schemas successfully.
- `gofmt -l` on all hand-written changed Go files — no output.
- `python3 -I -m compileall -q scripts/growth` — passed.
- Game object field offsets and aura packet layout were cross-checked against
  AzerothCore commit `d80ce1d8`.

## Not verified

No game session, automatic login, VM sync, container build, or live gameplay was
run. Final-class combat/recovery smoke, orientation/map-transition live smoke,
real module build discovery, four/five-session capacity and soak, and all AE
acceptance gates remain open. `config/roster.yaml` remains unconfigured. Unit
and fake-session tests do not establish live leveling capability.
