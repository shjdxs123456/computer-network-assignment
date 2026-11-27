# 手机端对接说明（给手机端同学）

> 本文只讲一件事：**手机 App 该怎么跟电脑通信**。手机端不用理解电脑怎么做决策，
> 只需要做三件事：**连上电脑、如实上报"我在播什么"、收到指令就执行暂停**。
> 完整协议规格见 [../computer/net/protocol.md](../computer/net/protocol.md)，本文是它的"手机视角精简版 + 可照抄的字段表"。

---

## 0. 分工一句话

| 角色 | 职责 |
| --- | --- |
| 电脑（TCP 服务端） | 监听 8765 端口，做决策，下发"该谁播 / 暂停谁" |
| 手机（TCP 客户端） | 主动连电脑，上报播放状态，收到暂停指令就暂停对应 App |

手机是"被指挥的一方"：只上报、只执行，不参与决策。

---

## 1. 连接方式

- **传输**：TCP。手机主动连电脑，电脑不主动找手机。
- **地址**：电脑和手机连**同一个 WiFi**。手机里填电脑的 IPv4 地址（形如 `192.168.1.23`）。
- **端口**：`8765`（写死在电脑配置里，通常不用改）。
- **配对码**：6 位数字（默认 `123456`，电脑启动时打印在屏幕上）。手机连上后第一条消息必须带上它，错了会被直接断开。
- **协议版本**：`1`，写死。版本不一致也会被断开。
- **连接超时**：TCP connect 超时 5 秒，连不上要能重试。

> 电脑启动时会打印三样东西：`本机 IP`、`端口`、`配对码`。手机 App 的界面要能让用户**手动填 IP 和配对码，填一次就记住**（换网络后 IP 会变，所以必须可改）。

---

## 2. 报文格式（最重要，照着写）

每条消息 = **4 字节长度 + JSON 正文**：

```
┌────────────────────┬──────────────────────────┐
│ 4 字节长度（大端）  │  N 字节 UTF-8 JSON 正文    │
└────────────────────┴──────────────────────────┘
```

- 长度字段：**无符号 32 位大端整数**，只表示 JSON 正文的字节数，**不含**自身 4 字节。
- 正文：UTF-8 编码的 JSON **对象**（不能是数组或裸字符串）。
- 最大长度：65535 字节，超过会被电脑断开。

**为什么带长度**：TCP 是字节流，两次 send 可能被合成一次收到、一次 send 也可能分几次收到（粘包/拆包）。先读 4 字节拿到长度，才知道正文该读多少字节。发送时：先算 `json 字符串的 UTF-8 字节数 N`，先发 4 字节的 N，再发正文。

**公共字段**：所有消息都带这三个，缺一个电脑可能直接忽略或报错：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 消息类型，见第 3、4 节 |
| `v` | int | 协议版本，固定 `1` |
| `ts` | int | 发送时刻的 Unix **毫秒**时间戳，只用于日志对齐，不参与任何决策 |

---

## 3. 手机要发的消息（手机 → 电脑）

### 3.1 `hello` —— 连上后的第一条（只发一次）

```json
{"type":"hello","v":1,"ts":1731111111123,"device_id":"a1b2c3...","device_name":"Pixel 7","pair_code":"123456","app_ver":"1.0"}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | string | 手机首次启动生成并持久化的 UUID，用来在日志里区分设备 |
| `device_name` | string | 手机型号，只显示用 |
| `pair_code` | string | 用户输入的 6 位数字配对码 |
| `app_ver` | string | 手机 App 版本号 |

### 3.2 `phone_state` —— 上报播放状态（每秒发一次）

```json
{"type":"phone_state","v":1,"ts":1731111111123,"seq":42,"playing":true,"package":"com.netease.cloudmusic","app_name":"网易云音乐","state_code":3,"has_control":true}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `seq` | int | 自增序号，从 1 开始，用于发现丢包乱序 |
| `playing` | bool | 是否有"正在播放"的合规 App |
| `package` | string | 正在播放的包名；`playing=false` 时传空串 `""` |
| `app_name` | string | App 显示名，只日志/界面用 |
| `state_code` | int | Android `PlaybackState` 常量，见下方取值 |
| `has_control` | bool | 该 App 能否下发暂停（拿得到 `TransportControls`） |

`state_code` 取值（直接透传 Android 常量）：

