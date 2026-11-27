"""音频会话控制模块（电脑端）。

作用：
    1.2 只能"看"，这个模块负责"动手"——把某个程序的音频会话静音。
    对应 target.md 里"如果决定手机播放，那么电脑静音相关的音频会话"这一步。

为什么要把"原始状态"记下来：
    如果用户原本就把某个程序静音了，我们调度结束后自作主张地"恢复成不静音"，
    就相当于篡改了用户的设置；更糟的是，如果程序崩溃没恢复，
    用户会遇到"电脑突然没声音，却不知道为什么"。
    所以第一次动某个进程之前，先记下它当时是不是静音，之后只恢复到自己记录的那个值。
"""

from __future__ import annotations

import comtypes

from pycaw.utils import AudioUtilities

# 进程号 -> 我们第一次动手之前它是否静音。
# 这是一个模块级变量（全局状态），刻意这么设计：
#   调用方不需要自己维护这张表，1.3 的"恢复"和后面 server 里的"退出恢复"都用同一份记录。
# 注意：进程退出后它的 pid 可能被系统复用给新进程，所以恢复时如果发现会话已经不存在，
# 就只能跳过——这是 PID 复用带来的天然限制，记录里会保留残余项，不影响功能。
_ORIGINAL_MUTED: dict[int, bool] = {}


def set_session_muted(pid: int, muted: bool) -> bool:
    """把指定进程的所有音频会话设为静音（或取消静音）。

    作用：
        按进程号找到该进程在默认播放设备上的全部音频会话，逐个设置静音开关。
        首次操作某个 pid 时，会先把它"原本是否静音"记进 _ORIGINAL_MUTED，
        供后续的恢复逻辑使用。

    入参：
        pid (int)：
            目标进程号。注意是进程号，不是进程名——
            同一个 exe 开了两个窗口就是两个 pid，需要分别处理。
        muted (bool)：
            True 表示静音；False 表示取消静音。

    返回（bool）：
        True  —— 至少成功设置了一个会话（该进程当前确实有音频会话）。
        False —— 没找到该进程的会话，或会话刚好在枚举与设置之间消失。
                 调用方应把 False 记进日志，不要当成"已经静音"。

    可能异常：
        本函数内部已对单个会话的 COM 异常做了兜底，正常情况下不向外抛异常。
        除非 pycaw 连默认播放设备都拿不到（比如拔掉了所有音频设备）。

    依赖：
        pycaw（AudioUtilities 枚举会话）、comtypes（捕获 COM 层错误）。
        运行在 conda 环境 lanchat 下。

    副作用：
        1. 真的会改变系统音量状态：把目标进程的会话静音或取消静音。
        2. 会写模块级变量 _ORIGINAL_MUTED（首次操作某 pid 时新增一条记录）。
        3. 不写文件、不发网络请求。
    """
    applied = False

    # 每次都重新枚举一次会话，而不是复用 1.2 的结果：
    # 因为 1.2 到调用这里之间可能隔了几百毫秒，会话可能已经消失或被重建。
    # 代价是每次多用几毫秒，换来的是"永远操作到当前真实存在的会话"。
    for session in AudioUtilities.GetAllSessions():
        if int(session.ProcessId) != pid:
            continue  # 不是目标进程，跳过

        volume = session.SimpleAudioVolume
        if volume is None:
            # 极少数系统会话没有音量接口，无法静音，跳过。
            continue

        try:
            # 只在该 pid 第一次被操作时记录原始值。
            # 用 not in 判断，保证后续多次静音/取消静音不会把"原始值"覆盖成我们改过的值。
            if pid not in _ORIGINAL_MUTED:
                _ORIGINAL_MUTED[pid] = bool(volume.GetMute())

            # SetMute 的第二个参数是"事件上下文 GUID"，用来让系统通知监听者是谁改的；
            # 我们不需要区分来源，传 None 即可。
            volume.SetMute(bool(muted), None)
            applied = True
        except comtypes.COMError:
            # 典型情况：会话在枚举之后、设置之前刚好结束（程序关闭了音频流）。
            # 这种失败不影响其他会话，也不值得中断整个调度，跳过即可。
            continue

    return applied


def restore_all() -> int:
    """把本次运行中改动过的所有静音状态恢复原样，并清空记录。

    作用：
        程序正常退出（Ctrl+C、关窗口）时调用一次，保证"我们改过的东西都还回去"，
        不会出现"跑了一次程序，电脑从此没声音"这种事故（设计决策 D2）。

    入参：
        无。

    返回（int）：
        成功恢复的进程数量。
        注意是"进程数"不是"会话数"：一个进程可能有多个音频会话，
        `set_session_muted` 内部会把它们一起处理，这里只按 pid 计数。
        返回 0 有两种可能：本次运行一个程序都没静音过，或每个 pid 的会话都已经消失。

    可能异常：
        正常不抛异常。单个 pid 处理时的任何异常都会被吞掉并继续，
        因为恢复流程必须"尽力而为"——不能因为一个进程失败就放弃其他进程。

    依赖：
        依赖本模块的 `set_session_muted()` 和 `_ORIGINAL_MUTED` 表。
        运行在 conda 环境 lanchat 下。

    副作用：
        1. 修改系统音量状态（把静音恢复成原样）。
        2. 清空模块级变量 `_ORIGINAL_MUTED`，所以本函数是幂等的：
           连续调用第二次会直接返回 0，不会重复操作。
        3. 不写文件、不发网络请求。
    """
    restored = 0

    # 用 list(...) 复制一份再遍历：循环体内部会间接使用 _ORIGINAL_MUTED，
    # 直接遍历原字典在"边遍历边改"时行为不可靠。
    for pid, original_muted in list(_ORIGINAL_MUTED.items()):
        try:
            # 恢复成"我们动手之前"的值，而不是无脑取消静音：
            # 用户原本就静音的程序，恢复后应该还是静音。
            if set_session_muted(pid, original_muted):
                restored += 1
        except Exception:
            # 设备被拔掉、COM 组件异常等极端情况：跳过这一条，继续恢复下一条。
            continue

    # 无论恢复成功与否都清空：
    # 进程已经退出的 pid 没有恢复的意义，留着只会让下次退出时再试一遍。
    _ORIGINAL_MUTED.clear()
    return restored
