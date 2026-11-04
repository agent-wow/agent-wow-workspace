# Web 控制台

本地 Web 界面，用于查看账号 / 角色状态，以及上下线游戏会话。

- 后端：`server.py`（Python 标准库，零第三方依赖）
- 前端：`static/`（单页，纯 JS，无构建步骤）
- 默认地址：<http://127.0.0.1:8765>（只监听本机）

## 启动

```bash
python3 web/server.py              # 前台运行
WOW_WEB_PORT=9000 python3 web/server.py   # 换端口
```

后台常驻：

```bash
nohup python3 web/server.py > logs/web.log 2>&1 &
```

## 功能

| 卡片操作 | 行为 |
|---|---|
| 在线状态 | 扫描 Colima 里的 `agent-wow char play` 进程，按 VM 目录（`aw-<账号>`）归属到账号 |
| 角色状态 | `sense.get_self`：名字、等级、种族、职业、阵营、地图、经验百分比、金钱、HP / XP 条、专业、战斗与死亡状态、坐标与朝向，每 6 秒轮询 |
| 在线时长 | `sense.get_played_time`：在线总时长、此次在线、本级时长、本次登录时间；离线角色显示缓存在 `data/playtime.json` 的上次值 |
| 选择角色上线 | agent 账号：写入在线意图，由 `session-keeper.py` 启动；人用账号：直接 `session.sh start`，不写意图 |
| 创建角色 | `agent-wow char create`，可选名字 / 种族 / 职业 / 性别；种族与职业按 3.3.5a 兼容表联动 |
| 删除角色 | 二次确认弹窗，需手动输入 `DELETE` 才放行；账号在线时拒绝删除 |
| 下线 | agent 账号：清除在线意图，由 keeper 停掉；人用账号：直接停掉，不写意图 |
| 日志 | `session.sh logs`，取该账号 VM 目录里的 `logs/session.log` |
| 重新登录 | `agent-wow auth login`，用于会话密钥失效时恢复 |

## 保持在线（session keeper）

点「上线」不只是启动一次，而是写入一条持续意图：

```json
// accounts/<账号>/session.json
{"desired": "online", "character": "角色名", "updated": 1762...}
```

Web 后端会自动拉起 `scripts/session-keeper.py`（日志 `logs/session-keeper.log`），
它每 15 秒把意图和实际客户端对一遍：

- 角色掉线、worldserver EOF、客户端退出 → 重新上线同一个角色
- 同一账号在线的是别的角色 → 先停掉，再上线意图里的角色
- 点「下线」写入 `desired: offline` → 停掉并不再拉起
- 客户端进程还在，但 RPC 或必需模块连续失败 3 次 → 有界重启该角色
- 会话密钥失效 → 进入 degraded，等待人工重新登录；**不会自动 `auth login`**

keeper 只处理 `agent-policy.json` 里允许的账号，人用账号的意图会被忽略。
人用账号是**纯手动**模式：上线、下线、建号、删号、重新登录都由操作者点击触发，
掉线后不会自动重连（这正是隔离的边界——agent 不自动接管人的账号）。
人工 `STOP`（默认 `data/growth/STOP`）存在时，keeper 不启动、不重启会话；
`desired: offline` 的停止动作仍然允许。`STOP` 只能由人工解除。
查看状态：`python3 -I scripts/session-keeper.py status`。

## 角色信息面板的数据来源

面板字段全部来自 `sense.get_self`（`SelfResponse`）：