| 值 | 含义 |
| --- | --- |
| 0 | NONE |
| 1 | STOPPED |
| 2 | PAUSED |
| 3 | PLAYING |
| 6 | BUFFERING |
| 7 | ERROR |

**上报节奏（必须遵守，否则电脑会误判）**：

1. 每 1 秒轮询一次 MediaSession，**状态与上次不同就立即发**。
2. **即使没变化，也每 1 秒发一次兜底**——电脑靠这个区分"手机没在播"和"手机掉线了"。
3. 限速：200ms 内最多发 1 条，避免某个 App 频繁抖动刷屏。
4. `playing=true` 的判定：`state_code == 3` 连续读到 2 次（约 2 秒）才认定在播，避免一秒内的提示音误触发。

### 3.3 `pause_ack` —— 执行完暂停后回执（收到一条 pause_cmd 就回一条）

```json
{"type":"pause_ack","v":1,"ts":1731111111123,"cmd_id":7,"ok":true,"state_code":2,"err":""}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cmd_id` | int | 原样回传 pause_cmd 里的值，用于配对 |
| `ok` | bool | 执行**并回读**后，该 App 是否确实已不在播放 |
| `state_code` | int | 回读到的 `PlaybackState` 常量 |
| `err` | string | 失败原因；成功时传空串（如 `NO_CONTROL` / `STILL_PLAYING` / `APP_GONE`） |

> **必须回读**：`pause()` 是"请求"不是"保证"，个别 App 会忽略或自动续播。回读后 `ok=false` 要如实写进 `err`，不能假装成功。

### 3.4 `pong` —— 收到 `ping` 后原样回时间戳

```json
{"type":"pong","v":1,"ts":1731111111123}
```

收到电脑的 `ping` 就回一条 `pong`，`ts` 回传 `ping` 里的值（电脑用来估往返时间）。

---

## 4. 电脑会发的消息（电脑 → 手机）

### 4.1 `hello_ok` —— 配对通过

```json
{"type":"hello_ok","v":1,"ts":1731111111123,"session_id":"session-1731111111123","server_name":"xiao","heartbeat_s":3}
```

| 字段 | 说明 |
| --- | --- |
| `session_id` | 本次连接的编号，日志用 |
| `server_name` | 电脑名，界面显示用 |
| `heartbeat_s` | 心跳周期秒数，固定 `3`，手机按它定时 ping/pong |

### 4.2 `hello_reject` —— 配对失败（随即被断开）

```json
{"type":"hello_reject","v":1,"ts":1731111111123,"code":"BAD_PAIR_CODE","msg":"配对码错误"}
```

| 字段 | 说明 |
| --- | --- |
| `code` | `BAD_PAIR_CODE`（配对码错）/ `BAD_VERSION`（版本不符）/ `BUSY`（已有别的手机连着） |
| `msg` | 给用户看的中文原因 |

收到后**不重试同一条连接**，把原因显示给用户，让用户重新填配对码 / 升级版本 / 等另一台断开。

### 4.3 `decision` —— 决策结果（只在"该谁播"变化时发）

