"""从 astrbot_plugin_private_companion 读取 Bot 的日程与拟人状态。

那个插件是**可选依赖**：装了就用它每天由 LLM 生成的真实日程、状态、可做事项
和重要日期；没装或读不到时退回内置的默认作息，插件本身仍然可用。

两条读取途径，按可靠性排序：

1. **运行中的实例**。AstrBot 的 `context.get_all_stars()` 给出插件注册表，
   拿到实例后读它的 `data` 字典（内存里就是最新的，不受落盘时机影响）。
   这条路径参考了依赖插件自己的 external_bridge_resolver.py。
2. **数据文件**。`<astrbot_data>/plugin_data/astrbot_plugin_private_companion/companions.json`。
   实例查找失败（未激活、热重载中、改了目录名）时用它兜底。

只读，不写：绝不修改依赖插件的任何数据。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Optional

DEPENDENCY_PLUGIN_NAME = "astrbot_plugin_private_companion"
DATA_FILE_NAME = "companions.json"

# 依赖插件把一天分成五段，slug/中文名/起始分钟/结束分钟；
# 结束 <= 起始表示跨午夜。与 bot_personal_contract.SCHEDULE_WINDOWS 一致。
SCHEDULE_WINDOWS = (
    ("late_night", "深夜", 21 * 60, 6 * 60),
    ("morning", "早晨", 6 * 60, 11 * 60),
    ("noon", "中午", 11 * 60, 14 * 60 + 30),
    ("afternoon", "下午", 14 * 60 + 30, 18 * 60),
    ("evening", "晚上", 18 * 60, 21 * 60),
)

# 睡眠段识别词，与依赖插件 daily_state._is_sleepy_plan_item 的判定保持一致。
# 分成两类：整夜睡眠一定算睡着；小睡类要让位给吃饭，因为 LLM 常把
# 「午餐和午休」写成一段，而那一段的主体是吃饭。
_NIGHT_SLEEP_WORDS = (
    "睡觉", "睡眠", "入睡", "熟睡", "浅睡", "梦乡", "被窝", "准备睡", "睡前", "熄灯休息",
)
_NAP_WORDS = ("午睡", "午休", "小睡", "补觉", "回笼觉", "打盹", "眯一会")
_AWAKE_WORDS = ("自然醒", "睡醒", "醒来", "起床", "洗漱", "失眠")

# 专注类活动：Bot 处于这些段时对外显示为"在忙"，且不适合被打扰。
_FOCUS_WORDS = (
    "工作", "上班", "正事", "学习", "上课", "写", "赶", "复习", "作业", "论文",
    "代码", "编程", "画", "创作", "投稿", "备课", "会议", "整理", "专注", "练",
    "努力", "加油", "干活",
)
_MEAL_WORDS = ("吃", "饭", "餐", "干饭", "食堂", "外卖")

# 缓存：内存实例每 20 秒复查一次，文件按 mtime 判断。频繁读取整份
# companions.json 会和依赖插件的写入竞争 IO，没有必要。
_INSTANCE_TTL_SECS = 20.0
_FILE_TTL_SECS = 10.0

DEFAULT_PLAN_ITEMS = [
    {"time": "07:30", "end": "08:10", "activity": "起床洗漱", "mood": "迷糊"},
    {"time": "08:10", "end": "09:00", "activity": "吃早餐", "mood": "满足"},
    {"time": "09:00", "end": "11:50", "activity": "专心做正事", "mood": "专注"},
    {"time": "11:50", "end": "13:30", "activity": "午餐和午休", "mood": "慵懒"},
    {"time": "13:30", "end": "16:30", "activity": "下午继续努力", "mood": "专注"},
    {"time": "16:30", "end": "17:30", "activity": "摸鱼休息", "mood": "放松"},
    {"time": "17:30", "end": "19:00", "activity": "晚餐时间", "mood": "开心"},
    {"time": "19:00", "end": "21:00", "activity": "自由时间", "mood": "放松"},
    {"time": "21:00", "end": "22:30", "activity": "洗漱准备睡觉", "mood": "安静"},
    {"time": "22:30", "end": "07:30", "activity": "睡觉", "mood": "熟睡"},
]

DEFAULT_STATE = {
    "sleep": "昨晚睡得还不错",
    "hunger": "不饿也不撑刚刚好",
    "health": "身体状态还行",
    "location": "在熟悉的小地方待着",
    "weather": "天气看起来还不错",
    "mood_bias": "心情挺平和",
    "energy": 70,
    "note": "",
}


def parse_hhmm(value: Any) -> Optional[int]:
    try:
        text = str(value).strip().replace("：", ":")
        hour, _, minute = text.partition(":")
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError, TypeError):
        return None
    return None


def fmt_hhmm(minute: int) -> str:
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def span_contains(start: int, end: int, minute: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # 跨午夜


def window_name(minute: int) -> str:
    for _slug, name, start, end in SCHEDULE_WINDOWS:
        if span_contains(start, end, minute):
            return name
    return "白天"


def _has_word(text: str, words) -> bool:
    return any(word in text for word in words)


def is_sleeping(activity: str) -> bool:
    """这一段 Bot 是不是睡着的。

    「睡醒后洗漱」含醒字，不算睡着；「午餐和午休」的主体是吃饭，
    也不算，否则整个午休时段会被当成不可打扰的睡眠。
    """
    if _has_word(activity, _AWAKE_WORDS):
        return False
    if _has_word(activity, _NIGHT_SLEEP_WORDS):
        return True
    return _has_word(activity, _NAP_WORDS) and not _has_word(activity, _MEAL_WORDS)


def is_focusing(activity: str) -> bool:
    if is_sleeping(activity) or _has_word(activity, _MEAL_WORDS):
        return False
    return _has_word(activity, _FOCUS_WORDS)


class Segment:
    """归一化后的一个日程段。"""

    __slots__ = ("activity", "end", "focusing", "message_seed", "mood", "sleeping", "start")

    def __init__(
        self,
        start: int,
        end: int,
        activity: str,
        mood: str = "",
        message_seed: str = "",
    ) -> None:
        self.start = start
        self.end = end
        self.activity = activity
        self.mood = mood
        self.message_seed = message_seed
        self.sleeping = is_sleeping(activity)
        self.focusing = is_focusing(activity)

    def __repr__(self) -> str:
        return f"Segment({fmt_hhmm(self.start)}-{fmt_hhmm(self.end)} {self.activity})"

    def contains(self, minute: int) -> bool:
        return span_contains(self.start, self.end, minute)

    def elapsed_minutes(self, minute: int) -> int:
        return (minute - self.start) % 1440

    def remaining_minutes(self, minute: int) -> int:
        return (self.end - minute) % 1440

    @property
    def key(self) -> str:
        return f"{self.start}-{self.activity}"

    @property
    def interruptible(self) -> bool:
        return not (self.sleeping or self.focusing)


class CompanionData:
    """一次读取的结果快照。"""

    __slots__ = ("can_do", "important_dates", "plan_date", "segments", "source", "state")

    def __init__(
        self,
        segments: list,
        state: dict,
        can_do: list,
        important_dates: list,
        source: str,
        plan_date: str,
    ) -> None:
        self.segments = segments
        self.state = state
        self.can_do = can_do
        self.important_dates = important_dates
        self.source = source
        self.plan_date = plan_date

    @property
    def from_dependency(self) -> bool:
        return self.source in {"instance", "file"}

    def segment_at(self, minute: int) -> Optional[Segment]:
        for segment in self.segments:
            if segment.contains(minute):
                return segment
        return None

    def next_segment(self, minute: int) -> Optional[Segment]:
        current = self.segment_at(minute)
        if current is None or len(self.segments) < 2:
            return None
        index = self.segments.index(current)
        return self.segments[(index + 1) % len(self.segments)]

    def energy(self) -> int:
        try:
            return max(0, min(100, int(self.state.get("energy") or 70)))
        except (TypeError, ValueError):
            return 70


def normalise_segments(items: Any) -> list:
    """把依赖插件的 plan items 变成首尾相接、覆盖整天的段列表。

    依赖插件的 `end` 是可选的，段之间也允许有空隙（LLM 生成的日程常有），
    这里把空隙并入前一段，让"现在在做什么"永远有答案。
    """
    parsed: list[Segment] = []
    if not isinstance(items, list):
        return parsed
    for item in items:
        if not isinstance(item, dict):
            continue
        start = parse_hhmm(item.get("time"))
        activity = str(item.get("activity") or "").strip()
        if start is None or not activity:
            continue
        end = parse_hhmm(item.get("end"))
        parsed.append(
            Segment(
                start,
                start if end is None else end,
                activity,
                str(item.get("mood") or "").strip(),
                str(item.get("message_seed") or "").strip(),
            )
        )
    if not parsed:
        return parsed
    parsed.sort(key=lambda seg: seg.start)
    # 去掉同一开始分钟的重复段，保留第一条。
    deduped = [parsed[0]]
    for segment in parsed[1:]:
        if segment.start != deduped[-1].start:
            deduped.append(segment)
    if len(deduped) == 1:
        deduped[0].end = deduped[0].start  # 单段视为全天
        return deduped
    for index, segment in enumerate(deduped):
        nxt = deduped[(index + 1) % len(deduped)]
        segment.end = nxt.start
    return deduped


def default_data() -> CompanionData:
    return CompanionData(
        normalise_segments(DEFAULT_PLAN_ITEMS),
        dict(DEFAULT_STATE),
        [],
        [],
        "default",
        "",
    )


def _plan_from_store(store: dict) -> tuple[list, str]:
    plan = store.get("daily_plan")
    if not isinstance(plan, dict):
        return [], ""
    return normalise_segments(plan.get("items")), str(plan.get("date") or "")


def _state_from_store(store: dict) -> dict:
    state = store.get("daily_state")
    merged = dict(DEFAULT_STATE)
    if isinstance(state, dict):
        for key in ("sleep", "hunger", "health", "location", "weather", "mood_bias", "note"):
            value = str(state.get(key) or "").strip()
            if value:
                merged[key] = value
        if state.get("energy") is not None:
            merged["energy"] = state.get("energy")
    return merged


def _can_do_from_store(store: dict) -> list:
    items = store.get("can_do")
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()][:20]


def _dates_from_store(store: dict) -> list:
    entries = store.get("important_dates")
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        title = str(entry.get("title") or "").strip()
        date_text = str(entry.get("date") or "").strip()
        if title and date_text:
            result.append(
                {
                    "title": title,
                    "date": date_text,
                    "note": str(entry.get("note") or "").strip(),
                    "repeat_yearly": bool(entry.get("repeat_yearly")),
                }
            )
    return result[:20]


def days_until(date_text: str, today: Optional[datetime] = None) -> Optional[int]:
    """支持 YYYY-MM-DD 与 MM-DD（按年度重复处理）。"""
    now = today or datetime.now()
    parts = date_text.split("-")
    try:
        if len(parts) == 3:
            target = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
            return (target.date() - now.date()).days
        if len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
            target = datetime(now.year, month, day)
            if target.date() < now.date():
                target = datetime(now.year + 1, month, day)
            return (target.date() - now.date()).days
    except (ValueError, TypeError):
        return None
    return None


class CompanionBridge:
    """读取依赖插件数据，带缓存与降级。"""

    def __init__(self, context: Any = None, data_file: str = "", logger=None) -> None:
        self._context = context
        self._explicit_data_file = data_file.strip()
        self._log = logger
        self._cached: Optional[CompanionData] = None
        self._cached_at = 0.0
        self._file_mtime = 0.0
        self._resolved_instance: Any = None
        self._resolved_at = 0.0
        self._reported_source = ""

    # ---- 实例查找 ----------------------------------------------------

    def _resolve_instance(self) -> Any:
        """在 AstrBot 注册表里找依赖插件的实例。

        只接受已激活（activated）且带 `data` 字典的实例；热重载后旧实例可能
        还在模块表里，注册表指向的才是活的那个。
        """
        now = time.monotonic()
        if self._resolved_instance is not None and now - self._resolved_at < _INSTANCE_TTL_SECS:
            return self._resolved_instance
        self._resolved_at = now
        self._resolved_instance = None
        context = self._context
        if context is None:
            return None
        getter = getattr(context, "get_all_stars", None)
        if not callable(getter):
            return None
        try:
            stars = list(getter() or [])
        except Exception:
            return None
        for metadata in stars:
            if not bool(getattr(metadata, "activated", True)):
                continue
            if not self._metadata_matches(metadata):
                continue
            instance = getattr(metadata, "star_cls", None)
            if instance is None:
                continue
            data = getattr(instance, "data", None)
            if isinstance(data, dict) and "daily_plan" in data:
                self._resolved_instance = instance
                return instance
        return None

    @staticmethod
    def _metadata_matches(metadata: Any) -> bool:
        expected = DEPENDENCY_PLUGIN_NAME.casefold()
        candidates = (
            getattr(metadata, "name", ""),
            getattr(metadata, "root_dir_name", ""),
            getattr(metadata, "module_path", ""),
            getattr(type(getattr(metadata, "star_cls", None)), "__module__", ""),
        )
        for value in candidates:
            text = str(value or "").casefold().replace("-", "_").replace("\\", "/")
            if expected in text.replace("/", "."):
                return True
        return False

    # ---- 文件查找 ----------------------------------------------------

    def _data_file_candidates(self) -> list:
        if self._explicit_data_file:
            return [self._explicit_data_file]
        candidates = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidates.append(
                os.path.join(
                    str(get_astrbot_data_path()),
                    "plugin_data",
                    DEPENDENCY_PLUGIN_NAME,
                    DATA_FILE_NAME,
                )
            )
        except Exception:
            pass
        # 无法 import AstrBot 时（例如单元测试）退回常见相对位置。
        candidates.append(
            os.path.join("data", "plugin_data", DEPENDENCY_PLUGIN_NAME, DATA_FILE_NAME)
        )
        return candidates

    def _read_file_store(self) -> Optional[dict]:
        for path in self._data_file_candidates():
            try:
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8") as stream:
                    store = json.load(stream)
                if isinstance(store, dict):
                    self._file_mtime = mtime
                    return store
            except (OSError, ValueError) as exc:
                if self._log is not None:
                    self._log.warning(
                        f"[SynctusCompanion] 读取依赖插件数据失败 ({path}): {exc}"
                    )
        return None

    # ---- 对外接口 ----------------------------------------------------

    def load(self, force: bool = False) -> CompanionData:
        now = time.monotonic()
        if (
            not force
            and self._cached is not None
            and now - self._cached_at < _FILE_TTL_SECS
        ):
            return self._cached

        data = self._load_uncached()
        self._cached = data
        self._cached_at = now
        if data.source != self._reported_source:
            self._reported_source = data.source
            if self._log is not None:
                if data.source == "instance":
                    self._log.info(
                        "[SynctusCompanion] 已联动 astrbot_plugin_private_companion"
                        f"（运行实例，日程 {len(data.segments)} 段）"
                    )
                elif data.source == "file":
                    self._log.info(
                        "[SynctusCompanion] 已联动 astrbot_plugin_private_companion"
                        f"（companions.json，日程 {len(data.segments)} 段）"
                    )
                else:
                    self._log.warning(
                        "[SynctusCompanion] 未读到 astrbot_plugin_private_companion 的日程，"
                        "使用内置默认作息"
                    )
        return data

    def _load_uncached(self) -> CompanionData:
        instance = self._resolve_instance()
        if instance is not None:
            store = getattr(instance, "data", None)
            if isinstance(store, dict):
                segments, plan_date = _plan_from_store(store)
                if segments:
                    return CompanionData(
                        segments,
                        _state_from_store(store),
                        _can_do_from_store(store),
                        _dates_from_store(store),
                        "instance",
                        plan_date,
                    )
        store = self._read_file_store()
        if store is not None:
            segments, plan_date = _plan_from_store(store)
            if segments:
                return CompanionData(
                    segments,
                    _state_from_store(store),
                    _can_do_from_store(store),
                    _dates_from_store(store),
                    "file",
                    plan_date,
                )
        return default_data()

    def bot_name(self) -> str:
        """依赖插件配置里的 Bot 名字，读不到时返回空串。"""
        instance = self._resolve_instance()
        for attr in ("bot_name", "_bot_name"):
            value = str(getattr(instance, attr, "") or "").strip()
            if value:
                return value
        config = getattr(instance, "config", None)
        if isinstance(config, dict):
            basic = config.get("basic_config")
            if isinstance(basic, dict):
                value = str(basic.get("bot_name") or "").strip()
                if value:
                    return value
            value = str(config.get("bot_name") or "").strip()
            if value:
                return value
        return ""
