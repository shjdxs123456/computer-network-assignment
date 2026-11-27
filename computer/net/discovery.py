"""UDP 自动发现服务（电脑端）。

作用：
    手机 App 打开后，通过 UDP 广播一条"发现请求"，本服务收到后单播回复
    "我是电脑，我的 IP 和 TCP 端口是 xxx"，让手机**不用手动输 IP** 就能连上。
    对应「校园网 AP 隔离 → 手机开热点」场景：热点网络内没有隔离，广播能通。

为什么用 UDP 而不是 TCP：
    发现是"一问一答、允许丢失"（手机可以连发几次，收到一次就行），
    不需要可靠传输；UDP 广播正是局域网设备发现的经典做法（类似 DHCP 找网关）。
    真正的数据（播放状态、指令）仍走 TCP 的 8765，因为那要求可靠、有序。

为什么回复里不带配对码：
    自动发现只负责"找到电脑的 IP 和端口"；配对码是手机与电脑之间约定的
    安全凭据，仍由用户在手机上输入（首次输入后记住）。两者职责分开。

本模块包含：
    DiscoveryService 类：
        start()  启动 UDP 监听线程（端口 DISCOVERY_PORT）
        stop()   停止服务
"""
from __future__ import annotations

import json
import logging
import socket
import threading

from net.netutil import get_all_local_ips, get_local_ip
from net.protocol import DISCOVERY_PORT, PROTO_VER

log = logging.getLogger("discovery")


class DiscoveryService:
    """UDP 自动发现服务：收到 discover 广播就回 discover_ok。"""

    def __init__(self, tcp_port: int, udp_port: int = DISCOVERY_PORT):
        """初始化发现服务。

        入参：
            tcp_port (int)：TCP 数据通道端口，回复给手机让它去连这个端口。
            udp_port (int)：UDP 发现端口，默认 DISCOVERY_PORT(8766)。
        """
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        """启动 UDP 监听线程（幂等：已在跑就不重复启动）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="discovery")
        self._thread.start()

    def stop(self) -> None:
        """停止服务：置停止标志并关闭 socket，让阻塞的 recvfrom 退出。"""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        """UDP 线程主体：监听发现端口，循环收请求并回复。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR：程序重启时能立即复用端口。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_BROADCAST：允许收发广播包（手机发的是 255.255.255.255 受限广播）。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("0.0.0.0", self.udp_port))  # 绑所有网卡，热点网卡的广播也能收到
        except OSError as exc:
            log.error(f"UDP 发现服务绑定端口 {self.udp_port} 失败：{exc}")
            return
        sock.settimeout(1)  # 每秒醒来一次检查是否要退出
        self._sock = sock
        log.info(f"UDP 发现服务已启动，端口 {self.udp_port}")

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(sock, data, addr)

        try:
            sock.close()
        except OSError:
            pass
        log.info("UDP 发现服务已停止")

    def _handle(self, sock: socket.socket, data: bytes, addr) -> None:
        """处理一条 UDP 报文：是 discover 就回 discover_ok，否则忽略。"""
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # 不是我们的发现请求（乱码/别的程序），直接忽略

        if not isinstance(msg, dict) or msg.get("type") != "discover":
            return

        # 手机在请求里带上它自己的 IP（from_ip），电脑据此挑"和手机同网段"的
        # 网卡 IP 回复。这是关键：电脑连热点后可能有多张网卡，必须回对那张。
        from_ip = msg.get("from_ip", "")
        my_ip = self._pick_ip(from_ip)

        reply = {
            "type": "discover_ok",
            "v": PROTO_VER,
            "ip": my_ip,                 # 手机该连的电脑 IP
            "port": self.tcp_port,       # 手机该连的 TCP 端口
            "server_name": socket.gethostname(),
        }
        try:
            # 单播回给"发请求的地址"，而不是广播，避免打扰其他设备。
            sock.sendto(json.dumps(reply, ensure_ascii=False).encode("utf-8"), addr)
        except OSError:
            return
        log.info(f"收到发现请求 from={addr}，回复 IP={my_ip}:{self.tcp_port}")

    @staticmethod
    def _pick_ip(from_ip: str) -> str:
        """挑选要回复给手机的电脑 IP。

        逻辑：
            手机热点通常是 /24 网段（如 192.168.43.0/24）。手机发来的 from_ip
            形如 "192.168.43.1"，取它的前三段作为网段前缀，在本机所有 IP 里
            找一个同网段的返回（那就是电脑在热点里的 IP）。
            找不到同网段时，退回 get_local_ip() 的推荐值。

        入参：
            from_ip (str)：手机自己的 IP（discover 请求里带上来的）。
        """
        # rsplit(".", 1) 把 "192.168.43.1" 拆成 ["192.168.43", "1"]，取前一段。
        if from_ip and from_ip.count(".") == 3:
            prefix = from_ip.rsplit(".", 1)[0]
            for ip in get_all_local_ips():
                if ip.startswith(prefix + "."):
                    return ip
        # 没带 from_ip 或没匹配到同网段：退回推荐 IP（可能不准确，但总比没有强）。
        return get_local_ip()