```json
{"type":"decision","v":1,"ts":1731111111123,"mode":"phone","reason":"PHONE_HIGHER","pc_app":"chrome.exe","phone_app":"com.netease.cloudmusic","pc_priority":5,"phone_priority":20}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `pc`（电脑播）/ `phone`（手机播）/ `none`（两边都没播） |
| `reason` | 决策依据，见下表 |
| `pc_app` | 电脑侧参与比较的进程名（无则空串） |
| `phone_app` | 手机侧当前包名（无则空串） |
| `pc_priority` | 电脑侧优先级 |
| `phone_priority` | 手机侧优先级 |

`reason` 取值：`ONLY_PC` / `ONLY_PHONE`（只有一侧在播）、`PC_HIGHER` / `PHONE_HIGHER`（优先级高者胜）、`TIE_KEEP_CURRENT`（同分维持现状）、`NONE`（两边都没播）。

手机端拿它**只做显示**（状态页显示"当前由谁出声"），不要拿它做任何动作——真正的动作是下面这条 `pause_cmd`。

### 4.4 `pause_cmd` —— 暂停指令（电脑赢、手机还在播时发）

```json
{"type":"pause_cmd","v":1,"ts":1731111111123,"cmd_id":7,"package":"com.netease.cloudmusic","reason":"PC_WINS"}
```

| 字段 | 说明 |
| --- | --- |
| `cmd_id` | 电脑侧自增的命令号，回 `pause_ack` 时原样带回 |
| `package` | 要暂停的包名 |
| `reason` | 固定 `PC_WINS`，预留扩展 |

收到后：对该包名的 MediaSession 调 `transportControls.pause()`，回读状态，然后回 `pause_ack`。**同一个 `cmd_id` 只执行一次**（去重），重复收到仍要回 ack。

### 4.5 `ping` —— 心跳探活（每 3 秒）

```json
{"type":"ping","v":1,"ts":1731111111123}
```

收到就回 `pong`。

### 4.6 `error` —— 收到无法处理的消息

```json
{"type":"error","v":1,"ts":1731111111123,"code":"UNKNOWN_TYPE","msg":"未知消息类型：xxx"}
```

`code` 取值：`BAD_FRAME`（帧格式错）、`UNKNOWN_TYPE`、`NOT_AUTHED`（未配对就发业务消息）、`TOO_LARGE`（超长）。收到 `error` **不重连不重试**，记日志即可。

---

## 5. 一次完整流程（时序图）

### 5.1 建连与配对

```
手机                                      电脑
 │ TCP connect ──────────────────────────> │ 监听 0.0.0.0:8765
 │ hello{pair_code, device_id} ─────────> │ 校验配对码 / 版本
 │ <────────────────── hello_ok{session_id, heartbeat_s=3}
 │ 或
 │ <────────────────── hello_reject{code}  （随即断开）
```

### 5.2 正常运行（配对之后）

```
手机                                      电脑
 │ phone_state{playing, package} ───────> │ 每秒收，更新手机状态
 │ ping ───────────────────────────────> │
 │ <──────────────────────────────── pong │
 │ <──────────────────────── decision{...}  （只在 mode 变化时发）
 │ <──────────────────────── pause_cmd{...} （电脑赢且手机在播时）
 │ pause_ack{cmd_id, ok} ───────────────> │
```

### 5.3 超时与断线

| 事件 | 手机该怎么做 |
| --- | --- |
| 连续 6 秒没收到电脑任何报文 | 判定离线，关闭 socket，显示"未连接" |
| 程序退出 / 网络断了 | 关闭 socket |
| 重连 | 退避重试：`1s → 2s → 4s → 8s → 10s`（封顶 10s），成功后重新走 hello 配对 |

---

## 6. 必须遵守的规则清单（最容易踩的坑）

1. **`playing=false` 也要发 `phone_state`**：否则电脑分不清"没在播"和"掉线了"。
2. **包名统一小写**：`com.netease.cloudmusic`，不要大写。
3. **所有消息都带 `type`/`v`/`ts`**，`v` 固定 `1`。
4. **`pause_cmd` 按 `cmd_id` 去重**：同一条指令只执行一次，但重复收到也要回 ack。
5. **`ts` 不要用于任何逻辑**：两端时钟不同步，只写日志。
6. **中文直接放 JSON**（UTF-8），配合长度前缀不会有乱码。
7. **一个 App 可能有多个 MediaSession**：取 `state_code == PLAYING` 的那个上报；都暂停时取最近活动的。
8. **防火墙**：电脑首次运行要允许 `python.exe` 入站（专用网络），否则手机连不上（表现和 AP 隔离一样，先用浏览器访问 `http://电脑IP:8765` 验证网络通不通）。

---

## 7. 手机端硬性前提（Android 权限）

读 MediaSession 必须拿到 **通知使用权**，否则一个播放器都读不到。手机端要引导用户去系统设置里开启，并关闭该 App 的电池优化（否则后台监听会被杀）。Android 14 起前台服务还要声明服务类型（写服务时一并处理）。

---

## 8. 联调步骤（推荐顺序）

1. 电脑端先跑 `python -m tools.fake_phone`（在 `computer/` 目录、lanchat 环境），它是"假手机"，能模拟手机的全部行为。用假手机把电脑侧调通。
2. 再用真手机 App 替换假手机：连同一 WiFi → 填电脑 IP + 配对码 → 观察电脑日志里是否出现 `phone_state`。
3. 打开手机音乐/视频 App，看电脑日志是否上报、是否收到 `decision`；让电脑侧开一个更高优先级的播放器，看手机是否收到 `pause_cmd` 并真的暂停。
