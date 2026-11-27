"""程序入口（电脑端）。

作用：
    把前面写好的各个模块串起来，跑出一个完整的调度循环：
    1. 读配置（priority.json）；
    2. 启动网络服务线程（TCP 等手机连）+ UDP 自动发现服务（帮手机找到本机 IP）；
    3. 打印本机 IP + 端口 + 配对码，方便手机填写；
    4. 主调度循环：每秒枚举电脑音频会话 → 读手机状态 → 决策 → 执行；
    5. 正常退出（Ctrl+C）时恢复所有静音、关闭网络与发现服务。

线程分工（对应选型"双线程"）：
    - 网络线程（net/server.py）：专心等手机、收发消息、维护手机状态。
    - 发现线程（net/discovery.py）：UDP 广播自动发现，帮手机找到本机 IP。
    - 主线程（本文件）：每秒做一次"看电脑在播什么 → 决策 → 执行"，不被网络阻塞。

本文件包含：
    print_banner()       打印启动信息（IP、端口、配对码、热点操作提示）
    pause_pc_playback()  让电脑侧正在出声的播放器暂停（决策为 phone 时的动作）
    apply_decision()     根据决策结果执行实际动作（暂停电脑 / 下发手机暂停 / 恢复）
    main()               入口：串起整个调度循环
"""
from __future__ import annotations

import logging
import time

from core.config_loader import load_config
from core.decision import decide
from core.media_control import pause_app_playback
from core.session_control import restore_all
from core.wasapi_sessions import list_active_sessions
from net.discovery import DiscoveryService
from net.netutil import get_all_local_ips, get_local_ip
from net.server import PhoneServer

log = logging.getLogger("main")


def print_banner(config: dict) -> None:
    """打印启动信息：本机 IP、端口、配对码，以及热点连接的操作提示。"""
    recommended = get_local_ip()
    all_ips = get_all_local_ips()
    print("=" * 56)
    print("蓝牙耳机跨设备智能调度系统 —— 电脑端")
    print(f"  推荐 IP（一般填这个）：{recommended}")
    if all_ips:
        print(f"  本机所有 IP：{', '.join(all_ips)}")
    print(f"  端口：{config['port']}        配对码：{config['pair_code']}")
    print("  【学校网络有 AP 隔离，用手机开热点连】")
    print("    1. 手机先开热点，电脑的 WiFi 连上手机热点")
    print("    2. 再启动本程序，手机端填「连热点后新出现的 IP」")
    print("       （通常是 192.168.43.x 这类热点网段）")
    print("    3. 仍连不上就用 ipconfig 看「无线局域网适配器」的 IPv4")
    print("  按 Ctrl+C 退出（会自动恢复所有静音）")
    print("=" * 56)


def pause_pc_playback() -> None:
    """让电脑侧正在出声的播放器暂停（真正断流，耳机才会切走）。

    设计说明（对应用户选型"落选只做暂停，静音兜底留空"）：
        只做 SMTC 暂停，不做静音。因为实测"只静音不释放音频流，耳机不会切走"，
        必须让播放器真正停下来。没注册 SMTC 的程序（壁纸引擎、游戏等）暂停会自动
        失败，天然被跳过——这就是"静音兜底留空"：暂时不对它们做任何处理。

    遍历当前所有活跃会话，对每个拿得到名字的进程尝试暂停。
    注意：活跃会话多时，逐个 pause_app_playback 会各自请求一次 SMTC 会话管理器，
    略有开销；但本函数只在"决策变为 phone"时调用一次，不是每秒都跑，可接受。
    """
    for session in list_active_sessions():
        app = session.get("app", "")
        if not app:
            continue  # 拿不到进程名的会话（系统进程等），跳过
        # SMTC 的 app 标识是 AUMID（如 "MSEdge"），不带 .exe 后缀，所以去掉后缀再匹配。
        # 若个别播放器匹配不到，运行 python -m core.media_control 打印真实 AUMID 后调关键字。
        keyword = app[:-4] if app.endswith(".exe") else app
        ok = pause_app_playback(keyword)
        if ok:
            log.info(f"让电脑 {app} 让路：已暂停")
        else:
            log.info(f"让电脑 {app} 让路：未匹配到 SMTC 会话（可能无 SMTC，静音兜底留空）")


def apply_decision(decision: dict, server: PhoneServer) -> None:
    """根据决策结果执行实际动作（只在 mode 发生变化时被调用）。

    入参：
        decision (dict)：decide() 的返回值。
        server (PhoneServer)：网络服务对象，用于把 decision / pause_cmd 发给手机。
    """
    mode = decision["mode"]

    if mode == "phone":
        # 手机胜出：电脑让路——暂停正在出声的播放器。
        pause_pc_playback()
        server.send_decision(decision)

    elif mode == "pc":
        # 电脑胜出：先恢复电脑侧静音（当前版本未做静音，此调用幂等，
        # 是给"未来加入静音兜底"预留的恢复点），再通知手机暂停。
        restore_all()
        server.send_decision(decision)
        phone = server.get_phone_state()
        # 手机确实还在播，才下发暂停命令（protocol.md M6）。
        if phone and phone.get("playing") and decision["phone_app"]:
            server.send_pause_cmd(decision["phone_app"])

    else:  # mode == "none"
        # 两边都没在播：恢复电脑侧静音，让电脑回到"没人抢占"的自然状态。
        restore_all()
        server.send_decision(decision)


def main() -> None:
    """入口：加载配置、启动服务、进入每秒一次的调度循环。"""
    # 日志格式带上时间戳和来源模块名，方便排查"什么时候发生了什么"。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()  # 配置读不出来会抛 ValueError 直接退出，属于预期行为
    server = PhoneServer(config["port"], config["pair_code"])
    server.start()
    # UDP 自动发现：手机广播 discover，本服务回 discover_ok（含 IP 和 TCP 端口），
    # 让手机不用手动输 IP。只在"手机开热点"这类无隔离的网络里有效。
    discovery = DiscoveryService(config["port"])
    discovery.start()
    print_banner(config)

    current_mode = "none"  # 当前"正在出声的一方"，初始为 none（都没在播）
    try:
        while True:
            time.sleep(1)

            # 1. 看电脑侧谁在出声（可能因设备被拔等异常失败，失败就按"没会话"处理）
            try:
                pc_sessions = list_active_sessions()
            except Exception as exc:
                log.warning(f"枚举电脑音频会话失败：{exc}")
                pc_sessions = []

            # 2. 读手机当前状态（未连接为 None）
            phone = server.get_phone_state()

            # 3. 纯函数决策
            decision = decide(phone, pc_sessions, config["rules"], current_mode)

            # 4. 只在 mode 变化时才执行动作和发 decision（protocol.md M5：变化才发）
            if decision["mode"] != current_mode:
                log.info(
                    f"决策变化：{current_mode} -> {decision['mode']}（{decision['reason']}）"
                    f" 电脑={decision['pc_app'] or '无'}({decision['pc_priority']})"
                    f" 手机={decision['phone_app'] or '无'}({decision['phone_priority']})"
                )
                apply_decision(decision, server)
                current_mode = decision["mode"]

    except KeyboardInterrupt:
        log.info("收到退出信号，正在恢复……")
    finally:
        # 无论正常退出还是 Ctrl+C，都要恢复我们改过的静音（设计决策 D2），
        # 否则电脑可能被永久静音。
        server.stop()
        discovery.stop()
        restored = restore_all()
        if restored:
            log.info(f"已恢复 {restored} 个程序的静音状态")
        log.info("已退出")


if __name__ == "__main__":
    main()
