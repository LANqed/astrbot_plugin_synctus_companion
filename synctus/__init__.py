"""Synctus 客户端的最小 Python 实现（供 AstrBot 插件内使用）。

线路协议与密码学参数必须与 crates/core 保持一致，见 docs/PROTOCOL.md。
"""

from .client import SynctusClient, SynctusClientConfig
from .crypto import RoomKeys, random_id
from .model import battery, foreground_app, pomodoro_state, status_snapshot, todo

__all__ = [
    "RoomKeys",
    "SynctusClient",
    "SynctusClientConfig",
    "battery",
    "foreground_app",
    "pomodoro_state",
    "random_id",
    "status_snapshot",
    "todo",
]
