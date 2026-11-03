# 2026-11-03 — Account isolation

## Decision

Accounts are either agent-owned or player-owned, never shared. The agent may
only operate on accounts explicitly listed in `agent-policy.json`. The user's
accounts are off-limits for login, session start, character creation and
character deletion.

Mixed accounts are a migration state, not the target: the agent character is
pinned by name until it can move to a dedicated account.

## Incident

While checking running sessions, an agent-wow client was found in
`aw-isawall317` playing **核桃壳三世** (level 74 hunter, the user's main). The
account had been used for concurrency testing on 2026-11-02. The session was
stopped (`session.sh stop isawall317`), which logged out and killed the client.
`Reachbot` on `doumoman` was left running; no user character was in it.

## Policy

`agent-policy.json` (workspace root, never passed to agent-wow):

```json
{
  "accounts": {
    "doumoboy1": {"characters": ["*"]},
    "doumoboy2": {"characters": ["*"]},
    "doumoboy3": {"characters": ["*"]},
    "doumoboy4": {"characters": ["*"]},
    "doumoboy5": {"characters": ["*"]}
  }
}
```

- Account absent → all access denied.
- `characters` omitted → no characters; `["*"]` → every character.
- `scripts/agent-policy.py` is the single checker:
  `account <name>` / `char <name> <character>`; exit 0 allowed, 3 denied.

Enforcement points:

- `scripts/session.sh start` checks account + character before syncing.
- `scripts/session.sh rpc` checks the account. `stop` bypasses the check on
  purpose so a rogue session can always be removed.
- `scripts/account.sh login` checks the account (a new login invalidates the
  session key and kicks the user). Re-running `init` on an existing account is
  checked too; `new`/first-time `init` stay open.
- `web/server.py` checks `/api/start`, `/api/login`, `/api/create`,
  `/api/delete`; `/api/stop` is allowed. `/api/chars` remains read-only.

The human accounts (`doumoman`, `isawall126`, `isawall317`) are not in the
policy at all. They stay in `accounts/` so the user can manage them, but the
agent must not log in, start a session, or create/delete characters there.

## Final account inventory (2026-11-03)

| Account | Role | worldrpc | State |
|---|---|---|---|
| `doumoboy1` | agent | 8093 | login rejected (bad credentials), needs re-init |
| `doumoboy2` | agent | 8092 | session valid, no characters |
| `doumoboy3` | agent | 8091 | session valid, no characters |
| `doumoboy4` | agent | 8090 | session valid, no characters |
| `doumoboy5` | agent | 8089 | session valid, no characters |
| `doumoman` | human | 8086 | off-limits; legacy `Reachbot` lives here |
| `isawall126` | human | 8087 | off-limits; legacy `Reachgirl` lives here |
| `isawall317` | human | 8088 | off-limits; user's main characters |

The lingering `Reachbot` session on `doumoman` was logged out and stopped, so
no agent session remains on a human account. `Reachbot` and `Reachgirl` still
exist on the human accounts but are off-limits from now on.

## Session keep-alive (2026-11-03)

The user's operating model: clicking 上线 selects one character and keeps it
online until 下线 is clicked; in between the agent may reconnect or restart it
as needed.

Implemented as a durable intent plus a keeper:

- `web/server.py` `/api/start` writes `accounts/<account>/session.json`
  (`desired: online`, `character`, `updated`); `/api/stop` writes
  `desired: offline` before stopping.
- `scripts/session-keeper.py` reconciles every 15s: restarts the selected
  character after EOF/crash, corrects a wrong character on the same account,
  and stops it when desired is offline. It only touches accounts allowed by
  `agent-policy.json`.
- The web server spawns the keeper on startup (`start_new_session`, log at
  `logs/session-keeper.log`); `flock` on `logs/session-keeper.lock` prevents
  duplicates.
- A 45s cooldown keeps the keeper from racing the web request that just wrote
  the intent. Failed starts back off 120s; an auth error triggers one
  `account.sh login` retry for that agent account.
- Temporary downtime without losing the selection:
  `session-keeper.py desired <account> offline`, then
  `... online <character>`.

## Next

Re-initialize `doumoboy1` with the correct credentials, then create agent
characters on `doumoboy1`–`doumoboy5`.
