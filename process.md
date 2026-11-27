# 项目进度

## 当前目标
- 完成「蓝牙耳机跨设备智能调度系统」：手机监听 MediaSession 播放状态，电脑监听本机 WASAPI 音频会话，双方通过局域网同步"谁想播什么"，由电脑侧统一的优先级表决定当前由哪一侧出声，落选方静音 / 暂停，使双设备多点蓝牙耳机跟随实际出声的一侧。

## 任务拆分清单
- [x] 阶段 0：通信协议设计（消息字段表 + 时序），不写代码 → `computer/net/protocol.md`
- [ ] 阶段 1：电脑端（先做到脱离耳机也能验证）
  - [x] 1.1 项目骨架 + JSON 配置/优先级表读取函数 → `computer/core/config_loader.py::load_config()`
  - [x] 1.2 WASAPI 枚举当前正在播放的音频会话 → `computer/core/wasapi_sessions.py::list_active_sessions()`
  - [x] 1.3 单个音频会话的静音 / 恢复（含原状态保存）→ `session_control.py::set_session_muted()` + `restore_all()`
  - [x] 1.4 纯函数：根据两侧播放意图 + 优先级表输出决策结果 → `computer/core/decision.py::decide()`
  - [x] 1.5 TCP 服务端：接受手机连接、心跳、断线重连 → `computer/net/server.py::PhoneServer`（配合 `net/protocol.py`、入口 `main.py`、调试 `tools/fake_phone.py`）
  - [x] 1.6 电脑侧真正停流：用 SMTC 暂停播放器 → `computer/core/media_control.py::pause_app_playback()`
- [ ] 阶段 2：手机端
  - [ ] 2.1 前台服务骨架 + 常驻通知（保证后台存活）
  - [ ] 2.2 通知使用权 → 获取活跃 MediaSession 列表
  - [ ] 2.3 上报当前播放包名（状态变化时上报）
  - [ ] 2.4 接收电脑指令 → 发送 MediaSession 暂停
  - [ ] 2.5 最小状态页（连接状态 / 当前决策）
- [ ] 阶段 3：联调与耳机实测
  - [ ] 3.1 无耳机联调（用电脑扬声器验证静音与恢复正确）
  - [ ] 3.2 接真蓝牙耳机验证切换行为并记录实测结论
  - [ ] 3.3 防抖、恢复、异常路径（断连 / 指令失败）
- [ ] 阶段 4：交付（协议说明、运行说明、演示与报告材料）
- [ ] 待完成（有余力再做，非首版范围）：电脑端 GUI 优先级表配置表格，替代手改 JSON

## 已完成
- 函数/方法：`decide()`
  - 文件位置：`computer/core/decision.py`
  - 作用：纯函数，输入"手机播放意图 + 电脑活跃会话列表 + 优先级表 + 当前模式"，输出 `mode`/`reason`/两侧参与比较的 app 与优先级
  - 关键决策：优先级表转 dict 查表（O(1)）；电脑侧取"命中最高的那条规则"；平局(P2)维持当前模式，当前模式无效时默认维持电脑（本机优先，暂停手机比暂停电脑代价低）
  - 自测结论：9 组假数据断言全过（ONLY_PC/ONLY_PHONE/PC_HIGHER/PHONE_HIGHER/TIE_KEEP_CURRENT/NONE/未配置=0/平局维持/平局默认pc）
- 函数/方法：`protocol.py`（`build_msg`/`encode_msg`/`recv_frame` + 常量）
  - 文件位置：`computer/net/protocol.py`
  - 作用：按 protocol.md 实现 4 字节大端长度前缀 + UTF-8 JSON 的编解码；`recv_frame` 处理粘包/拆包
  - 自测结论：单条往返、粘包拆分、拆包重组、长度前缀、中文保留 5 项全过（用 socketpair 模拟）
- 函数/方法：`PhoneServer`（TCP 服务端类）
  - 文件位置：`computer/net/server.py`
  - 作用：独立线程监听 8765；hello 配对校验(pair_code/v)；收 phone_state/pause_ack/ping 并更新共享手机状态；主线程经 `send_decision`/`send_pause_cmd` 下发；心跳 3 秒 ping、6 秒无响应判离线；只允许一个连接(BUSY)
  - 关键决策：`_phone`/`_conn` 加锁保护跨线程访问；`get_phone_state` 返回副本；服务端主动发 ping 探活
