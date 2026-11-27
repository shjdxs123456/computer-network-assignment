"""本机网络信息工具模块。

作用：
    提供"本机 IP 地址"相关的两个小工具函数，供 main.py（打印启动信息）和
    discovery.py（自动发现时选择正确的网卡 IP 回复）共用。

为什么单独拆一个文件：
    这两个函数会被多处使用，且和"协议编解码"、"连接管理"都不是一回事。
    单独放一个文件，避免 main 和 discovery 之间互相 import 造成循环依赖。

本模块包含：
    get_local_ip()     估算本机"对外出口"的 IP（作为推荐值）
    get_all_local_ips()  列出本机所有非回环 IPv4（供对照挑选）
"""
from __future__ import annotations

import socket


def get_local_ip() -> str:
    """估算本机在局域网里的 IPv4 地址，作为"推荐 IP"返回。

    原理：
        创建一个 UDP socket 并"假装"要连外部地址，系统会替我们选定一个出口网卡，
        读 socket 的本地地址就是这个网卡的 IP。UDP 不会真的发包，所以即便断网也能拿到。
        这是获取"对外 IP"的常见技巧，比 gethostbyname(gethostname()) 更不容易拿到 127.0.0.1。

    注意：
        当电脑有多张网卡（比如有线网 + 连手机热点的 WiFi），它选的是"默认路由"
        走的那个网卡，未必是手机能连到的那个。所以只当"推荐值"，最终要让用户
        对照 get_all_local_ips() 的结果挑选。

    返回（str）：
        本机 IP；失败时返回 "127.0.0.1"。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # 只用于选出口网卡，不真的连
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def get_all_local_ips() -> list[str]:
    """列出本机所有非回环 IPv4 地址，供手机对照选择。

    为什么需要它：
        电脑可能同时有多张网卡（有线网 + 连手机热点的 WiFi），
        get_local_ip() 猜的那个 IP 未必是手机能连到的那个。
        把所有 IP 都列出来让用户对照，最不容易填错。

    返回（list[str]）：
        去重、排序后的 IPv4 列表（不含 127.x）；取不到时返回空列表。
    """
    ips: set[str] = set()
    try:
        # 用主机名反查所有 IPv4 地址。Windows 上通常会返回每张网卡的 IP。
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)
