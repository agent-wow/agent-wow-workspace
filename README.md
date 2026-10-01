# agent-wow workspace

A workspace for running isolated agent-wow sessions and compatible with your preferred
agent (codex, claude code, opencode, etc).

**Refer to the [agent-wow repository](https://github.com/agent-wow/agent-wow) for the full documentation on modules.**

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