- 函数/方法：`main.py`（入口 + 调度循环）
  - 文件位置：`computer/main.py`
  - 作用：读配置 → 起网络线程 → 打印本机 IP/端口/配对码 → 每秒"枚举 WASAPI→读手机→decide→变化时执行"→退出恢复静音
  - 关键决策：双线程（网络线程 + 主调度循环）；只在 mode 变化时执行动作并发 decision；`pause_pc_playback()` 只暂停不静音（静音兜底留空）
- 函数/方法：`fake_phone.py`（假手机调试脚本）
  - 文件位置：`computer/tools/fake_phone.py`
  - 作用：模拟手机完整走协议：hello→上报 phone_state→收 decision/pause_cmd→回 pause_ack→自动恢复播放，用于脱离真手机联调
  - 自测结论：与 PhoneServer 联调全链路通过（配对→上报→decision/pause_cmd 下发→pause_ack 回执→断线）
- 函数/方法：`restore_all()`
  - 文件位置：`computer/core/session_control.py`
  - 作用：遍历 `_ORIGINAL_MUTED`，把每个 pid 恢复成原始静音状态，随后清空记录；单个 pid 失败不影响其他
  - 关键决策：用 `list(...)` 复制后遍历；恢复值取"原始值"而非无脑取消静音；无论成败都清空，保证幂等
  - 自测结论：静音 wallpaper64 → `restore_all()` 返回 1、muted 回到 False、记录清空；第二次调用返回 0
- 函数/方法：`pause_app_playback()`
  - 文件位置：`computer/core/media_control.py`
  - 作用：通过 WinRT SMTC 找到匹配 app 标识的媒体会话，若正在播放则调用 `try_pause_async()`，让播放器真正停止、释放蓝牙音频流
  - 关键决策：用"包含匹配 + 不区分大小写"去对上 AUMID（如 Edge 是 `MSEdge`）与 WASAPI 的 exe 名；已在播放才发暂停；原本已暂停直接返回 True；不自动续播（用户已确认）
  - 新依赖：`winsdk 1.0.0b10`（已装入 lanchat 环境）
  - 自测结论：列出 1 个会话 `MSEdge`（PAUSED，标题正常）；`pause_app_playback("edge")` 返回 True；不存在的关键字返回 False
  - 已知边界：未注册 SMTC 的播放器控制不到；注意 `asyncio.run` 不能在已有事件循环中调用（本项目为同步调用，不受影响）
- 函数/方法：`set_session_muted()`
  - 文件位置：`computer/core/session_control.py`
  - 作用：按 pid 把该进程所有音频会话静音/取消静音，并在首次操作时记录原始静音状态
  - 关键决策：每次调用重新枚举会话（避免操作到已失效的会话句柄）；原始状态只在首次操作时写入模块级 `_ORIGINAL_MUTED`，不被后续调用覆盖；`SetMute` 的 eventContext 传 None；单会话 COMError 吞掉继续，不影响其他会话
  - 自测结论：对 wallpaper64.exe 静音 → 读回 muted=True；取消静音 → muted=False；不存在的 pid 返回 False；原始状态记录为 {27840: False}
  - 已知限制：进程退出后 pid 可能被复用；会话被程序重建后静音状态可能丢失（阶段 3 观察）
- 函数/方法：`list_active_sessions()`
  - 文件位置：`computer/core/wasapi_sessions.py`
  - 作用：用 pycaw 枚举默认输出设备上的音频会话，返回 pid / app（小写）/ active / muted / volume
  - 关键决策：只返回 `AudioSessionState.Active` 的会话（可通过参数放开）；单个会话取进程名失败只让 app 为空串，不中断整体；结果按 app 排序保证输出稳定
  - 运行环境：conda 环境 `lanchat`（`D:\software\anaconda\envs\lanchat\python.exe`），pycaw 20260927 / comtypes 1.4.17
  - 自测结论：枚举到 6 个会话，其中 `wallpaper64.exe` 为 Active（Wallpaper Engine 一直在出声），符合预期
  - 已知边界：只覆盖默认输出设备；壁纸类常驻程序会长期处于 Active，可能被当成"电脑在播"
