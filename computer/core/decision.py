"""决策模块（纯函数）。

作用：
    这是整个系统的"大脑"：把"电脑侧谁在播"、"手机侧谁在播"、
    "用户自定义的优先级表"、"当前谁在出声"这四样东西算一遍，
    输出"现在该由谁出声"的结论（mode），以及支撑这个结论的理由和比较过程。

为什么必须是纯函数：
    它不读写文件、不发网络请求、不改音量、不依赖任何全局变量，
    同样的输入一定得到同样的输出。这样我们可以用"假数据"直接断言结果，
    不用真的开播放器、连手机就能验证决策逻辑对不对（对应 PLAD 1.4 的验收标准）。

本模块只包含一个函数 decide()：
    decide(phone_intent, pc_sessions, rules, current_mode) -> dict
"""
from __future__ import annotations


def decide(
    phone_intent: dict | None,
    pc_sessions: list[dict],
    rules: list[dict],
    current_mode: str = "none",
) -> dict:
    """根据两侧播放意图和优先级表，算出"现在该谁出声"。

    作用：
        把手机侧、电脑侧的播放意图，对照用户自定义的优先级表做比较，
        输出一个决策结果。它只"算"，不"做"——真正的静音/暂停动作由调用方执行。

    入参：
        phone_intent (dict | None)：
            手机当前状态，来自 server 的共享状态。为 None 表示"手机没连上"。
            字段约定（与 server.py 的共享状态一致）：
              {
                "playing":    bool,  # 是否正在播放（手机端按 state_code==PLAYING 判定）
                "package":    str,   # 正在播放的包名，如 "com.netease.cloudmusic"
                "app_name":   str,   # app 显示名，仅日志用，不参与决策
                "state_code": int,   # Android PlaybackState 常量，仅日志用
                "has_control":bool,  # 该 app 是否能下发暂停，仅日志用
              }
        pc_sessions (list[dict])：
            电脑侧"正在出声"的会话列表，来自 wasapi_sessions.list_active_sessions()。
            每个元素至少要有 "app"（进程名，小写）字段。
        rules (list[dict])：
            优先级表，来自 config_loader.load_config() 的返回里的 "rules"。
            每条 {"device": "pc"/"phone", "app": str, "priority": int}。
        current_mode (str)：
            当前"正在出声的一方"，取值 "pc" / "phone" / "none"。
            只在"两侧优先级相同"时用于"维持现状不抢占"（规则 P2）。

    返回（dict）：
        {
          "mode":           "pc" | "phone" | "none",  # 结论：该谁出声
          "reason":         str,  # 结论来源，取值见 protocol.md 的 M5 说明
          "pc_app":         str,  # 电脑侧参与比较的进程名（无则空串）
          "phone_app":      str,  # 手机侧参与比较的包名（无则空串）
          "pc_priority":    int,  # 电脑侧取到的最高优先级（无则 0）
          "phone_priority": int,  # 手机侧取到的优先级（无则 0）
        }

    reason 取值含义：
        ONLY_PC / ONLY_PHONE  只有一侧在播（或只有一侧命中规则）
        PC_HIGHER / PHONE_HIGHER  两侧都命中规则，优先级高者胜
        TIE_KEEP_CURRENT     两侧优先级相同，维持当前正在播的一方（P2）
        NONE                 两侧都没在播（或都没命中规则）

    可能异常：
        本函数是纯计算，正常情况下不抛异常。前提是入参结构正确：
        rules 必须是 load_config 校验过、app 已转小写的列表；若调用方传入
        未标准化的 rules（比如 app 带大写），会因查不到而把优先级当成 0，
        导致决策错误——所以务必使用 load_config 的输出。

    依赖：
        无第三方库，也不依赖本项目的其他模块（保持纯函数，方便单测）。

    副作用：
        无。不写文件、不发网络、不改音频状态、不改任何全局变量。
    """
    # ---------- 1. 把优先级表转成"查表用"的字典 ----------
    # 键是 (device, app)，值是 priority。列表每次线性查找也能写，但转成 dict
    # 后查找是 O(1)，且 decide 会被每秒调用一次，这个转换值得做。
    # load_config 已经保证 app 是小写、无重复，这里直接建表即可。
    lookup: dict[tuple[str, str], int] = {}
    for rule in rules:
        lookup[(rule["device"], rule["app"])] = rule["priority"]

    # ---------- 2. 电脑侧：在活跃会话里找"命中最高的那条规则" ----------
    # 电脑侧可能同时有多个程序在出声（浏览器 + 壁纸引擎 + 音乐……），
    # 我们只关心"优先级最高"的那个，它才是电脑侧最想播的东西。
    # 未在规则表里出现的进程（如壁纸引擎），按 P1 优先级为 0，不参与抢播。
    pc_app = ""
    pc_priority = 0
    for session in pc_sessions:
        app = session.get("app", "")
        # 空串表示"拿不到进程名"（系统进程/权限不足），跳过。
        if not app:
            continue
        priority = lookup.get(("pc", app), 0)
        # 严格大于才替换：同优先级时保留先遇到的那个，保证结果稳定
        # （pc_sessions 已按 app 排序，所以"先遇到"是确定的字典序）。
        if priority > pc_priority:
            pc_app = app
            pc_priority = priority

    # ---------- 3. 手机侧：读取"是否在播 + 播什么" ----------
    # 手机侧一次只上报一个"正在播放的 app"（协议 M4 约定）。
    # playing=False 或包名为空，都视为"手机没在播"。
    phone_app = ""
    phone_priority = 0
    if phone_intent and phone_intent.get("playing"):
        package = phone_intent.get("package", "")
        if package:
            phone_app = package
            phone_priority = lookup.get(("phone", package), 0)

    # ---------- 4. 根据两侧优先级做决策 ----------
    # 这里的分支覆盖了 protocol.md M5 的所有 reason 取值，顺序很重要：
    # 先判断"有没有人想播"，再比较谁更高，最后处理平局。
    if pc_priority == 0 and phone_priority == 0:
        # 两边都没命中规则（或都没在播）→ 无事发生。
        mode = "none"
        reason = "NONE"
    elif phone_priority == 0:
        # 只有电脑命中 → 电脑播。
        mode = "pc"
        reason = "ONLY_PC"
    elif pc_priority == 0:
        # 只有手机命中 → 手机播。
        mode = "phone"
        reason = "ONLY_PHONE"
    elif pc_priority > phone_priority:
        mode = "pc"
        reason = "PC_HIGHER"
    elif phone_priority > pc_priority:
        mode = "phone"
        reason = "PHONE_HIGHER"
    else:
        # 两侧优先级相同且都 > 0：P2 规定"同优先级不抢占，维持当前正在播的一方"。
        # 边界情况：current_mode 既不是 pc 也不是 phone（比如程序刚启动、两边
        # 恰好同时出现同优先级的播放）。此时没有"现状"可维持，默认维持电脑：
        # 电脑是本机，优先保证电脑出声，且暂停手机（发一条 pause_cmd）比
        # 暂停电脑（要动本机播放器）代价更低。
        if current_mode in ("pc", "phone"):
            mode = current_mode
        else:
            mode = "pc"
        reason = "TIE_KEEP_CURRENT"

    return {
        "mode": mode,
        "reason": reason,
        "pc_app": pc_app,
        "phone_app": phone_app,
        "pc_priority": pc_priority,
        "phone_priority": phone_priority,
    }
