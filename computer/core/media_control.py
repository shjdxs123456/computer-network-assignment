"""播放器控制模块（电脑端，基于 WinRT SMTC）。

作用：
    让电脑上的播放器**真正暂停**，而不只是静音。

为什么需要它：
    实测证明，只把音频会话静音，播放器仍然在解码、WASAPI 仍然往蓝牙端点送包，
    耳机认为"电脑这边还在放"，于是不会切到手机。
    只有让播放器真的停下来（音频流断了），耳机才会切走。

底层原理：
    Windows 10 1809 之后提供 SMTC（System Media Transport Controls）：
    每个"愿意被系统媒体键控制"的播放器都会把自己的会话注册进来，
    系统据此在锁屏/音量浮层显示歌曲信息和控制按钮。
    winsdk 是微软官方 WinRT API 的 Python 绑定，我们通过它拿到会话列表，
    再对指定会话调用 try_pause_async()。

    这跟手机端用 MediaSession 读播放器、发暂停指令是同一套思路，只是平台不同。
    代价：没注册 SMTC 的播放器（少数老旧程序）控制不到，只能退回"静音"。
"""

from __future__ import annotations

import asyncio

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)


def pause_app_playback(app_keyword: str) -> bool:
    """确保指定播放器当前不在播放（必要时调用它的暂停）。

    作用：
        在 SMTC 会话列表里找到 app 标识匹配的会话，若它正在播放，就发暂停请求。
        这是"决定让手机播"时，让电脑真正释放蓝牙音频链路的必要动作。

    入参：
        app_keyword (str)：
            用来匹配 SMTC 会话的 app 标识的关键字，**不区分大小写，按包含关系匹配**。
            注意 SMTC 用的是 AUMID 风格的名字，和 WASAPI 的 exe 名不完全一样：
              - Edge        → "MSEdge"      （关键字用 "edge" 即可）
              - Chrome      → "Chrome"
              - 网易云音乐  → 形如 "cloudmusic.exe" 或一串包名，需实测确认
            所以调用方要用"能同时匹配到两者"的关键字，或者先用本文件的 __main__ 打印出来看。

    返回（bool）：
        True  —— 目标会话现在确实不在播放（原本已暂停，或暂停成功）。
                 注意：找不到会话时返回 False，不要当成"已经停了"。
        False —— 没找到匹配的会话，或暂停请求被系统拒绝（try_pause_async 返回 False）。

    可能异常：
        正常不抛异常。若在"已经有事件循环在跑"的环境里调用，asyncio.run 会抛 RuntimeError
        ——本项目的调用方都是同步代码，不会遇到。

    依赖：
        winsdk（WinRT 绑定）。运行在 conda 环境 lanchat 下。

    副作用：
        1. 会真的暂停用户的播放器（影响用户正在看/听的内容）。
        2. 每次调用都会临时创建一个 asyncio 事件循环并销毁。
        3. 不写文件、不发网络请求。
    """

    # WinRT 的接口全是 async 的，必须在一个事件循环里调用。
    # 这里把异步逻辑写成内嵌函数，外层用 asyncio.run 跑完就收，
    # 调用方就能像普通函数一样同步使用，不用关心 async/await。
    async def _pause_matching_session() -> bool:
        manager = await SessionManager.request_async()
        keyword = app_keyword.strip().lower()

        for session in manager.get_sessions():
            app_id = session.source_app_user_model_id or ""
            if keyword not in app_id.lower():
                continue  # 不是目标播放器

            status = session.get_playback_info().playback_status
            if status != PlaybackStatus.PLAYING:
                # 已经没在播（暂停/停止/关闭），目标已达成，不必再发一次暂停。
                # 这一步很重要：对已暂停的会话反复调 pause 没有意义，
                # 某些播放器还会因此弹出通知。
                return True

            # 返回 True 表示系统接受了暂停请求。
            # 注意它只是"请求已送达"，播放器真的停下来可能要几十毫秒，
            # 调用方如果需要确认，可以随后用 list_active_sessions() 复查。
            return bool(await session.try_pause_async())

        return False  # 一个都没匹配上

    return asyncio.run(_pause_matching_session())


"""if __name__ == "__main__":
    # 自测/排查用：先列出系统里所有 SMTC 会话，看清 app 标识长什么样。
    # 用法：python -m core.media_control            → 只列出会话
    #       python -m core.media_control edge       → 列出并尝试暂停包含 "edge" 的会话
    import sys

    async def _dump() -> None:
        manager = await SessionManager.request_async()
        sessions = manager.get_sessions()
        print(f"共 {len(sessions)} 个 SMTC 会话：")
        for s in sessions:
            info = s.get_playback_info()
            title = ""
            try:
                props = await s.try_get_media_properties_async()
                title = props.title
            except Exception:
                title = "（读取失败）"
            print(f"  - app_id={s.source_app_user_model_id!r} 状态={info.playback_status!r} 标题={title!r}")

    asyncio.run(_dump())
    if len(sys.argv) > 1:
        print("暂停结果：", pause_app_playback(sys.argv[1]))"""