- 函数/方法：`load_config()`
  - 文件位置：`computer/core/config_loader.py`
  - 作用：读取 `config/priority.json`，校验 server 段与 rules 段，返回标准化后的配置字典（app 名统一小写）
  - 关键决策：错误全部用中文 ValueError 抛出并指明"第几条规则/哪个字段"；不引入任何第三方库；用 utf-8-sig 兼容记事本 BOM；用 `(device, app)` 去重
  - 配套数据文件：`computer/config/priority.json`（4 条示例规则）
  - 自测结论：正常配置 1 例 + 失败场景 7 例（缺 server / 配对码 5 位 / priority 为字符串 / device 写错 / 大小写不同的重复规则 / JSON 语法错 / 端口越界）全部符合预期
- 需求评审：已向用户列出 10 项模糊点、9 项风险、2 套备选架构。
- 已确认 4 项关键选型：电脑主控 / Python + pycaw / 只静音 + 让播放器暂停 / 优先级表用 JSON（GUI 列为待完成）。
- 已建立计划书 PLAD.md：目录结构、环境准备、开发顺序与验收标准。

## 本轮完成
- 一次性完成电脑端剩余全部逻辑：决策纯函数 `decide()`（1.4）、协议编解码 `protocol.py`、TCP 服务端 `PhoneServer`（1.5）、入口 `main.py`、调试脚本 `fake_phone.py`、依赖清单 `requirements.txt`。
- 三项架构选型已由用户确认：① 落选（手机播）时电脑侧**只暂停不静音**（静音兜底留空）；② mode=none 时**恢复静音**；③ **双线程**结构。
- 验证全部通过：decide 9 组假数据、protocol 5 项编解码、PhoneServer 与 fake_phone 全链路联调、main.py 正常启动并进入调度循环。

## 下一步计划
- 手机端（阶段 2）由另一位同学负责，按 `computer/net/protocol.md` 的字段表实现：`hello` 配对、`phone_state` 上报、`decision`/`pause_cmd` 接收、`pause_ack` 回执、`ping/pong` 心跳。
- 电脑端与手机端联调时，用 `tools/fake_phone.py` 先模拟手机对协议、再用真机替换；`main.py` 启动时会打印本机 IP 和配对码供手机填写。

## 已知问题 / 待确认
- **实测结论（2026-11-11）**：Edge 播视频时仅静音，耳机**不会**切到手机；必须把视频**暂停**（音频流真正停止）耳机才切走。证实"耳机按活跃音频流判断"，且 `SetMute` 不释放流。→ 已补任务 1.6（SMTC 暂停，D12）。
- P1–P8 已全部确认（见 D11），无遗留待定项。
- target.md 提到的「静音 → 电脑切 HFP → 手机连耳机 → 再切回」兜底策略**未实现**，D7 已确认首版跳过。技术上 Windows 无公开 API 精确切换蓝牙 profile，只能靠 PnP 禁用/启用端点（需管理员、设备会短暂消失、且可能把耳机钉在通话模式）。是否补回待用户决定。
- 待实测：用户的蓝牙耳机是否支持双设备（多点）连接与自动切换（阶段 3 验证）。
- 原理备注：`ISimpleAudioVolume.SetMute` 只是把采样值变成 0，播放器仍在解码、WASAPI 仍向蓝牙端点送包，A2DP 链路依然"活跃"；只有当所有流都停止，端点才会释放。
- **游戏场景的限制**：游戏基本不注册 SMTC，无法被外部暂停；WASAPI 只能静音（不释放流），所以"电脑在打游戏 + 想让耳机切给手机"目前无解。独占模式（WASAPI Exclusive）下连静音都可能失效。手机侧游戏同样读不到（无 MediaSession，MixedAudio 已跳过）。候选兜底是"切换默认输出设备 + 静音新端点"，尚未实现。

