# Multi-account support

agent-wow resolves exactly one credential file and one saved session per run:

- `config_dir/auth.json` — username + password
- `data_dir/` — badger DB holding the authserver session key
- `config_dir/realm.json` — selected realm

`auth status` reads the saved key from `data_dir`, **not** `auth.json` — so two
accounts sharing one `data_dir` will report whichever session was written last.
Verified on 2026-11-02: a config pointing at a fresh `data_dir` returns
`no saved session`.

## Layout

Each account gets its own pair, selected via the global `--config` flag:

```
accounts/<username>/agent-wow.yaml   # config_dir + data_dir for this account
accounts/<username>/config/auth.json # credentials (gitignored)
accounts/<username>/data/            # session key (gitignored)
```

The directory name **is the auth username, lowercased** (the filesystem is
case-insensitive). `account.sh init` renames the directory automatically once
`auth init` has written the username, and every command lowercases its argument,
so `account.sh chars ISAWALL126` and `... chars isawall126` are equivalent.

`accounts/doumoman/` follows the same layout as every other account. The root
`config.yaml` also points at `accounts/doumoman/{config,data}`, so the legacy
single-account flow (no `--config`) keeps working; the old top-level `config/`
and `data/` directories no longer exist.

## Accounts

| Directory | Username | worldrpc | Notes |
|---|---|---|---|
| `doumoman` | DOUMOMAN | 8086 | 6 characters |
| `isawall126` | ISAWALL126 | 8087 | character: Reachgirl (Human Priest) |
| `isawall317` | ISAWALL317 | 8088 | 8 characters |

Note: re-running `auth login` for an account invalidates that account's previous
session key. A client already in the world gets kicked and must reconnect (the
leveling supervisor restarted Reachbot automatically when this happened).

## Usage

```bash
scripts/account.sh ls                  # list accounts
scripts/account.sh init <name>         # interactive credential setup
scripts/account.sh login <name>
scripts/account.sh chars <name>
scripts/account.sh play <name> <char>  # starts in the Colima VM
```

New accounts auto-allocate the next free worldrpc port (`new --port N` overrides).

For the VM path (modules), `scripts/session.sh` honours `ACCOUNT=<name>`; each
account plays from its own VM directory `aw-<name>` with its own worldrpc port,
log and pid file, so several accounts can run side by side. The host copy of
`accounts/*/data` is authoritative and is shipped to each VM replica. Gameplay
never writes the saved session key (only `auth login` does), so `session.sh stop`
must not copy VM account data back over the host.

## Concurrent verification (2026-11-02)

Verified three simultaneous clients on the private realm, each with five module
containers and an independent RPC endpoint:

- port 8086: `Reachbot` (`doumoman`)
- port 8087: `Reachgirl` (`isawall126`)
- port 8088: `希尔瓦娜姐` (`isawall317`)

`sense.get_self` returned the correct character on every port. Logging out and
stopping the third client left the first two online and their module containers
intact. Process discovery must use `pgrep -f "[a]gent-wow.*char play"`: accounts
pass `--config` between `agent-wow` and `char`, so the older contiguous pattern
misses those clients.

Resource usage in the 4-core / 3.9-GiB Colima VM: 1 / 2 / 3 sessions used about
1.28 / 1.42 / 1.55 GiB respectively (whole-VM `used`, including Docker and other
processes). Client RSS was about 25–35 MiB each; five idle module containers used
about 20–30 MiB per session and very little CPU. There is no graphics rendering.

## Web console (2026-11-02)

`web/` adds a local console for account/character state and session control:

- `web/server.py` — Python standard library only (`http.server`), no third-party deps.
- `web/static/` — single page, no build step.
- Backend shells out to `account.sh` / `session.sh` / `agent-wow`; it does not
  reimplement any of that logic.

Endpoints: `/api/state`, `/api/chars`, `/api/self`, `/api/logs` (GET) and
`/api/start`, `/api/stop`, `/api/login` (POST). See `web/README.md`.

Two findings worth keeping:

- Process discovery must use `pgrep -f "[a]gent-wow.*char play"`. Accounts pass
  `--config` between `agent-wow` and `char`, so a contiguous pattern misses them.
- worldrpc is **port**-scoped, not directory-scoped: an account's RPC works
  regardless of which VM directory its client runs from. Only `stop` and `logs`
  need the real directory, because they match on the process cwd.

Verified end to end: start → in-world `sense.get_self` → stop, with the other
two accounts staying online throughout.

## 角色金钱字段（2026-11-03）

3.3.5a 的金钱是 `PLAYER_FIELD_COINAGE`，**单个 uint32**（不是 64 位）：

```
UNIT_END                = 0x0094
PLAYER_FIELD_COINAGE    = UNIT_END + 0x03FE = 0x0492
```

TrinityCore 3.3.5 `Player::SetMoney(uint32)` → `SetUInt32Value(PLAYER_FIELD_COINAGE, value)`，
`MAX_MONEY_AMOUNT = INT32_MAX`。

**踩过的坑**：XP 字段后面紧跟的 `PLAYER_SKILL_INFO_1_1 = 0x027C`（Size 384，TWO_SHORT）
不是金钱。之前按 `Uint64(0x027C)` 取值，把「技能信息低 32 位」和「下一个 slot」拼成了一个
假 64 位数（几十亿量级）。真钱在 `0x0492`，紧邻 `PLAYER_REST_STATE_EXPERIENCE = 0x0491`。

**验证方式**：`sense.debug_fields` 导出原始 slot。两个锚点互相印证 ——
74 级猎人 `0x492 = 17775320`（≈1777 金 53 银），7 级新号 `0x492 = 10`（铜）。
`0x491` 分别是 59606 / 1，与「休息状态经验」语义吻合。

### 字段偏移权威来源

`gh api "repos/TrinityCore/TrinityCore/contents/src/server/game/Entities/Object/Updates/UpdateFields.h?ref=3.3.5"`
（分支名带 `?ref=` 时必须给 URL 加引号，否则 zsh 报 `no matches found`）。

`raw.githubusercontent.com` 与 `curl` 在本机被沙箱挡掉，`gh api` 可以走通。
