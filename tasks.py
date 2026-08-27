"""Bot 的待办清单。

清单要像真人的：有具体的事、有截止日期带来的压力、会随一天推进被勾掉，
而不是三条永远不变的占位文本。来源按优先级：

1. **依赖插件的重要日期**（`important_dates`）→ 带 deadline 的任务，
   例如「周五要交的稿子（还剩 2 天）」。这类任务在临近时排在最前。
2. **今天的日程段** → 每个非睡眠段生成一件事，日程过去了就自动勾掉，
   于是对端看到的「已完成 4 / 待办 6」会随时间推进。
3. **依赖插件的可做事项**（`can_do`）→ 补齐到目标条数的零碎小事。

任务 id 由日期与内容哈希得出，因此同一天多次生成保持稳定，
对端不会看到列表整体跳动。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from .companion_bridge import days_until, fmt_hhmm

# 兜底小事：依赖插件没装、或 can_do 为空时用它补齐清单。
FILLER_TASKS = (
    "回一下攒着的消息",
    "把桌面上的东西归位",
    "把待读的文章清一清",
    "给手机和耳机充电",
    "整理今天拍的照片",
    "记一下今天的开销",
    "把明天要带的东西备好",
    "浇花、擦一下键盘",
)

DEADLINE_HINT_DAYS = 7  # 只显示七天内的 deadline，更远的还没有紧迫感
MAX_ITEMS = 12


def _stable_id(date_key: str, title: str) -> str:
    digest = hashlib.sha256(f"{date_key}|{title}".encode()).digest()
    return digest[:8].hex()


def _deadline_title(entry: dict, remaining: int) -> str:
    title = entry["title"]
    note = entry.get("note") or ""
    if remaining == 0:
        when = "今天截止"
    elif remaining == 1:
        when = "明天截止"
    else:
        when = f"还剩 {remaining} 天"
    base = f"{title}（{when}）"
    if note:
        base = f"{base} · {note}"
    return base[:80]


def build_task_list(
    data,
    now: datetime,
    *,
    goal_count: int = 8,
    day_start_ts_ms: int = 0,
) -> list:
    """生成今天的待办清单，返回 Synctus 的 Todo 字典列表。"""
    minute = now.hour * 60 + now.minute
    date_key = now.strftime("%Y-%m-%d")
    created_at = day_start_ts_ms or int(
        now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    items: list = []
    seen_titles: set = set()

    def add(title: str, done: bool, done_minute: Optional[int] = None) -> None:
        title = title.strip()
        if not title or title in seen_titles or len(items) >= MAX_ITEMS:
            return
        seen_titles.add(title)
        entry = {
            "id": _stable_id(date_key, title),
            "title": title,
            "done": done,
            "created_at": created_at,
            "pomodoros": 0,
        }
        if done:
            offset = minute if done_minute is None else done_minute
            entry["done_at"] = created_at + offset * 60_000
        items.append(entry)

    # 1) deadline 任务。近的排前面，今天/明天到期的最优先。
    deadlines = []
    for entry in data.important_dates:
        remaining = days_until(entry["date"], now)
        if remaining is None or remaining < 0 or remaining > DEADLINE_HINT_DAYS:
            continue
        deadlines.append((remaining, entry))
    deadlines.sort(key=lambda pair: pair[0])
    for remaining, entry in deadlines[:4]:
        add(_deadline_title(entry, remaining), done=False)

    # 2) 日程段任务：过去的段算已完成，于是完成数会随一天推进。
    for segment in data.segments:
        if segment.sleeping:
            continue
        title = f"{fmt_hhmm(segment.start)} {segment.activity}"
        already_past = not segment.contains(minute) and segment.start < minute
        add(title, done=already_past, done_minute=segment.end % 1440)

    # 3) can_do / 兜底小事补齐。
    extras = list(data.can_do) or list(FILLER_TASKS)
    for extra in extras:
        if len(items) >= max(goal_count, len(items)):
            break
        add(extra, done=False)

    return items[:MAX_ITEMS]


def counts(items: list) -> tuple:
    """返回 (未完成数, 今日已完成数)，对应 StatusSnapshot 的两个计数字段。"""
    done = sum(1 for item in items if item.get("done"))
    return len(items) - done, done


def render_task_lines(items: list, limit: int = 8) -> list:
    lines = []
    for item in items[:limit]:
        mark = "[x]" if item.get("done") else "[ ]"
        lines.append(f"{mark} {item.get('title', '')}")
    if len(items) > limit:
        lines.append(f"… 还有 {len(items) - limit} 项")
    return lines
