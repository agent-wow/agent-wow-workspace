# agent-wow workspace

A workspace for running isolated agent-wow sessions and compatible with your preferred
agent (codex, claude code, opencode, etc).

**Refer to the [agent-wow repository](https://github.com/agent-wow/agent-wow) for the full documentation.**

## Usage

All generated configuration files, data, modules, and scripts will be kept within
this workspace.

### Clone the repository

```bash
git clone --depth 1 https://github.com/agent-wow/agent-wow-workspace.git
cd agent-wow-workspace
```

If you are running concurrent agents, it is recommended for each agent to have
their own workspace.

### Set up agent credentials for AzerothCore

```bash
agent-wow auth init
```

You will be prompted to enter your agent's AzerothCore username and password.

### Start prompting

Assuming you have all the requirements from the agent-wow docs and valid AzerothCore
credentials, you can start prompting your agent to do things in WoW.

## Sandbox support

Use [Docker Sandboxes](https://docs.docker.com/ai/sandboxes) to run an agent with
access to this workspace, the internet, and AzerothCore on the host. The launcher
is `./.sandbox/sbx.sh` and supports all the [`sbx` built-in agents](https://docs.docker.com/ai/sandboxes/agents/).

The host needs Linux, access to `/dev/kvm`, Python 3, and Docker Sandboxes (`sbx`)
v0.46.0 or newer. AzerothCore must also be reachable from the sandbox on host ports
3724 and 8085.

Bootstrap a new sandbox with sbx:

```bash
./.sandbox/sbx.sh init codex
```

Check and run the sandbox:

```bash
./.sandbox/sbx.sh check
./.sandbox/sbx.sh run
```

For all supported commands:

```bash
./.sandbox/sbx.sh
```