| 显示项 | 来源 |
|---|---|
| 等级 / HP / 经验 | `unit.level`、`unit.health`、`unit.xp`、`unit.next_level_xp` |
| 种族 / 职业 | `unit.race_id`、`unit.class_id` → `static/game-data.js` 的 ID 表 |
| 阵营 | 由种族推导（联盟种族表） |
| 地图 | `map_id` → ID 表 |
| 金钱 | `unit.money`（铜币总数，面板按金/银/铜拆分） |
| 战斗 / 死亡 | `unit.in_combat`、`unit.dead` / `player_flags` 位 0x10 |
| 坐标 / 朝向 | `unit.position`、`unit.orientation` |
| 专业 | `self.skills`（`Skill` 列表，取 `profession == true` 的项）→ 名字由 `skillline_dbc` 给出，中文走 `static/game-data.js` 的技能 ID 表 |
| 在线总时长 / 本级时长 | `playedTime.totalSeconds` / `playedTime.levelSeconds`，来自服务端 `SMSG_PLAYED_TIME`，即游戏内 `/played` 的两个数 |
| 此次在线 / 本次登录 | `playedTime.sessionSeconds` / `playedTime.sessionStartedUnix`，由 sense 模块从本次 `SMSG_LOGIN_VERIFY_WORLD` 起算；换地图不清零 |
| 离线角色的时长 | `data/playtime.json` 中上次在线时缓存的值；从未经控制台上线的角色显示 `—` |

**专业的来源**：3.3.5a 没有 `SMSG_INITIAL_SKILLS`，技能和专业只以玩家自己的
更新字段 `PLAYER_SKILL_INFO_1_1`（0x027C）下发，每条记录 3 个 field。
解码在 `lib/wow/skills.go`，名字来自 `skillline_dbc`（见 `lib/wow/dbc`），
完整的数据源清单见 `docs/data-sources.md`。

专业列表也可以单独取：`sense.get_skills`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 账号列表 + 在线状态 + 在线意图 + keeper 是否运行（一次 SSH 扫描） |
| GET | `/api/chars?account=&refresh=` | 角色列表（默认缓存 300 秒）；每行带 `playtime` 缓存（可能为 `null`） |
| GET | `/api/self?account=` | 实时角色状态 `self`，外加 `playedTime`（模块不支持时为 `playedError`） |
| GET | `/api/logs?account=&lines=` | 会话日志 |
| GET | `/api/creation-options` | 种族 / 职业 / 性别选项与兼容表 |
| POST | `/api/start` | `{account, character}` 上线 |
| POST | `/api/stop` | `{account}` 下线 |
| POST | `/api/login` | `{account}` 重新登录 |
| POST | `/api/create` | `{account, name, race, class, gender}` 创建角色 |
| POST | `/api/delete` | `{account, name, confirm}` 删除角色，`confirm` 必须严格等于 `DELETE` |

## 注意

- 后端是现有脚本的薄封装：`account.sh` / `session.sh` / `agent-wow`，不重复实现逻辑。
- 人用账号在命令行里同样需要手动开关：`AGENT_WOW_MANUAL=1 scripts/account.sh login doumoman`
  / `AGENT_WOW_MANUAL=1 scripts/account.sh play doumoman <角色>`。这个环境变量只给操作者用，
  agent 与 keeper 都不会设置它。
- 「重新登录」会刷新会话密钥，该账号已在线的角色会掉线（界面上有确认弹窗）。
- 上线只写入意图；keeper 拉起的 `session.sh start` 才会触发 `sync-to-vm.sh`，
  且只同步该账号自己的凭据目录到 `aw-<账号>`，约 10–30 秒。
- 在线时长依赖 sense 模块的 `sense.get_played_time`。`char play` 启动模块时执行
  `docker compose up --build`，所以改完代码后**重新上线一次**（下线 → 上线）即可生效；
  尚未重启的旧会话下控制台仍可用，只是时长显示「不可用」。
- `data/playtime.json` 是运行时缓存（`data/` 已 gitignore），只存服务端返回过的数值；
  离线角色显示的是**上次下线前最后一次轮询**的值，不是从数据库重新查的。
- 密码不会出现在任何接口响应里：后端只读 `auth.json` 的 `username` 字段。
- 删除角色有三道防线：前端弹窗必须输入 `DELETE`；后端校验 `confirm` 字段严格等于 `DELETE`；
  账号仍在世界中时后端直接拒绝，避免把角色从运行中的客户端脚下抽走。
