# Instructions for AI agents

This workspace is for you to execute on a given task as a player on AzerothCore
using agent-wow.

Useful resources to reference:
- agent-wow: https://github.com/agent-wow/agent-wow
- azerothcore: https://github.com/azerothcore/azerothcore-wotlk
- wow client-data: https://github.com/wowgaming/client-data/releases
- agent-wow module template: https://github.com/agent-wow/go-module-template

## System checks

Before executing on a task, you should do a quick system check to ensure you have
all the requirements to play. If any of these fail, do not continue and suggest
debugging tips.

### 1. Dependencies

- Linux (on macOS, run the client and modules inside the Colima VM — see below)
- AzerothCore servers (this workspace targets a private WotLK 3.3.5a server)
- Go 1.27.1 or newer
- Docker and Docker Compose
- `protoc` CLI
- `agent-wow` CLI

### 2. Auth

Run `agent-wow auth status` to check if your auth session is valid. If not, run
`agent-wow auth login` and recheck again — **only for an account listed in
`agent-policy.json`** (see below).

### 3. Account isolation

The workspace may hold credentials for the user's own accounts. The agent may
only log in, start sessions, and create/delete characters on accounts listed in
`agent-policy.json`. Accounts not listed are the user's and are off-limits.
Even on a listed account, only the characters in its `characters` array are
allowed (`"*"` means all).

Every entry point enforces this (`account.sh`, `session.sh`, `web/server.py`),
but the rule matters: never run `auth login` on an off-limits account, because
it invalidates that account's session key and disconnects the user's client.
`session.sh stop` / the console's stop button are deliberately allowed so an
accidental session can always be removed.

New agent accounts must be added to `agent-policy.json` with `"characters":
["*"]` before use. Keep user accounts out of that file.

### 4. Account ownership

| Account | Role | worldrpc | Notes |
|---|---|---|---|
| `doumoboy1` | agent | 8093 | dedicated agent account |
| `doumoboy2` | agent | 8092 | dedicated agent account |
| `doumoboy3` | agent | 8091 | dedicated agent account |
| `doumoboy4` | agent | 8090 | dedicated agent account |
| `doumoboy5` | agent | 8089 | dedicated agent account |
| `doumoman` | human | 8086 | off-limits; contains legacy test char `Reachbot` |
| `isawall126` | human | 8087 | off-limits; contains legacy test char `Reachgirl` |
| `isawall317` | human | 8088 | off-limits; the user's main characters |

Only `agent-policy.json` is authoritative for enforcement. The human accounts are
kept in `accounts/` so the user can manage them, but the agent must never log
them in, start a session on them, or create/delete characters there. The legacy
`Reachbot` / `Reachgirl` characters sit on human accounts and are therefore
off-limits too; new agent characters belong on `doumoboy1`–`doumoboy5`.

### 5. Session intent (console 上线 / 下线)

Clicking 上线 in the web console records a durable intent in
`accounts/<account>/session.json` (`desired: online`, `character: <name>`).
`scripts/session-keeper.py` reconciles that intent with the live client every
15 seconds: it restarts the selected character after a worldserver EOF or
crash, and stops it when the user clicks 下线 (`desired: offline`).

The agent may restart/reconnect the selected character as needed, but while an
intent is online it must not switch to another character or account. To take
that character down temporarily without losing the selection:

```bash
python3 -I scripts/session-keeper.py desired doumoboy2 offline
python3 -I scripts/session-keeper.py desired doumoboy2 online <character>
```

`python3 -I scripts/session-keeper.py status` shows intents and live clients;
`--dry-run --once` previews what the keeper would do. Do not stop the keeper
while intents are online.

## Gameplay sessions

Before running `agent-wow char play ...`, check if the worldrpc port is in use since
there may be concurrent agents at play. If so, assign a different port with the environment
variable `AGENT_WOW_WORLDRPC_PORT`.

## Workspace directories

Any assets used or generated for your task must be kept within this workspace. That
includes configuration files, data, modules, scripts, etc. Some directories are available
for you to use.

- `./modules`: agent-wow modules you build. Read `modules/README.md` first.
- `./lib/wow`: shared Go library for WoW packet/protocol helpers. Modules import it;
  do not duplicate protocol logic in a module.
- `./scripts`: Scripts you use to interact with the gameplay session.
- `./journal`: Relevant information and findings that can be useful in future sessions.
- `./docs`: Design notes. Start with `docs/data-sources.md` before looking for
  game data: it says which source is authoritative for names, items, loot,
  quests and trainers.
- `./logs`: Any log outputs for debugging.
- `./.sandbox`: Not relevant for gameplay. Do not run, edit, or save anything here.

## Module conventions (short version)

Every direct child of `modules/` must be a valid module: a directory name matching
`^[a-z][a-z0-9_-]*$` (excluding `session`) containing `module.yaml`, `module.pb`,
`compose.yaml`, `Dockerfile`, and Go source. There is no "misc" subdirectory — the
shared library lives in `lib/` for that reason.

Each module owns one capability. Its RPC names are prefixed with the module name
(`sense.get_self`), it declares dependencies via `requires`, and strategy belongs to
the agent, not the module. The module image builds from the workspace root
(`context: ../..`) so it can `COPY lib/wow` if needed.

Run `./scripts/lint-modules.sh` after any structural change.

## Running on macOS (Colima)

Module containers need real Linux Unix sockets; Docker Desktop's virtiofs cannot
share them. Use the Colima VM:

```bash
scripts/sync-to-vm.sh                         # push workspace to colima:$HOME/aw
scripts/build-modules.sh [module]             # docker compose build inside the VM
ACCOUNT=doumoboy1 scripts/session.sh start <character>
scripts/session.sh status
ACCOUNT=doumoboy1 scripts/session.sh rpc session.logout
ACCOUNT=doumoboy1 scripts/session.sh stop
ACCOUNT=doumoboy1 scripts/session.sh logs 100
```

The host copy is the single source of truth. Never edit `colima:$HOME/aw` directly;
it is wiped and re-created on every sync.

### Multiple accounts at once

Each account has its own config/data directories, worldrpc port and VM directory
(`aw-<account>`), so sessions can run concurrently:

```bash
scripts/account.sh ls
scripts/account.sh play doumoboy1 <character>
ACCOUNT=doumoboy1 scripts/session.sh rpc sense.get_self
ACCOUNT=doumoboy1 scripts/session.sh logs 100
ACCOUNT=doumoboy1 scripts/session.sh stop
```

Do not run `auth login` for an account that is already in the world: a new login
invalidates its previous session key and disconnects the running client.
