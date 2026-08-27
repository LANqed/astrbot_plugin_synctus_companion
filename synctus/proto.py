"""线路协议：长度前缀帧，与 crates/core/src/proto.rs 对应。

    ┌────────────┬──────────┬──────────────────────┐
    │ len: u32be │ kind: u8 │ body: len-1 字节     │
    └────────────┴──────────┴──────────────────────┘

`len` 上限 64 KiB，先校验再分配，这是每条连接的内存上界。
body 是 JSON（Ping/Pong 无 body）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

PROTOCOL_VERSION = 1
MAX_FRAME_LEN = 64 * 1024

KIND_HELLO = 1
KIND_CHALLENGE = 2
KIND_AUTH = 3
KIND_WELCOME = 4
KIND_ERROR = 5
KIND_RELAY = 6
KIND_PRESENCE = 7
KIND_PING = 8
KIND_PONG = 9

_KIND_NAMES = {
    KIND_HELLO: "hello",
    KIND_CHALLENGE: "challenge",
    KIND_AUTH: "auth",
    KIND_WELCOME: "welcome",
    KIND_ERROR: "error",
    KIND_RELAY: "relay",
    KIND_PRESENCE: "presence",
    KIND_PING: "ping",
    KIND_PONG: "pong",
}


class ProtocolError(Exception):
    """协议层面的错误。与网络异常区分，便于上层决定是否重连。"""


class Frame:
    __slots__ = ("body", "kind")

    def __init__(self, kind: int, body: Optional[dict] = None) -> None:
        self.kind = kind
        self.body = body or {}

    def __repr__(self) -> str:
        name = _KIND_NAMES.get(self.kind, str(self.kind))
        return f"Frame({name}, {self.body!r})"

    @property
    def name(self) -> str:
        return _KIND_NAMES.get(self.kind, str(self.kind))


def hello_frame(
    room: str, device_id: str, user: str, device_name: str, version: str
) -> Frame:
    return Frame(
        KIND_HELLO,
        {
            "proto": PROTOCOL_VERSION,
            "room": room,
            "device_id": device_id,
            "version": version,
            # 明文发送：中继解不开状态载荷，但管理面板需要按主人分组设备。
            # 这是用户自选的显示名，不是秘密。
            "user": user,
            "device_name": device_name,
        },
    )


def auth_frame(mac_b64: str) -> Frame:
    return Frame(KIND_AUTH, {"mac": mac_b64})


def relay_frame(from_device: str, body_b64: str, retain: Optional[str]) -> Frame:
    body: dict[str, Any] = {"from": from_device, "body": body_b64}
    if retain:
        body["retain"] = retain
    return Frame(KIND_RELAY, body)


def encode_frame(frame: Frame) -> bytes:
    payload = b"" if frame.kind in (KIND_PING, KIND_PONG) else json.dumps(
        frame.body, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    length = len(payload) + 1
    if length > MAX_FRAME_LEN:
        raise ProtocolError(f"帧过大: {length} 字节")
    # 一个缓冲、一次写入：避免连接在帧头与帧体之间断掉留下半个帧。
    return length.to_bytes(4, "big") + bytes([frame.kind]) + payload


async def write_frame(writer: asyncio.StreamWriter, frame: Frame) -> None:
    writer.write(encode_frame(frame))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> Optional[Frame]:
    """读一个帧。干净的 EOF 返回 None。"""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("读取帧头时连接中断") from exc
    length = int.from_bytes(header, "big")
    if length == 0:
        raise ProtocolError("帧长度为 0")
    if length > MAX_FRAME_LEN:
        # 分配前拒绝：这是每条连接的内存上界。
        raise ProtocolError(f"帧长度超限: {length} > {MAX_FRAME_LEN}")
    try:
        buf = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("读取帧体时连接中断") from exc
    kind = buf[0]
    if kind not in _KIND_NAMES:
        raise ProtocolError(f"未知帧类型: {kind}")
    if kind in (KIND_PING, KIND_PONG):
        return Frame(kind)
    try:
        body = json.loads(buf[1:].decode("utf-8")) if len(buf) > 1 else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"帧体不是合法 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ProtocolError("帧体必须是 JSON 对象")
    return Frame(kind, body)
