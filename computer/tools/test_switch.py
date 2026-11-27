"""手动实测脚本：验证"电脑侧静音后，蓝牙耳机到底会不会切回手机"。

这是一个**调试工具**，不属于产品功能，也不会被主程序调用。

用法（在 computer/ 目录下、lanchat 环境里执行）：

    python -m tools.test_switch                     # 默认：静音 msedge，观察 15 秒
    python -m tools.test_switch msedge 30           # 指定程序关键字 + 观察秒数
    python -m tools.test_switch --all 20            # 静音所有正在出声的程序

测试步骤：
    1. 电脑上用 Edge 打开一个视频并播放（确保有声音）；
    2. 手机上同时开始播放音乐/视频，并确认耳机当前连在电脑这一侧；
    3. 运行本脚本，按提示回车；
    4. 盯着耳机听：声音有没有从电脑切到手机？
    5. 脚本到时间会自动恢复静音，再观察耳机有没有切回来。

脚本只能"静音"，不能替你判断耳机行为——耳机切没切是人工观察的结论。
脚本会每秒打印一次"默认输出设备名"和"仍然活跃的会话"，
用来判断静音之后电脑的音频流是不是真的停了。
"""

from __future__ import annotations

import argparse
import time

from pycaw.utils import AudioUtilities

from core.session_control import _ORIGINAL_MUTED, set_session_muted
from core.wasapi_sessions import list_active_sessions


def main() -> None:
    """按命令行参数执行一次"静音 → 观察 → 恢复"的完整流程。"""
    parser = argparse.ArgumentParser(description="实测：电脑静音后耳机是否切走")
    parser.add_argument("keyword", nargs="?", default="msedge", help="要静音的程序名关键字，默认 msedge")
    parser.add_argument("seconds", nargs="?", type=int, default=15, help="观察秒数，默认 15")
    parser.add_argument("--all", action="store_true", help="静音所有正在出声的程序，而不是按关键字挑")
    args = parser.parse_args()

    # 默认输出设备 = 电脑"当下往哪里放声音"。耳机连上并成为默认设备后，这里会显示耳机名字。
    device = AudioUtilities.GetSpeakers()
    print(f"默认输出设备：{device.FriendlyName}")

    sessions = list_active_sessions()
    if not sessions:
        print("当前没有任何程序在出声，请先在 Edge 里播放视频再运行。")
        return

    print("当前正在出声的程序：")
    for item in sessions:
        print(f"  - {item['app']} (pid={item['pid']}, muted={item['muted']})")

    # --all：把当前所有活跃会话都静音（更接近真实调度：落选的一侧整体闭嘴）。
    # 否则只挑名字里含关键字的那个（通常是 msedge）。
    targets = sessions if args.all else [s for s in sessions if args.keyword in s["app"]]
    if not targets:
        print(f"没找到名字里包含 “{args.keyword}” 的会话，可以用 --all 或换关键字。")
        return

    names = ", ".join(f"{t['app']}({t['pid']})" for t in targets)
    print(f"\n即将静音：{names}")
    input("请在手机上开始播放，然后按回车执行静音……")

    muted_pids = []
    try:
        for item in targets:
            ok = set_session_muted(item["pid"], True)
            print(f"  静音 {item['app']}({item['pid']}) → {'成功' if ok else '失败（会话已消失？）'}")
            if ok:
                muted_pids.append(item["pid"])

        print(f"\n开始观察 {args.seconds} 秒：请用耳朵判断耳机是否切到了手机。\n")
        for elapsed in range(1, args.seconds + 1):
            time.sleep(1)
            # 每秒重新问系统要一次状态：
            #   - 默认设备变了 → 说明音频链路发生了迁移
            #   - 目标会话仍然 active → 说明静音并没有让播放器停下来，流还在跑
            current_device = AudioUtilities.GetSpeakers().FriendlyName
            still_active = [s["app"] for s in list_active_sessions()]
            print(
                f"[{elapsed:2d}s] 默认设备={current_device} | 仍在出声={still_active or '（无）'}"
            )
    finally:
        # 无论中途是否出错（包括 Ctrl+C），都必须恢复，否则电脑会一直没声音。
        print("\n恢复静音状态……")
        for pid in muted_pids:
            # 恢复到"我们动手之前"的状态，而不是无脑取消静音：
            # 如果用户原本就把这个程序静音了，我们不应该替他打开。
            set_session_muted(pid, _ORIGINAL_MUTED.get(pid, False))
            print(f"  已恢复 pid={pid}（原始 muted={_ORIGINAL_MUTED.get(pid, False)}）")

    print("\n测试结束。请记录：")
    print("  1. 静音后耳机是否切到了手机？")
    print("  2. 恢复后耳机是否切回了电脑？")
    print("  3. 观察期间『仍在出声』列表里有没有别的程序（比如壁纸引擎），它会拖住电脑的音频流。")


if __name__ == "__main__":
    main()
