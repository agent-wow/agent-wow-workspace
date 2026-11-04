# AI 角色上天遁地：地形高度修复（2026-11-04）

## 根因

`feat/autonomous-growth-mvp` 的 `move` 模块没有地形数据：

- `ForwardPosition` 固定使用起点 Z。
- `MoveTo` 只在线性插值起点和请求终点 Z。
- 每个 heartbeat 会把该估计坐标发给服务器，附近客户端因而看到角色上坡钻地、下坡悬空。

这与私服服务器无关；正常 WoW 客户端只是显示服务器广播的位置。

## 本次修复

- `lib/wow/terrain.go` 增加 AzerothCore map-v9 读取、tile 缓存、flat/float/uint8/uint16 高度解码、hole 检查和与 `GridTerrainData` 一致的三角插值。
- `move` 的起点、每个 heartbeat 和终点都按当前 map/X/Y 采样地面 Z。
- 地形缺失、hole、非有限高度返回 `Unavailable`；与估计高度相差超过 8 码时拒绝移动，避免桥梁、室内或悬崖场景被错误吸附到地表。
- 中途地形失效时，在最后可靠位置发送 `MSG_MOVE_STOP`。
- `move.recover_ground` 提供显式人工恢复：确认旧保存 Z 已错误后，可绕过 8 码保护发送一次贴地停止包；普通移动不会自动调用它。
- `compose.yaml` 将 `data/client-data/terrain-v20/maps` 只读挂载到 `/data/maps`。
- `fetch-client-data.sh --terrain` 下载并校验 wowgaming/client-data v20.0，只解出 maps。
- `fetch-terrain-tiles.py` 可用 HTTP Range 精确下载少量 tile，ZIP CRC 校验通过。
- `sync-to-vm.sh` 同步并保留静态 terrain 数据。

## 已验证

- `go -C lib/wow test ./...`：通过。
- `go -C modules/move test ./...`：通过。
- `go -C modules/move test -race ./src/service`：通过。
- `go vet`（lib/wow、move）：通过。
- 三种移动方式的所有 start/heartbeat/stop 包均逐点检查地面 Z。
- 地形中途失效会停止；危险高度差不会发送移动包。
- `lint-modules.sh`、Compose 配置、shell 语法和 `git diff --check`：通过。
- growth 同步脚本测试：2 个通过，包括 terrain 入包断言。

全量测试中的既有/环境性失败：

- `modules/example/src/server.TestModulePackage`：原分支 `enabled: false` 与测试期望不符；本次未改 example。
- `test_vm_backup...restore...`：检测到真实 growth workers 正在运行而拒绝恢复；本次未改备份逻辑，也未停止角色。

- 真实 v20 地图校验通过：Northshire `83.5317` vs 实测 `83.53`；Valley of Trials `38.1494` vs 实测 `38.2334`。
- 当前默认地图目录包含角色实际经过区域需要的 5 个 tile；未覆盖区域 fail closed。

## 实机部署与验证

修复已选择性合入主工作区，保留其已有的 `network_mode: none` 和其他未提交改动。五个授权 AI 角色均由主工作区 keeper 管理并恢复原职业在线；没有修改私服，也没有登录或启动人类账号。

- `腰果战士`：2 码往返，Z 从登录估计 `38.2334` 校正为 `38.1381` / `38.1494`。
- `腰果萨满`：2 码往返，Z 为 `38.1393` / `38.1494`。
- `腰果猎人`：登录估计 `67.7011`，移动到真实地形的 `63.9199` / `63.6542`，纠正约 4.05 码悬空。
- `腰果牧师`：附近服务器 NPC 的 Z 为 `37.55–48.29`，确认旧保存位置 `17.5719` 是钻地而非桥梁/室内；显式 `move.recover_ground` 恢复到 `39.6562`，之后正常移动。该角色所在区域补充下载了 `0013140.map`。
- `腰果术士`：2 码往返均为 `58.4304`；真实地图独立解析两点也都是 `58.4304`，确认这是平地而非旧固定 Z。
- 五个运行中的 move 容器都确认挂载 `/data/maps`，最后检查时近期日志没有地形错误。
- 正常移动仍保留 8 码保护；`move.recover_ground` 是独立、显式的人工恢复 RPC，不会被普通 forward/backward/move_to 自动触发。

部署期间有一次未指定账号 VM 目录的构建同步到了默认 `aw` 副本。没有运行中的人类会话，主机账号凭据未改，也没有登录/启动人类账号；默认 VM 副本应在以后由主工作区正常同步恢复，不在本次 AI 账号部署中继续操作它。

## 尚未完成

- 由人类观察账号进行最终视觉确认；本次实机结论来自服务器坐标、附近 NPC 坐标、RPC 和真实地图交叉验证。
- 当前是局部地图覆盖，角色进入其他未下载 tile 时会安全停止；需要更大地图覆盖时继续补 tile 或下载完整 maps。

## 边界

这只解决户外地形贴地，不是完整寻路：仍然直线移动、不绕障，也未采样 VMap 桥梁和建筑。后续可在此地形基础上接 mmaps/Detour。
