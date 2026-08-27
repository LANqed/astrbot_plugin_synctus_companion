"""重连型 Synctus 客户端：把 Bot 作为房间里的一台虚拟设备发布状态。

行为与 crates/core/src/client.rs 对齐的部分：
- 握手 Hello → Challenge → Auth → Welcome
- 每 25 秒发一次 Ping 保活
- 指数退避重连：起始 1 秒、上限 30 秒、叠加最多 25% 抖动
- 每次成功连接后立刻重发最后一次状态与待办，配合中继的保留状态，
  双方在一个来回内互相可见
- 配对码或协议版本错误属于终态，停止重连

这里只发布状态、接收对端的 nudge/status，不实现番茄钟引擎——Bot 的
"专注"来自陪伴插件的日程，而不是本地计时器。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional
from collections.abc import Awaitable, Callable

from . import proto
from .crypto import MissingDependency, RoomKeys, b64, unb64
from .model import now_ms

HEARTBEAT_SECS = 25
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
BACKOFF_JITTER = 0.25
# 中继的空闲超时是 90 秒，读超时留出足够余量但仍能发现僵死连接。
READ_TIMEOUT_SECS = 90.0
HANDSHAKE_TIMEOUT_SECS = 15.0
CLOCK_SKEW_WARN_MS = 60_000

PLUGIN_VERSION = "1.0.0"


class FatalConfigError(Exception):
    """终态错误：重试没有意义，等用户改配置。"""


@dataclass
class SynctusClientConfig:
    server: str
    invite_code: str
    device_id: str
    device_name: str
    user: str
    tls: bool = True
    tls_verify: bool = True
    tls_server_name: Optional[str] = None

    def server_host_port(self) -> tuple[str, int]:
        text = self.server.strip()
        if text.startswith("["):
            host, _, rest = text[1:].partition("]")
            port = rest.lstrip(":")
            return host, int(port or 8787)
        host, _, port = text.rpartition(":")
        if not host:
            return text, 8787
        return host, int(port or 8787)

    def server_name(self) -> str:
        if self.tls_server_name:
            return self.tls_server_name
        return self.server_host_port()[0]


@dataclass
class PeerView:
    """对端设备的最近一次快照，供插件渲染。"""

    status: dict = field(default_factory=dict)
    todos: list = field(default_factory=list)
    online: bool = True
    received_at: float = 0.0


class SynctusClient:
    """一条自动重连的 Synctus 连接。

    生命周期：`start()` 起后台任务，`publish()` / `publish_todos()` 更新本机
    状态，`stop()` 关闭。所有回调都在事件循环里调用，回调内的异常会被记录
    但不会打断连接。
    """

    def __init__(
        self,
        config: SynctusClientConfig,
        *,
        on_nudge: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_peer_status: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        on_state_change: Optional[Callable[[str, str], Awaitable[None]]] = None,
        logger=None,
    ) -> None:
        self._config = config
        self._on_nudge = on_nudge
        self._on_peer_status = on_peer_status
        self._on_state_change = on_state_change
        self._log = logger
        self._keys: Optional[RoomKeys] = None
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_status: Optional[dict] = None
        self._last_todos: Optional[list] = None
        self._peers: dict[str, PeerView] = {}
        self._connected = False
        self._last_error = ""
        self._fatal = ""

    # ---- 公开状态 ----------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._fatal or self._last_error

    @property
    def peers(self) -> dict[str, PeerView]:
        return self._peers

    def room_id_hex(self) -> str:
        return self._keys.room_id_hex() if self._keys else ""

    # ---- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="synctus-client")

    async def stop(self) -> None:
        self._stop.set()
        writer, self._writer = self._writer, None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({task}, timeout=3)
        self._connected = False

    async def publish(self, status: dict) -> None:
        """发布状态快照。断线时只缓存，重连后自动补发。"""
        self._last_status = status
        await self._send_peer({"t": "status", **status}, retain="status")

    async def publish_todos(self, items: list) -> None:
        self._last_todos = items
        await self._send_peer(
            {"t": "todos", "device_id": self._config.device_id, "items": items, "at": now_ms()},
            retain="todos",
        )

    async def send_nudge(self, kind: str, text: str, from_name: str) -> bool:
        payload = {
            "t": "nudge",
            "kind": kind,
            "from_name": from_name,
            "at": now_ms(),
        }
        if text:
            payload["text"] = text
        return await self._send_peer(payload, retain=None)

    # ---- 内部实现 ----------------------------------------------------

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[SynctusCompanion] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[SynctusCompanion] {message}")

    async def _emit_state(self, state: str, detail: str) -> None:
        if self._on_state_change is None:
            return
        try:
            await self._on_state_change(state, detail)
        except Exception as exc:
            self._warn(f"状态回调异常: {exc}")

    async def _send_peer(self, payload: dict, *, retain: Optional[str]) -> bool:
        writer = self._writer
        if writer is None or self._keys is None:
            return False
        try:
            plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            # aad 绑定发送方设备标识，房间内的设备无法冒充别人发消息。
            sealed = self._keys.seal(plaintext, self._config.device_id.encode("utf-8"))
            await proto.write_frame(
                writer,
                proto.relay_frame(self._config.device_id, b64(sealed), retain),
            )
            return True
        except (OSError, proto.ProtocolError, ValueError) as exc:
            self._last_error = f"发送失败: {exc}"
            self._warn(self._last_error)
            return False

    async def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                self._keys = RoomKeys(self._config.invite_code)
            except MissingDependency as exc:
                self._fatal = str(exc)
                self._warn(self._fatal)
                await self._emit_state("fatal", self._fatal)
                return
            except ValueError as exc:
                self._fatal = f"配对码无效: {exc}"
                self._warn(self._fatal)
                await self._emit_state("fatal", self._fatal)
                return

            try:
                await self._session()
                backoff = BACKOFF_START
            except FatalConfigError as exc:
                self._fatal = str(exc)
                self._warn(f"连接被拒绝，停止重试: {exc}")
                await self._emit_state("fatal", str(exc))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._warn(f"连接中断，将重连: {self._last_error}")
            finally:
                if self._connected:
                    self._connected = False
                    await self._emit_state("disconnected", self._last_error)
                self._writer = None

            if self._stop.is_set():
                return
            # 叠加抖动，避免两端同时重启后以相同节奏反复冲击服务器。
            delay = min(backoff, BACKOFF_MAX) * (1 + random.random() * BACKOFF_JITTER)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self._config.tls:
            return None
        ctx = ssl.create_default_context()
        if not self._config.tls_verify:
            # 自签中继：载荷仍是端到端加密的，但会失去中继身份验证。
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _session(self) -> None:
        host, port = self._config.server_host_port()
        ctx = self._ssl_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=ctx,
                server_hostname=self._config.server_name() if ctx else None,
            ),
            timeout=HANDSHAKE_TIMEOUT_SECS,
        )
        self._writer = writer
        try:
            await asyncio.wait_for(
                self._handshake(reader, writer), timeout=HANDSHAKE_TIMEOUT_SECS
            )
            self._connected = True
            self._last_error = ""
            self._info(f"已连接中继 {host}:{port}，房间 {self.room_id_hex()[:8]}…")
            await self._emit_state("connected", f"{host}:{port}")
            # 立刻重发最后一次状态，配合中继的保留状态在一个来回内互相可见。
            if self._last_status is not None:
                await self._send_peer({"t": "status", **self._last_status}, retain="status")
            if self._last_todos is not None:
                await self._send_peer(
                    {
                        "t": "todos",
                        "device_id": self._config.device_id,
                        "items": self._last_todos,
                        "at": now_ms(),
                    },
                    retain="todos",
                )
            await self._pump(reader, writer)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=3)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        assert self._keys is not None
        await proto.write_frame(
            writer,
            proto.hello_frame(
                self._keys.room_id_hex(),
                self._config.device_id,
                self._config.user,
                self._config.device_name,
                PLUGIN_VERSION,
            ),
        )
        frame = await proto.read_frame(reader)
        if frame is None:
            raise proto.ProtocolError("服务器在握手时关闭连接")
        if frame.kind == proto.KIND_ERROR:
            raise self._error_to_exception(frame)
        if frame.kind != proto.KIND_CHALLENGE:
            raise proto.ProtocolError(f"握手时收到意外帧: {frame.name}")

        nonce = unb64(str(frame.body.get("nonce") or ""))
        await proto.write_frame(
            writer, proto.auth_frame(b64(self._keys.auth_response(nonce)))
        )

        frame = await proto.read_frame(reader)
        if frame is None:
            raise proto.ProtocolError("认证后连接被关闭")
        if frame.kind == proto.KIND_ERROR:
            raise self._error_to_exception(frame)
        if frame.kind != proto.KIND_WELCOME:
            raise proto.ProtocolError(f"认证后收到意外帧: {frame.name}")

        for peer in frame.body.get("peers") or []:
            view = self._peers.setdefault(str(peer), PeerView())
            view.online = True
        skew = abs(now_ms() - int(frame.body.get("server_time") or 0))
        if skew > CLOCK_SKEW_WARN_MS:
            self._warn(f"本机时间与服务器相差 {skew // 1000} 秒，状态时间可能不准")

    def _error_to_exception(self, frame: proto.Frame) -> Exception:
        code = str(frame.body.get("code") or "")
        message = str(frame.body.get("message") or "")
        detail = f"{code}: {message}"
        # 配对码不一致与协议版本不符属于终态：重试只会一直失败。
        if code in {"auth_failed", "bad_proto", "proto_mismatch", "unauthorized"}:
            return FatalConfigError(detail)
        return proto.ProtocolError(detail)

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        next_ping = time.monotonic() + HEARTBEAT_SECS
        while not self._stop.is_set():
            timeout = max(1.0, min(next_ping - time.monotonic(), READ_TIMEOUT_SECS))
            try:
                frame = await asyncio.wait_for(proto.read_frame(reader), timeout=timeout)
            except asyncio.TimeoutError:
                if time.monotonic() >= next_ping:
                    await proto.write_frame(writer, proto.Frame(proto.KIND_PING))
                    next_ping = time.monotonic() + HEARTBEAT_SECS
                continue
            if frame is None:
                return
            await self._handle_frame(frame, writer)

    async def _handle_frame(
        self, frame: proto.Frame, writer: asyncio.StreamWriter
    ) -> None:
        if frame.kind == proto.KIND_PING:
            await proto.write_frame(writer, proto.Frame(proto.KIND_PONG))
            return
        if frame.kind == proto.KIND_PONG:
            return
        if frame.kind == proto.KIND_PRESENCE:
            device_id = str(frame.body.get("device_id") or "")
            online = bool(frame.body.get("online"))
            if device_id:
                view = self._peers.setdefault(device_id, PeerView())
                view.online = online
            return
        if frame.kind == proto.KIND_ERROR:
            # 中继超过限速时丢弃消息并回一条 Error 但不断开：突发通常是 bug。
            self._last_error = (
                f"{frame.body.get('code')}: {frame.body.get('message')}"
            )
            self._warn(f"中继返回错误: {self._last_error}")
            return
        if frame.kind == proto.KIND_RELAY:
            await self._handle_relay(frame)
            return

    async def _handle_relay(self, frame: proto.Frame) -> None:
        assert self._keys is not None
        sender = str(frame.body.get("from") or "")
        if not sender or sender == self._config.device_id:
            return
        try:
            sealed = unb64(str(frame.body.get("body") or ""))
            plaintext = self._keys.open(sealed, sender.encode("utf-8"))
            payload = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._warn(f"无法解密来自 {sender} 的消息: {exc}")
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("t") or "")
        if kind == "status":
            view = self._peers.setdefault(sender, PeerView())
            incoming_at = int(payload.get("at") or 0)
            if incoming_at < int(view.status.get("at") or 0):
                return  # 丢弃乱序更新
            view.status = payload
            view.online = True
            view.received_at = time.time()
            if self._on_peer_status is not None:
                try:
                    await self._on_peer_status(sender, payload)
                except Exception as exc:
                    self._warn(f"对端状态回调异常: {exc}")
        elif kind == "todos":
            view = self._peers.setdefault(sender, PeerView())
            items = payload.get("items")
            view.todos = items if isinstance(items, list) else []
        elif kind == "nudge":
            if self._on_nudge is not None:
                try:
                    await self._on_nudge(payload)
                except Exception as exc:
                    self._warn(f"nudge 回调异常: {exc}")
        elif kind == "ping":
            await self._send_peer({"t": "pong", "at": now_ms()}, retain=None)
