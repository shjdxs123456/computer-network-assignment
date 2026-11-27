"""假手机脚本（调试用）。

作用：
    在电脑上模拟一台手机，连上主程序的 TCP 服务，完整走一遍协议流程：
    hello 配对 → 每秒上报 phone_state（在播某个包名）→ 接收 decision / pause_cmd
    → 执行"暂停"并回 pause_ack。用来在"没有真手机"的情况下，先把
    "决策 → 电脑让路 / 下发暂停"整条链路跑通（对应规则 P7）。

用法（在 computer/ 目录、lanchat 环境里执行）：

    python -m tools.fake_phone                          # 默认连 127.0.0.1:8765，报网易云包名
    python -m tools.fake_phone --package com.example.x  # 上报别的包名
    python -m tools.fake_phone --host 192.168.1.5       # 连另一台电脑

演示提示：
    - 默认包名 com.netease.cloudmusic 在 priority.json 里优先级是 20，比电脑侧都高，
      所以手机会一直"赢"，能看到电脑让路（暂停电脑播放器）。
    - 想看"电脑赢、下发 pause_cmd"：把 --package 填一个未配置的包名（如 com.example.x，
      优先级按 0 算），同时电脑侧开着已配置的播放器（如 chrome），电脑就会赢并下发暂停。
    - 收到 pause_cmd 后本脚本会假装暂停，--resume 秒后假装用户又点开播放，可看来回切换。

本脚本只模拟"手机这一侧"的行为，不碰电脑的音频，也不依赖 pycaw/winsdk。
"""
from __future__ import annotations

import argparse
import select
import socket
import time

from net.protocol import DEFAULT_PORT, PROTO_VER, build_msg, encode_msg, recv_frame


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="假手机：模拟手机端上报与收指令")
    parser.add_argument("--host", default="127.0.0.1", help="电脑 IP，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--package", default="com.netease.cloudmusic", help="上报的包名")
    parser.add_argument("--app-name", default="网易云音乐", help="app 显示名（仅日志）")
    parser.add_argument("--pair-code", default="123456", help="配对码，需与 priority.json 一致")
    parser.add_argument("--device-name", default="FakePhone", help="设备名（仅日志）")
    parser.add_argument("--resume", type=float, default=5.0, help="收到暂停后多少秒恢复播放，默认 5")
    return parser.parse_args()


def handle_msg(msg: dict, sock: socket.socket, args: argparse.Namespace) -> tuple[bool, float]:
    """处理服务端发来的一条消息，返回新的 (playing, resume_at)。

    playing：当前是否假装"在播"；resume_at：暂停后应恢复播放的时间戳（0 表示不自动恢复）。
    """
    msg_type = msg.get("type")

    if msg_type == "hello_ok":
        print(f"[配对成功] session={msg.get('session_id')} 服务端={msg.get('server_name')}")
    elif msg_type == "hello_reject":
        print(f"[配对被拒] {msg.get('code')} {msg.get('msg')}")
        # 配对失败时服务端会立即断开，这里直接结束进程。
        raise SystemExit(1)
    elif msg_type == "decision":
        print(
            f"[收到决策] mode={msg.get('mode')} reason={msg.get('reason')} "
            f"电脑={msg.get('pc_app') or '无'}({msg.get('pc_priority')}) "
            f"手机={msg.get('phone_app') or '无'}({msg.get('phone_priority')})"
        )
    elif msg_type == "pause_cmd":
        print(f"[收到暂停] cmd_id={msg.get('cmd_id')} 包名={msg.get('package')}")
        # 假装执行暂停，并回一个"确实已停"的回执（协议 M7，ok=true）。
        sock.sendall(
            encode_msg(
                build_msg(
                    "pause_ack",
                    cmd_id=msg.get("cmd_id"),
                    ok=True,
                    state_code=2,  # 2 = PAUSED
                    err="",
                )
            )
        )
        print("  → 已执行暂停并回 pause_ack")
        # 返回 playing=False 和"几秒后自动恢复播放"的时间点，用来演示来回切换。
        return False, time.time() + args.resume
    elif msg_type == "ping":
        # pong 原样回传 ts（协议 M8）。
        sock.sendall(encode_msg(build_msg("pong", ts=msg.get("ts"))))
    elif msg_type == "error":
        print(f"[收到错误] {msg.get('code')} {msg.get('msg')}")
    else:
        print(f"[忽略消息] type={msg_type!r}")

    return True, 0.0  # 除 pause_cmd 外，其余消息不改变 playing


def main() -> None:
    """连接服务端，进入"每秒上报 + 持续接收"的循环。"""
    args = parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=5)
    # 连上后第一条必须是 hello（协议 M1）。
    sock.sendall(
        encode_msg(
            build_msg(
                "hello",
                device_id="fake-phone-001",
                device_name=args.device_name,
                pair_code=args.pair_code,
                app_ver="1.0",
            )
        )
    )
    print(f"已连接 {args.host}:{args.port}，上报包名={args.package}，配对码={args.pair_code}")

    playing = True       # 初始假装"正在播放"
    resume_at = 0.0      # 0 表示没有待恢复
    last_send = 0.0      # 上次上报时间，用于控制 1 秒一条
    seq = 0              # 上报序号，自增（协议 M4 的 seq）

    while True:
        # 用 select 判断"有没有数据可读"，避免 recv 一直阻塞导致上报节奏被打乱。
        # 0.2 秒轮询一次，既能及时收消息，又不会占满 CPU。
        readable, _, _ = select.select([sock], [], [], 0.2)

        if readable:
            try:
                msg = recv_frame(sock)
            except (ConnectionError, OSError, ValueError) as exc:
                print(f"连接断开：{exc}")
                break
            playing, resume_at = handle_msg(msg, sock, args)

        # 到时间了就上报一次当前状态（协议 M4：状态变化立即发 + 每秒兜底发）。
        now = time.time()
        if now - last_send >= 1.0:
            seq += 1
            state_code = 3 if playing else 2  # 3=PLAYING，2=PAUSED
            sock.sendall(
                encode_msg(
                    build_msg(
                        "phone_state",
                        seq=seq,
                        playing=playing,
                        package=args.package if playing else "",
                        app_name=args.app_name,
                        state_code=state_code,
                        has_control=True,
                    )
                )
            )
            last_send = now

        # 模拟"用户又点开播放"：到了 resume_at 就恢复 playing。
        if not playing and resume_at and now >= resume_at:
            playing = True
            resume_at = 0.0
            print("[模拟] 用户重新开始播放")

    sock.close()
    print("已退出")


if __name__ == "__main__":
    main()
