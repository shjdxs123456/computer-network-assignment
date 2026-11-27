"""WASAPI 音频会话枚举模块（电脑端）。

作用：
    告诉上层"电脑上现在有哪些程序正在出声"。
    这是电脑侧的"眼睛"，对应手机侧的 MediaSession 监听。

底层原理：
    Windows 用 WASAPI（Windows Audio Session API）管理所有播放声音的程序。
    每一个正在出声的程序，在系统里都对应一个"音频会话"（Audio Session）。
    我们通过 pycaw 这个库去问系统要会话列表，读出：进程号、进程名、
    会话状态（活跃/空闲/已结束）、静音状态、音量。

    注意：这里枚举的是"默认播放设备"上的会话。耳机连上电脑并成为默认设备后，
    枚举到的就是耳机上的声音；如果声音还留在扬声器上，这里看不出来区别。
"""

from __future__ import annotations

from pycaw.constants import AudioSessionState
from pycaw.utils import AudioUtilities


def list_active_sessions(include_inactive: bool = False) -> list[dict]:
    """枚举默认输出设备上的音频会话，返回每个会话的进程名与播放/静音状态。

    作用：
        给 1.4 的决策函数提供"电脑侧谁在播"的输入；给 1.3 提供待静音的对象。

    入参：
        include_inactive (bool)：
            False（默认）只返回正在出声的会话（State == Active）。
            True 连空闲会话一起返回，调试时用来看"系统到底认识哪些程序"。

    返回（list[dict]）：每个元素形如
        {
          "pid":    17696,             # 进程号，int
          "app":    "chrome.exe",      # 进程名，已 strip + 转小写；取不到时为空串
          "active": True,              # 是否正在出声（State == Active）
          "muted":  False,             # 该会话当前是否被静音
          "volume": 1.0,               # 该会话音量，0.0 ~ 1.0 的浮点数
        }
        列表按 app 名字排序，保证同样内容每次输出顺序一致，便于对比日志。

    可能异常：
        - 系统没有可用的播放设备时，pycaw 内部会抛异常（COMError 等），本函数不吞掉，
          交给调用方决定怎么提示用户。
        - 单个会话读进程名可能因权限失败（系统进程），这种情况已被 try/except 挡掉，
          只会让该条 app 变成空串，不影响其他会话。

    依赖：
        pycaw（封装 WASAPI/COM，自带 psutil 用于取进程名）、comtypes（pycaw 的底层依赖）。
        需要 conada 环境 lanchat。

    副作用：
        只读系统状态，不改音量、不静音、不写文件、不发网络请求。
        每调用一次都会重新问系统要一遍会话列表，所以不要放在过紧的循环里
        （1 秒一次足够）。
    """
    # GetAllSessions() 内部会做三件事：拿到默认播放设备 → 打开它的会话管理器 → 遍历会话。
    # 每调用一次都是全新的一次枚举，反映"调用这一刻"的真实状态。
    sessions = AudioUtilities.GetAllSessions()

    result: list[dict] = []
    for session in sessions:
        # session.State 是 int（0=Inactive 空闲, 1=Active 正在出声, 2=Expired 已结束）。
        # 转成枚举再比较，比直接写数字可读性好得多。
        state = AudioSessionState(session.State)

        if not include_inactive and state != AudioSessionState.Active:
            continue

        # session.Process 是一个 psutil.Process 对象；取不到时（比如系统声音、
        # 进程刚好退出）是 None。名字可能重复调用，这里只取一次。
        app = ""
        try:
            if session.Process is not None:
                app = session.Process.name()
        except Exception:
            # 典型情况：权限不足（AccessDenied）、进程已退出（NoSuchProcess）。
            # 这类会话我们仍然保留，只是名字为空，方便日志里看到"有个拿不到名字的会话"。
            app = ""
        # 与 config_loader 保持一致：去空格 + 转小写，这样 1.4 用配置里的规则能直接比对上。
        app = app.strip().lower()

        # SimpleAudioVolume 是控制该会话音量/静音的接口。极少数系统会话可能没有，
        # 拿不到就当成"未静音、音量 1.0"，避免整个函数因为一条异常数据崩掉。
        muted = False
        volume = 1.0
        if session.SimpleAudioVolume is not None:
            muted = bool(session.SimpleAudioVolume.GetMute())
            volume = float(session.SimpleAudioVolume.GetMasterVolume())

        result.append(
            {
                "pid": int(session.ProcessId),
                "app": app,
                "active": state == AudioSessionState.Active,
                "muted": muted,
                "volume": round(volume, 3),
            }
        )

    # 排序只是为了日志好读：同样的会话集合，每次打印的顺序都一样。
    result.sort(key=lambda item: (item["app"], item["pid"]))
    return result


"""if __name__ == "__main__":
    # 直接运行本文件即可自测：python -m core.wasapi_sessions
    # （在 computer/ 目录下执行；如果只想看正在出声的，不加参数即可）
    for row in list_active_sessions(include_inactive=True):
        print(row)"""
