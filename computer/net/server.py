"""TCP 服务端模块（电脑端）。

作用：
    电脑作为 TCP 服务端，等手机连进来。连上之后负责三件事：
    1. 配对：校验 hello 里的配对码和协议版本，通过才进入 READY，否则回 hello_reject 并断开；
    2. 收消息：持续接收手机发来的 phone_state / pause_ack / ping；
    3. 发消息：主线程需要时，把 decision / pause_cmd 发给手机。

    同时维护一份"手机当前状态"的共享数据，主线程每秒读它来做决策。

为什么单独跑一个线程（对应选型"双线程"）：
    网络收消息是"阻塞"的（recv 会一直等手机发数据）。如果和主调度循环
    放在同一个线程，主循环就会被卡住，没法每秒枚举电脑音频状态。
    所以本模块用一个独立线程专心管网络，主线程专心做"枚举 → 决策 → 执行"。

线程安全：
    手机状态 _phone 和当前连接 _conn 会被两个线程同时访问（网络线程写、
    主线程读），所以所有读写都加同一把锁 self._lock 保护。

本模块包含：
    PhoneServer 类：
        start()            启动网络线程
        stop()             停止服务并关闭连接
        get_phone_state()  主线程读手机当前状态（未连接返回 None）
        send()             发送任意消息
        send_decision()    发送决策结果（M5）
        send_pause_cmd()   下发暂停命令（M6）
"""
from __future__ import annotations

import logging
import socket
import threading

from net.protocol import (
    HEARTBEAT_S,
    HEARTBEAT_TIMEOUT_S,
    PROTO_VER,
    build_msg,
    encode_msg,
    now_ms,
    recv_frame,
)

log = logging.getLogger("phone-server")


