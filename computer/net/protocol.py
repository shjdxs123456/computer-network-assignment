"""通信协议实现（报文编解码 + 常量）。

作用：
    把 computer/net/protocol.md 里的约定变成可调用的代码，只做"格式"层面的事：
    - 常量：协议版本、最大帧长、心跳周期等（对应 protocol.md 第 8 节的数值约定）
    - 消息构造：自动补上 type / v / ts 三个公共字段
    - 编码：把一个 dict 变成"4 字节长度前缀 + UTF-8 JSON 正文"的字节串
    - 解码：从 socket 流里读出一帧，处理 TCP 粘包 / 拆包问题

为什么不把"连接、心跳、状态机"放这里：
    那些属于"连接管理"，在 server.py 里实现。本模块只负责"报文长什么样、
    怎么串行化 / 反串行化"，这样编解码可以被 server.py 和 tools/fake_phone.py 共用。

本模块包含：
    PROTO_VER / MAX_FRAME_BYTES / HEARTBEAT_S / HEARTBEAT_TIMEOUT_S 等常量
    now_ms()        取当前 Unix 毫秒时间戳
    build_msg()     构造带公共字段的消息 dict
    encode_msg()    dict -> 长度前缀 + JSON 字节串
    recv_frame()    从 socket 读一帧并解析为 dict（处理粘包 / 拆包）
"""
from __future__ import annotations

import json
import socket
import struct
import time

# ---------- 常量（与 protocol.md 第 8 节一一对应） ----------
PROTO_VER = 1          # 协议版本，hello 校验时比对
DEFAULT_PORT = 8765    # 默认监听端口（实际以 priority.json 的 server.port 为准）
MAX_FRAME_BYTES = 65535  # 单帧正文最大字节数，超过视为畸形包
HEARTBEAT_S = 3        # 心跳周期（秒）：每 3 秒发一次 ping
HEARTBEAT_TIMEOUT_S = 6  # 心跳超时（秒）：连续 6 秒没收到对端任何报文判离线


def now_ms() -> int:
    """返回当前 Unix 毫秒时间戳，用作消息的 ts 公共字段（只记日志，不做决策）。"""
    return int(time.time() * 1000)


def build_msg(msg_type: str, **fields) -> dict:
    """构造一条带公共字段的消息 dict。

    作用：
        所有消息都要带 type / v / ts 三个公共字段（protocol.md 第 4 节），
        这里统一补上，调用方只填本类型特有的字段即可，避免漏写公共字段。

    入参：
        msg_type (str)：消息类型，如 "hello"、"phone_state"、"decision"。
        **fields：该类型特有的字段，直接并进返回的 dict。

    返回（dict）：
        完整消息，已包含 type、v=PROTO_VER、ts=now_ms()。
    """
    msg = {"type": msg_type, "v": PROTO_VER, "ts": now_ms()}
    msg.update(fields)
    return msg


def encode_msg(payload: dict) -> bytes:
    """把消息 dict 编码成"4 字节长度前缀 + JSON 正文"的字节串。

    作用：
        这是 protocol.md 第 3 节的帧格式实现。长度前缀是解决 TCP 粘包 / 拆包的关键。

    入参：
        payload (dict)：消息内容（应已包含 type 等公共字段）。

    返回（bytes）：
        可直接 sendall 的字节串。格式：前 4 字节大端无符号整数表示正文长度 N，
        后面跟 N 字节 UTF-8 JSON。

    可能异常：
        ValueError：正文长度超过 MAX_FRAME_BYTES（正常消息不可能这么长，属于编程错误）。
    """
    # ensure_ascii=False 让中文（如 app 显示名）原样保留，不会被转成 \uXXXX；
    # 反正有长度前缀，UTF-8 多字节不会导致"长度算错"。
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"消息过长：{len(body)} 字节，超过上限 {MAX_FRAME_BYTES}")
    # ">I" 表示"大端、无符号 32 位整数"（network byte order），与 protocol.md 约定一致。
    return struct.pack(">I", len(body)) + body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """从 socket 精确读满 n 字节才返回（处理 TCP 拆包）。

    入参：
        sock：已连接的 socket。
        n：要读的字节数。

    返回（bytes）：
        恰好 n 字节。

    可能异常：
        ConnectionError：读到一半对端关闭（recv 返回空），说明连接断了。
        socket.timeout：设置了超时的 socket 在 n 字节读满前超时（由调用方处理）。
    """
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            # recv 返回空 = 对端正常关闭连接（close / 断网），不是"暂时没数据"。
            raise ConnectionError("对端关闭了连接")
        buf += chunk
    return buf


def recv_frame(sock: socket.socket) -> dict:
    """从 socket 读出一帧完整消息，解析并返回 dict。

    作用：
        先读 4 字节长度，再按长度读正文，最后解析 JSON。
        这样就天然解决了"两次 send 合一次收到 / 一次 send 分几次收到"的粘包拆包问题。

    入参：
        sock：已连接的 socket。

    返回（dict）：
        解析出的消息对象（已保证顶层是 JSON 对象）。

    可能异常：
        ConnectionError：读长度或正文时对端关闭。
        socket.timeout：读满一帧前超时（调用方据此做心跳超时判断）。
        ValueError：长度超过上限，或正文不是合法 JSON / 顶层不是对象。
    """
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        # 长度字段被破坏或对端发了畸形包：不可信，直接抛错交给上层断开。
        raise ValueError(f"帧长度 {length} 超过上限 {MAX_FRAME_BYTES}")

    body = _recv_exact(sock, length)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("帧正文顶层必须是 JSON 对象")
    return data
