# Modules

`module_dir` in `config.yaml` points here. agent-wow scans **every subdirectory**
of this folder and requires a valid `module.yaml` in each one
(`pkg/modules/discovery/manifest.go`). Consequences:

- Directory names must match `^[a-z][a-z0-9_-]*$`; `session` is reserved.
- Do **not** add `pkg/`, `_template/`, `shared/` or any other non-module
  directory here. Shared code lives in `../lib/wow`.
- Files at this level (`README.md`, `.gitkeep`) are ignored by discovery.

## Module map

| Module | Status | Purpose |
|---|---|---|
| `example` | disabled | Reference template and smoke test (`example.status`) |
| `sense` | implemented, verified | Read-only perception: self, nearby players/units, target |
| `move` | implemented, verified | Locomotion: forward/back/turn/stop plus position estimate |
| `combat` | implemented, verified | Melee swing, stop, spells, death and spirit-healer recovery |
| `character` | implemented, unit tested | Trainer purchases, talent learning/snapshots and game-object use |
| `party` | implemented, verifying | Accepts `SMSG_GROUP_INVITE` automatically (`party.get_state`, `party.set_auto_accept`) |
| `loot` | implemented, verifying | Opens corpses, selective/group-safe loot and money (`loot.loot`, `loot.take`, `loot.get_state`) |
| `inventory` | implemented, unit tested | Bags, equipment, item templates, vendor buy/sell/repair and buyback |
| `quest` | implemented, unit tested | Quest-giver menus, accept/turn-in, kill progress and rewards |

`sense` and `move` are both live. Note the split: `move` owns the character's
authoritative position (it dead-reckons while walking and re-seeds from
`SMSG_LOGIN_VERIFY_WORLD`), while `sense` owns other objects. An agent should
read its own position from `move.get_position`; `sense.get_self` is the snapshot
taken when the character entered the world (plus any server update received).

## Rules

- One module = one capability family. Keep the RPC surface small (<= 10).
- Directory name is the RPC prefix: `move/` exposes `move.forward`.
- Proto package: `<module>.module.v1`, service name `Module`, Go package alias
  `modulev1` (matches the upstream template).
- Commit `module.pb`; it is the descriptor agent-wow loads at runtime.
- Protocol bytes belong in `../lib/wow`, not in module handlers.
- Strategy stays in the agent. Modules translate intent into packets; they do
  not decide what to do.
- Dependencies are one-way (`combat` -> `sense`). Never create cycles.
- Every module needs a `README.md` documenting RPC inputs/outputs, units,
  subscribed opcodes, preconditions and what has actually been verified in game.

## Standard layout

```
modules/<name>/
├── module.yaml       # RPC aliases, packet subscriptions, requires
├── module.proto
├── module.pb
├── compose.yaml      # build context: ../..
├── Dockerfile
├── Makefile
├── go.mod
├── cmd/main.go
├── src/{api,server,service}/
└── README.md
```

## Build context

`compose.yaml` builds from the workspace root so modules can import `lib/wow`:

```yaml
services:
  module:
    build:
      context: ../..
      dockerfile: modules/<name>/Dockerfile
```

The root `.dockerignore` keeps `config/`, `data/`, logs and VCS data out of the
build context.

## Checklist for a new module

1. Copy `example/` to `modules/<name>/`.
2. Rename the proto package/module path and RPC aliases.
3. `make generate` (needs `protoc`, `brew install protobuf`).
4. Implement handlers in `src/service/`.
5. `../scripts/lint-modules.sh`, then `../scripts/build-modules.sh <name>`.
6. `../scripts/sync-to-vm.sh && ../scripts/session.sh start Reachbot`.
7. Verify in game, then write down what you verified in the module README.