## 设计决策记录
- D1（原则）手机上报的是"用户意图播放"而不是"当前是否真的在出声"。电脑静音后不能反推成"用户不想播"，否则会出现决策来回震荡。
- D2（原则）所有静音操作必须先保存原始音量/静音状态，程序正常退出时恢复，避免电脑被永久静音。（P5 已确认：断线时不做额外动作，只停止调度）
- D3（原则）首版跳过 MixedAudio（Shizuku），只用 MediaSession 识别手机播放。
- D4（待定）通信协议优先考虑自定义长度前缀 JSON over TCP + 心跳，便于网络课讲解报文。
- D5（已确认）决策中心在电脑：电脑持有优先级表并做决策；手机上报包名，接收电脑下发的暂停指令。
- D6（已确认）电脑端技术栈 Python + pycaw（依赖 comtypes，用于调 Windows Core Audio 的 COM 接口）。
- D7（已确认）耳机切换先只做"静音 + 让播放器暂停"，把切换交给耳机自身的多点逻辑；HFP 兜底与停用端点强切都不做。
- D8（已确认）优先级表用 JSON 配置文件手工编辑；电脑端 GUI 表格列为"待完成"，不进首版范围。
- D9（已确认）通信：手机与电脑同一 WiFi；手机手动填写电脑 IP 并记住；电脑为 TCP 服务端（8765），自定义长度前缀 JSON 报文；hello 带 6 位配对码，校验失败即断开；3 秒心跳。
- D10（已确认）AP 隔离属校园网常态且无法关闭，应急顺序：手机开热点 → `adb reverse` USB 反向端口转发 → Windows 移动热点；因此手机端 IP 必须可改可保存。
- D11（已确认，P1–P8）优先级 = 设备 + 包名 + 数字，未列出为 0；同分不抢占；手机不设白名单（凡 MediaSession 可读且可暂停的 app 都上报，故配置中删除 phone_whitelist）；电脑全量枚举；断线/关机不降级；1 对 1；保留 fake_phone.py 调试；配置不热加载，改完重启。
- D12（已确认）电脑侧改用 WinRT SMTC 让播放器真正暂停（`winsdk`）。原因：实测仅静音不能让耳机切换。切换回电脑时**不自动续播**，只解除静音。
- D13（已确认）游戏场景不做处理：首版只保证"有 SMTC 注册的视频/音乐播放器"能被暂停；游戏只能静音、不足以让耳机切走，此限制写进交付说明。"切换默认输出设备"仅作为候选，不实现。
- D14（已确认）电脑端收尾三选型：① 落选（决策=手机播）时电脑侧**只做 SMTC 暂停、不做静音兜底**（静音兜底留空，后续需要再补）；② mode=none 时恢复电脑侧静音；③ 双线程结构（网络线程 + 主调度循环，每秒枚举一次 WASAPI）。
- D15（已确认）`decide()` 平局(P2)且当前模式无效时，默认维持电脑：电脑是本机、优先保证出声，且暂停手机（发一条 pause_cmd）比暂停电脑（动本机播放器）代价更低。
- D16（已确认）学校网络存在 AP 隔离（同一 WiFi 下设备互不可达），连接方案定为**手机开热点**：电脑连手机热点，手机填电脑在热点网段的新 IP。电脑端 `main.py` 改为打印"推荐 IP + 本机所有 IP"供对照挑选（`get_all_local_ips()`）；手机端 IP 输入框必须可改可保存。文档同步：PLAD.md 6.1 标注已选定、phone/README.md 1.1 新增热点操作步骤。
- D17（已确认）手机填 IP 太麻烦，新增**UDP 广播自动发现**：手机发 `discover` 到 `255.255.255.255:8766`，电脑单播回 `discover_ok`（含同网段 IP + TCP 端口）。发现端口 `DISCOVERY_PORT=8766`（protocol.py），`net/discovery.py` 用 `_pick_ip()` 按 /24 网段匹配本机 IP 回复；`main.py` 启动 `DiscoveryService` 线程。文档同步：protocol.md 2.1 节 + M10/M11 消息 + 第 8 节常量、phone/README.md 1.2 节 + 5.0 时序。
