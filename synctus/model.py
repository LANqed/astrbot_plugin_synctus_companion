"""与对端同步的状态模型：字段名必须与 crates/core/src/model.rs 的 serde 表示一致。

Rust 侧用 `#[serde(tag = "t")]` 的内部标签枚举承载载荷，因此 `status` 的字段
是平铺的，而 `todos` 是带 `device_id/items/at` 的结构变体。字段名保持简短，
因为每次变化都会重新发布整份快照。
"""

from __future__ import annotations

import time
from typing import Any, Optional

# Platform 枚举：serde rename_all = "lowercase"
PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"
PLATFORM_ANDROID = "android"
PLATFORM_OTHER = "other"

# Presence 枚举：serde rename_all = "snake_case"
PRESENCE_ACTIVE = "active"
PRESENCE_RESTING = "resting"
PRESENCE_AWAY = "away"
PRESENCE_BUSY = "busy"

PRESENCE_LABELS = {
    PRESENCE_ACTIVE: "在忙",
    PRESENCE_RESTING: "休息中",
    PRESENCE_AWAY: "离开",
    PRESENCE_BUSY: "免打扰",
}

# PomodoroPhase 枚举
PHASE_IDLE = "idle"
PHASE_FOCUS = "focus"
PHASE_SHORT_BREAK = "short_break"
PHASE_LONG_BREAK = "long_break"

# NudgeKind 枚举
NUDGE_LABELS = {
    "knock": ("👋", "敲了敲你"),
    "hug": ("🤗", "抱了抱你"),
    "coffee": ("☕", "请你喝咖啡"),
    "rest": ("🛋", "让你去休息"),
    "focus_together": ("🍅", "邀你一起专注"),
    "nag": ("👀", "发现你在摸鱼"),
    "cheer": ("🎉", "为你鼓掌"),
}


def now_ms() -> int:
    return int(time.time() * 1000)


def battery(percent: int, charging: bool, minutes_left: Optional[int] = None) -> dict:
    payload: dict[str, Any] = {
        "percent": max(0, min(100, int(percent))),
        "charging": bool(charging),
    }
    if minutes_left is not None:
        payload["minutes_left"] = max(0, int(minutes_left))
    return payload


def foreground_app(
    app: str, name: Optional[str] = None, title: Optional[str] = None
) -> dict:
    payload: dict[str, Any] = {"app": app}
    if name:
        payload["name"] = name
    if title:
        payload["title"] = title
    return payload


def pomodoro_state(
    phase: str,
    ends_at: Optional[int] = None,
    round_index: int = 0,
    completed_today: int = 0,
) -> dict:
    payload: dict[str, Any] = {
        "phase": phase,
        "round": max(0, int(round_index)),
        "completed_today": max(0, int(completed_today)),
    }
    if ends_at is not None:
        payload["ends_at"] = int(ends_at)
    return payload


def todo(
    todo_id: str,
    title: str,
    done: bool,
    created_at: int,
    done_at: Optional[int] = None,
    pomodoros: int = 0,
) -> dict:
    payload: dict[str, Any] = {
        "id": todo_id,
        "title": title,
        "done": bool(done),
        "created_at": int(created_at),
        "pomodoros": max(0, int(pomodoros)),
    }
    if done_at is not None:
        payload["done_at"] = int(done_at)
    return payload


def status_snapshot(
    *,
    device_id: str,
    name: str,
    platform: str,
    user: str,
    presence: str,
    at: Optional[int] = None,
    foreground: Optional[dict] = None,
    battery_state: Optional[dict] = None,
    music: Optional[dict] = None,
    pomodoro: Optional[dict] = None,
    todos_open: int = 0,
    todos_done_today: int = 0,
    idle_secs: Optional[int] = None,
    focus_today_min: int = 0,
    goal_min: int = 0,
    streak_days: int = 0,
) -> dict:
    """一台设备发布的全部内容。`at` 也用于对端丢弃乱序更新。"""
    payload: dict[str, Any] = {
        "device_id": device_id,
        "name": name,
        "platform": platform,
        "user": user,
        "at": now_ms() if at is None else int(at),
        "presence": presence,
        "todos_open": max(0, int(todos_open)),
        "todos_done_today": max(0, int(todos_done_today)),
        "focus_today_min": max(0, int(focus_today_min)),
        "goal_min": max(0, int(goal_min)),
        "streak_days": max(0, int(streak_days)),
    }
    if foreground is not None:
        payload["foreground"] = foreground
    if battery_state is not None:
        payload["battery"] = battery_state
    if music is not None:
        payload["music"] = music
    if pomodoro is not None:
        payload["pomodoro"] = pomodoro
    if idle_secs is not None:
        payload["idle_secs"] = max(0, int(idle_secs))
    return payload


def describe_nudge(nudge: dict) -> str:
    """把收到的 nudge 渲染成一行中文，用于转达给 QQ 侧。"""
    kind = str(nudge.get("kind") or "")
    emoji, default_text = NUDGE_LABELS.get(kind, ("👋", "碰了碰你"))
    sender = str(nudge.get("from_name") or "对方")
    text = str(nudge.get("text") or "").strip()
    if text:
        return f"{emoji} {sender}：{text}"
    return f"{emoji} {sender} {default_text}"