class PhoneServer:
    """电脑侧的 TCP 服务端 + 手机连接管理器。"""

    def __init__(self, port: int, pair_code: str):
        """初始化服务端。

        入参：
            port (int)：监听端口，来自配置的 server.port。
            pair_code (str)：配对码，来自配置的 server.pair_code。
        """
        self.port = port
        self.pair_code = pair_code
        self._lock = threading.Lock()  # 保护 _phone 和 _conn 的跨线程访问
        self._phone: dict | None = None  # 手机当前状态，None 表示未连接
        self._conn: socket.socket | None = None  # 当前已认证的连接
        self._stop = threading.Event()  # 停止信号
        self._thread: threading.Thread | None = None  # 网络线程
        self._cmd_seq = 0  # pause_cmd 的自增命令号（与 pause_ack 配对）

    # ---------------- 对外方法（主线程调用） ----------------

    def start(self) -> None:
        """启动网络线程（幂等：已在跑就不重复启动）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="phone-server")
        self._thread.start()

    def stop(self) -> None:
        """停止服务：置停止标志并关闭当前连接，让阻塞的 recv / accept 退出。"""
        self._stop.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def get_phone_state(self) -> dict | None:
        """返回手机当前状态的副本；未连接返回 None。

        返回（dict | None）：
            None 表示手机没连上。否则返回一份拷贝（调用方改它不影响内部状态）：
            {
              "playing": bool, "package": str, "app_name": str,
              "state_code": int, "has_control": bool, "connected": True,
            }
        """
        with self._lock:
            phone = self._phone
        # 返回副本，避免调用方拿到引用后意外改坏内部状态。
        return dict(phone) if phone is not None else None

    def send(self, msg: dict) -> bool:
        """把一条消息发给当前连接的手机（线程安全）。

        返回（bool）：
            True 发送成功；False 没连接（或发送时连接已断）。
        """
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        return self._raw_send(conn, msg)

    def send_decision(self, decision: dict) -> bool:
        """发送决策结果（协议 M5）。入参是 decide() 的返回值。"""
        return self.send(
            build_msg(
                "decision",
                mode=decision["mode"],
                reason=decision["reason"],
                pc_app=decision["pc_app"],
                phone_app=decision["phone_app"],
                pc_priority=decision["pc_priority"],
                phone_priority=decision["phone_priority"],
            )
        )

    def send_pause_cmd(self, package: str) -> bool:
        """下发暂停命令（协议 M6），命令号自增，供手机回 pause_ack 时配对。"""
        with self._lock:
            self._cmd_seq += 1
            cmd_id = self._cmd_seq
        return self.send(build_msg("pause_cmd", cmd_id=cmd_id, package=package, reason="PC_WINS"))

    # ---------------- 内部实现（网络线程内调用） ----------------

    @staticmethod
    def _raw_send(conn: socket.socket, msg: dict) -> bool:
        """直接向指定 socket 发送一条消息，不涉及锁（供本类内部用）。"""
        try:
            conn.sendall(encode_msg(msg))
            return True
        except OSError:
            return False

    def _run(self) -> None:
        """网络线程主体：监听端口，循环接受连接。"""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR：程序重启时能立即复用端口，避免"上一次的 socket 还在 TIME_WAIT"导致绑定失败。
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind(("0.0.0.0", self.port))  # 0.0.0.0 = 监听所有网卡，手机从 WiFi 能连进来
        except OSError as exc:
            log.error(f"绑定端口 {self.port} 失败：{exc}（端口可能被占用）")
            return
        server_sock.listen(1)  # 只允许一个手机（P6 一对一）
        # accept 也设超时，否则 stop() 后线程可能一直卡在 accept 上退不出去。
        server_sock.settimeout(1)
        log.info(f"网络服务已启动，监听 0.0.0.0:{self.port}")

        while not self._stop.is_set():
            try:
                conn, addr = server_sock.accept()
            except socket.timeout:
                continue  # 每秒醒来一次，检查是否要退出
            except OSError:
                break  # 监听 socket 被关闭
            # 处理这个连接（会阻塞到连接结束才返回，再回来 accept 下一个）
            self._handle_connection(conn, addr)

        try:
            server_sock.close()
        except OSError:
            pass
        log.info("网络服务已停止")

    def _handle_connection(self, conn: socket.socket, addr) -> None:
        """处理一个 TCP 连接：配对 → 收发消息 → 收尾。"""
        # ---- 1. 只允许一个手机（P6），第二个连接直接拒绝 ----
        with self._lock:
            busy = self._conn is not None
        if busy:
            self._raw_send(conn, build_msg("hello_reject", code="BUSY", msg="已有手机连接，请先断开再试"))
            conn.close()
            log.warning(f"拒绝来自 {addr} 的第二个连接（BUSY）")
            return

        # 读超时设为心跳周期：每 3 秒没数据就触发一次 socket.timeout，
        # 借此机会发 ping、判断是否离线（见 _recv_loop）。
        conn.settimeout(HEARTBEAT_S)

        # ---- 2. 等第一条消息 hello，并做配对校验 ----
        try:
            hello = recv_frame(conn)
        except (socket.timeout, ConnectionError, OSError, ValueError):
            # 连上后既不说话又超时，或第一条就发畸形包：直接放弃。
            conn.close()
            return

        ok, code, reason = self._auth(hello)
        if not ok:
            self._raw_send(conn, build_msg("hello_reject", code=code, msg=reason))
            conn.close()
            log.warning(f"{addr} 配对失败：{code} {reason}")
            return

        # ---- 3. 配对通过：注册为当前连接，回 hello_ok ----
        session_id = f"session-{now_ms()}"  # 用时间戳当会话号，仅日志区分
        with self._lock:
            self._conn = conn
            # 初始状态：已连接但没在播。之后由 phone_state 持续更新。
            self._phone = {
                "playing": False,
                "package": "",
                "app_name": "",
                "state_code": 0,
                "has_control": False,
                "connected": True,
            }
        log.info(
            f"手机已接入：{addr}，device_id={hello.get('device_id')}，"
            f"名称={hello.get('device_name')}"
        )
        self._raw_send(
            conn,
            build_msg("hello_ok", session_id=session_id, server_name=socket.gethostname(), heartbeat_s=HEARTBEAT_S),
        )

        # ---- 4. 进入收发循环，直到断线 / 超时 / 程序退出 ----
        self._recv_loop(conn)

        # ---- 5. 收尾：清状态、关连接 ----
        with self._lock:
            if self._conn is conn:  # 防止把"新连接"的状态误清掉
                self._conn = None
                self._phone = None
        try:
            conn.close()
        except OSError:
            pass
        log.info("手机连接已结束")

    def _auth(self, hello: dict) -> tuple[bool, str, str]:
        """校验 hello 消息，返回 (是否通过, 错误码, 错误描述)。

        错误码与 protocol.md M3 一致：BAD_VERSION / BAD_PAIR_CODE。
        顺序：先看版本再看配对码，版本不符优先提示版本问题。
        """
        if hello.get("v") != PROTO_VER:
            return False, "BAD_VERSION", "协议版本不匹配，请升级手机端"
        if hello.get("pair_code") != self.pair_code:
            return False, "BAD_PAIR_CODE", "配对码错误"
        return True, "", ""

    def _recv_loop(self, conn: socket.socket) -> None:
        """READY 之后的收发循环：收消息分发 + 心跳检测。"""
        last_seen = now_ms()  # 最近一次收到对端报文的时间（毫秒）

        while not self._stop.is_set():
            try:
                msg = recv_frame(conn)
            except socket.timeout:
                # 3 秒没收到任何数据。先判断是否已经超过心跳超时（6 秒 = 2 个周期）。
                if now_ms() - last_seen > HEARTBEAT_TIMEOUT_S * 1000:
                    log.warning("心跳超时，判定手机离线")
                    break
                # 还没超时：主动发个 ping 探活，顺便让手机回 pong。
                self.send(build_msg("ping", ts=now_ms()))
                continue
            except (ConnectionError, OSError, ValueError) as exc:
                # 对端关闭 / 网络异常 / 收到畸形包：结束本次连接。
                log.info(f"连接结束：{exc}")
                break

            last_seen = now_ms()  # 收到任意合法报文都算"还活着"
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        """按消息类型分发处理。"""
        msg_type = msg.get("type")

        if msg_type == "phone_state":
            self._handle_phone_state(msg)
        elif msg_type == "pause_ack":
            self._handle_pause_ack(msg)
        elif msg_type == "ping":
            # pong 原样回传 ts，手机可据此估算往返时间（protocol.md M8）。
            self.send(build_msg("pong", ts=msg.get("ts")))
        elif msg_type == "pong":
            pass  # 收到 pong 说明链路通，last_seen 已更新，无需额外动作
        elif msg_type == "hello":
            self.send(build_msg("error", code="NOT_AUTHED", msg="已配对，重复 hello"))
        else:
            self.send(build_msg("error", code="UNKNOWN_TYPE", msg=f"未知消息类型：{msg_type!r}"))

    def _handle_phone_state(self, msg: dict) -> None:
        """更新手机当前状态（协议 M4），供主线程决策使用。"""
        package = msg.get("package")
        if not isinstance(package, str):
            package = ""
        app_name = msg.get("app_name")
        if not isinstance(app_name, str):
            app_name = ""

        with self._lock:
            self._phone = {
                "playing": bool(msg.get("playing", False)),
                "package": package,
                "app_name": app_name,
                "state_code": msg.get("state_code", 0),
                "has_control": bool(msg.get("has_control", False)),
                "connected": True,
            }
        log.debug(f"手机上报：playing={msg.get('playing')} package={package} state_code={msg.get('state_code')}")

    def _handle_pause_ack(self, msg: dict) -> None:
        """处理手机暂停回执（协议 M7）。首版只记日志，不重发。"""
        log.info(
            f"手机暂停回执：cmd_id={msg.get('cmd_id')} ok={msg.get('ok')} "
            f"state_code={msg.get('state_code')} err={msg.get('err', '')}"
        )
