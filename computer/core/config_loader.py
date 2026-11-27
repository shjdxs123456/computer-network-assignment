"""配置加载模块。

这个模块只做一件事：把 ``computer/config/priority.json`` 读进内存，
并顺手做一次"体检"，保证后面所有模块拿到的配置一定是干净、可用的。

为什么单独拆一个模块：
    配置文件是用户手改的，最容易写错（少个逗号、把数字写成字符串）。
    如果让每个模块自己去读、自己去判断，一旦出错会到处崩、且报错看不懂。
    集中在这里做一次校验，错误信息就能精确指到"第几条规则、哪个字段"。
"""

# 让类型注解在旧一点的 Python（3.9）上也能写 str | None 这种新语法，
# 它不改变运行逻辑，只是让注解"延迟求值"。3.10 及以上可以省略这一行。
from __future__ import annotations

import json
from pathlib import Path

# 默认配置路径：以本文件位置为基准往上退两级，再进 config/priority.json。
# 用 __file__ 拼绝对路径，而不是写 "config/priority.json"，
# 是为了让程序在任何目录下启动都能找到配置文件（不依赖"当前工作目录"）。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "priority.json"
# .resolve()得到绝对路径

def load_config(path: str | Path | None = None) -> dict:
    """读取并校验优先级配置文件，返回整理好的配置字典。

    作用：
        把 priority.json 从磁盘读出来，做完整的格式与取值校验，
        并把容易写错的地方统一标准化（app 名去空格、转小写）。

    入参：
        path (str | Path | None)：
            配置文件路径。不传则使用 DEFAULT_CONFIG_PATH
            （即 computer/config/priority.json）。传相对路径时，
            相对的是"当前工作目录"。

    返回（dict）：
        {
          "port":      8765,                 # 监听端口，整数
          "pair_code": "123456",             # 配对码，6 位数字的字符串
          "rules": [                         # 已标准化的规则列表
              {"device": "pc", "app": "chrome.exe", "priority": 5},
              ...
          ],
          "path": "D:\\...\\priority.json"    # 实际读取的文件，写日志时有用
        }
        说明：rules 里 app 已 strip() + lower()，所以下游比较时可以直接
        用小写进程名/包名去查，不用每处都自己转一次。

    可能异常：
        ValueError：以下任一情况都会抛出，消息为中文且指明位置——
            1. 文件不存在 / 读不出来（权限、被占用等）；
            2. 不是合法 JSON（会带上出错的行号、列号）；
            3. 顶层不是对象；
            4. 缺少 server 段，或 port 不在 1024~65535；
            5. pair_code 不是 6 位数字字符串；
            6. rules 缺失 / 为空 / 某条不是对象；
            7. device 不是 "pc" 或 "phone"；
            8. app 是空字符串；
            9. priority 不是 >= 0 的整数（注意 true/false 也会被拦下）；
            10. 同一条 device + app 重复出现。

    依赖：
        Python 标准库 json、pathlib，不依赖任何第三方库。
        不依赖本项目的其他模块。

    副作用：
        只读磁盘，不写文件、不发网络请求、不改全局状态。
    """
    # ---------- 1. 定位文件 ----------
    if path is None:
        path = DEFAULT_CONFIG_PATH
    # expanduser 处理 ~ 开头的路径；resolve 转成绝对路径，报错时能给用户看得懂的完整路径。
    path = Path(path).expanduser().resolve()

    if not path.is_file():
        raise ValueError(f"配置文件不存在：{path}")

    try:
        # 用 utf-8-sig 而不是 utf-8：Windows 记事本另存时可能带 BOM 头，
        # 标准 json 解析遇到 BOM 会直接报错，utf-8-sig 会自动把 BOM 吃掉。
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"配置文件读取失败：{path}（{exc}）") from exc

    # ---------- 2. 解析 JSON ----------
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # 手写 JSON 最常见的错误是多余/缺失的逗号，把行列号直接报给用户最省事。
        raise ValueError(
            f"配置文件不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc

    # raise ... from exc 把原始异常信息保留下来，方便调试。
    # raise ValueError("配置文件解析失败") from exc

    if not isinstance(data, dict):
        raise ValueError('配置文件顶层必须是一个 JSON 对象（用 { } 包起来）')

    # ---------- 3. 校验 server 段 ----------
    server = data.get("server")
    if not isinstance(server, dict):
        raise ValueError('缺少 server 段，或 server 不是一个 JSON 对象')

    port = server.get("port", 8765)
    # 坑：Python 里 True/False 是 int 的子类，isinstance(True, int) 为 True。
    # 所以必须先用 isinstance(port, bool) 把布尔值单独挡掉，否则 "port": true 会被当成 1 通过类型检查。
    if isinstance(port, bool) or not isinstance(port, int) or not (1024 <= port <= 65535):
        raise ValueError(f"server.port 必须是 1024~65535 之间的整数，现在是：{port!r}")

    pair_code = server.get("pair_code")
    # isdigit() 保证"全是数字"；再加长度判断，避免 "12345" 或 "1234567" 混进来。
    if not isinstance(pair_code, str) or len(pair_code) != 6 or not pair_code.isdigit():
        raise ValueError(
            f'server.pair_code 必须是 6 位数字组成的字符串（要在 JSON 里加引号），现在是：{pair_code!r}'
        )

    # ---------- 4. 校验 rules 段 ----------
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules 必须是非空数组（形如 \"rules\": [ {…}, {…} ]）")

    normalized_rules = []
    seen = set()  # 用来发现重复规则；元素是 (device, app) 元组
    # enumerate(..., start=1) 让报错里的"第 N 条"和用户数数的习惯一致（从 1 开始）。
    for index, item in enumerate(rules, start=1):
        where = f"rules 第 {index} 条"

        if not isinstance(item, dict):
            raise ValueError(f'{where} 必须是对象，形如 {{"device":"pc","app":"chrome.exe","priority":5}}')

        device = item.get("device")
        # 允许用户写成 "PC"，但内部统一成小写，避免同一个东西两种写法。
        if isinstance(device, str):
            device = device.strip().lower()
        if device not in ("pc", "phone"):
            raise ValueError(f'{where} 的 device 只能是 "pc" 或 "phone"，现在是：{item.get("device")!r}')

        app = item.get("app")
        if not isinstance(app, str) or not app.strip():
            raise ValueError(f"{where} 的 app 必须是非空字符串")
        # 标准化：去首尾空格 + 转小写。
        # 原因：Windows 进程名大小写不稳定（NeteaseCloudMusic.exe / neteasecloudmusic.exe 都出现过），
        # 统一小写后，1.2 采集时也转小写，两边就能直接比。
        app = app.strip().lower()

        priority = item.get("priority")
        # 同样先挡 bool：否则 "priority": true 会被当成 1。
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ValueError(f"{where} 的 priority 必须是 >= 0 的整数，现在是：{priority!r}")

        key = (device, app)
        if key in seen:
            # 重复规则会让"取哪一条"变成不确定行为，直接报错比默默取后者安全。
            raise ValueError(f"{where} 和前面的规则重复：{device} / {app}")
        seen.add(key)

        normalized_rules.append({"device": device, "app": app, "priority": priority})

    # ---------- 5. 返回整理好的结果 ----------
    return {
        "port": port,
        "pair_code": pair_code,
        "rules": normalized_rules,
        "path": str(path),  # 记录实际读的是哪个文件，排查"改错配置"时非常有用
    }
