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

- Linux
- AzerothCore servers
- Go 1.27.1 or newer
- Docker and Docker Compose
- `protoc` CLI
- `agent-wow` CLI

### 2. Auth

Run `agent-wow auth status` to check if your auth session is valid. If not, run
`agent-wow auth login` and recheck again.

## Gameplay sessions

Before running `agent-wow char play ...`, check if the worldrpc port is in use since
there may be concurrent agents at play. If so, assign a different port with the environment
variable `AGENT_WOW_WORLDRPC_PORT`.

## Workspace directories

Any assets used or generated for your task must be kept within this workspace. That
includes configuration files, data, modules, scripts, etc. Some directories are available
for you to use.

- `./module`: agent-wow modules you build.
- `./scripts`: Scripts you use to interact with the gameplay session.
- `./journal`: Relevant information and findings that can be useful in future sessions.
- `./logs`: Any log outputs for debugging.
- `./.sandbox`: Not relevant for gameplay. Do not run, edit, or save anything here.
