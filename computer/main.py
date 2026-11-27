"""程序入口（电脑端）。

作用：
    把前面写好的各个模块串起来，跑出一个完整的调度循环：
    1. 读配置（priority.json）；
    2. 启动网络服务线程（等手机连）；
    3. 打印本机 IP + 端口 + 配对码，方便手机填写；
    4. 主调度循环：每秒枚举电脑音频会话 → 读手机状态 → 决策 → 执行；
    5. 正常退出（Ctrl+C）时恢复所有静音、关闭网络服务。

线程分工（对应选型"双线程"）：
    - 网络线程（net/server.py）：专心等手机、收发消息、维护手机状态。
    - 主线程（本文件）：每秒做一次"看电脑在播什么 → 决策 → 执行"，不被网络阻塞。

本文件包含：
    get_local_ip()       估算本机在 WiFi 里的 IP（给手机填）
    print_banner()       打印启动信息
    pause_pc_playback()  让电脑侧正在出声的播放器暂停（决策为 phone 时的动作）
    apply_decision()     根据决策结果执行实际动作（暂停电脑 / 下发手机暂停 / 恢复）
    main()               入口：串起整个调度循环
"""
from __future__ import annotations

import logging
import socket
import time

from core.config_loader import load_config
from core.decision import decide
from core.media_control import pause_app_playback
from core.session_control import restore_all
from core.wasapi_sessions import list_active_sessions
from net.server import PhoneServer

log = logging.getLogger("main")


def get_local_ip() -> str:
    """估算本机在局域网里的 IPv4 地址，返回给手机填写。

    原理：
        创建一个 UDP socket 并"假装"要连外部地址，系统会替我们选定一个出口网卡，
        读 socket 的本地地址就是这个网卡的 IP。UDP 不会真的发包，所以即便断网也能拿到。
        这是获取"对外 IP"的常见技巧，比 gethostbyname(gethostname()) 更不容易拿到 127.0.0.1。

    返回（str）：
        本机 IP；失败时返回 "127.0.0.1"（此时请用户用 ipconfig 自查）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # 只用于选出口网卡，不真的连
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def print_banner(config: dict) -> None:
    """打印启动信息：本机 IP、端口、配对码。"""
    ip = get_local_ip()
    print("=" * 56)
    print("蓝牙耳机跨设备智能调度系统 —— 电脑端")
    print(f"  本机 IP（手机端填这个）：{ip}")
    print(f"  端口：{config['port']}        配对码：{config['pair_code']}")
    print("  若手机连不上：用 ipconfig 确认无线网卡 IP，并放行防火墙")
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
        restored = restore_all()
        if restored:
            log.info(f"已恢复 {restored} 个程序的静音状态")
        log.info("已退出")


if __name__ == "__main__":
    main()
