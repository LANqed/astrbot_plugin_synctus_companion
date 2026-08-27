"""把日程段翻译成 Synctus 的状态字段。

对端的悬浮窗/通知栏读的是 presence、foreground、pomodoro、focus_today_min
这几个字段，所以 Bot 的"正在做什么"必须落到这些字段上，而不是只写在文本里：

- 睡觉段 → `resting` + 前台"锁屏"，对端看到「休息中」
- 专注段 → `active` + 番茄钟处于 focus，对端看到「在忙 · 🍅专注」，
  从而「一起专注」和「别摸鱼了」这些按钮对 Bot 也有意义
- 吃饭/通勤 → `away`，人不在键盘前
- 摸鱼段 → `active` + 前台是娱乐 App，对端能看到 Bot 也在刷视频

专注分钟数从日程积分而来：Bot 没有本地番茄钟，它的专注时长就是今天已经
走过的专注段时长。这条数字与 goal_min 一起决定对端看到的进度条。
"""

from __future__ import annotations

from typing import Optional

from .synctus import model

# 前台"应用"：Bot 那台手机上正在开着什么。按活动关键词匹配，
# 匹配不到时用一个中性的占位，避免谎报具体应用。
_APP_RULES = (
    (("睡", "梦", "被窝", "熄灯"), "锁屏", "手机在充电"),
    (("洗漱", "起床", "刷牙"), "锁屏", "刚起床"),
    (("吃", "饭", "餐", "干饭", "食堂", "外卖"), "美团", "在点吃的"),
    (("刷", "视频", "b站", "bilibili"), "bilibili", None),
    (("游戏", "打游戏", "练级"), "游戏", None),
    (("看书", "读", "小说", "文章", "报纸"), "微信读书", None),
    (("音乐", "听歌"), "网易云音乐", None),
    (("代码", "编程", "调试", "写程序"), "VS Code", None),
    (("写", "稿", "论文", "作业", "投稿", "整理"), "备忘录", None),
    (("上课", "学习", "复习", "背", "刷题"), "笔记", None),
    (("散步", "走", "逛", "出门", "路上", "通勤"), "地图", None),
    (("聊", "消息", "回复"), "QQ", None),
    (("画", "创作"), "画板", None),
)

_AWAY_WORDS = ("吃", "饭", "餐", "干饭", "食堂", "外卖", "散步", "出门", "逛", "路上", "通勤", "洗漱", "洗澡")


def foreground_for(segment) -> dict:
    """给日程段挑一个前台应用。title 用日程活动本身，等于告诉对端在干嘛。"""
    if segment is None:
        return model.foreground_app("锁屏", name="锁屏")
    activity = segment.activity
    for words, app, override_title in _APP_RULES:
        if any(word in activity for word in words):
            return model.foreground_app(app, name=app, title=override_title or activity)
    return model.foreground_app("手机", name="手机", title=activity)


def presence_for(segment, energy: int) -> str:
    if segment is None:
        return model.PRESENCE_AWAY
    if segment.sleeping:
        return model.PRESENCE_RESTING
    if any(word in segment.activity for word in _AWAY_WORDS):
        return model.PRESENCE_AWAY
    if segment.focusing:
        # 专注段保持 active：Bot 在忙，但允许对端戳它。
        # 免打扰（busy）只留给精力很差的时候，避免 Bot 长期不可打扰。
        return model.PRESENCE_BUSY if energy < 25 else model.PRESENCE_ACTIVE
    return model.PRESENCE_ACTIVE


def focus_minutes_so_far(segments: list, minute: int) -> int:
    """今天到现在为止，日程里的专注段累计了多少分钟。"""
    total = 0
    for segment in segments:
        if not segment.focusing:
            continue
        length = (segment.end - segment.start) % 1440 or 1440
        start = segment.start
        # 只统计已经走过的部分；跨午夜段按今天已过的那一截计。
        if segment.contains(minute):
            total += segment.elapsed_minutes(minute)
        elif start < minute:
            total += length
    return total


def focus_goal_minutes(segments: list) -> int:
    """把今天全部专注段的总时长当作目标，于是进度条在一天结束时正好走满。"""
    total = 0
    for segment in segments:
        if segment.focusing:
            total += (segment.end - segment.start) % 1440 or 1440
    return total


def pomodoro_for(
    segment, minute: int, completed_today: int, day_start_ms: int
) -> Optional[dict]:
    """专注段映射成一个正在跑的番茄钟，截止时间就是这一段的结束时间。

    只同步截止时间戳，不同步倒计时——对端本地插值即可，
    这也是 Synctus 番茄钟的既有设计。

    `ends_at` 由当天零点加分钟数算出，而不是"现在 + 剩余"：后者每次采样都会
    漂移几毫秒，会让状态快照每个 tick 都"变化"从而反复重发。
    """
    if segment is None or not segment.focusing:
        if completed_today > 0:
            return model.pomodoro_state(model.PHASE_IDLE, completed_today=completed_today)
        return None
    # minute + remaining 超过 1440 时正好落到次日，跨午夜的段也对。
    end_minute = minute + segment.remaining_minutes(minute)
    return model.pomodoro_state(
        model.PHASE_FOCUS,
        ends_at=day_start_ms + end_minute * 60_000,
        round_index=completed_today + 1,
        completed_today=completed_today,
    )


def completed_focus_rounds(segments: list, minute: int) -> int:
    """已经结束的专注段数量，作为"今日完成回合数"。"""
    count = 0
    for segment in segments:
        if segment.focusing and not segment.contains(minute) and segment.start < minute:
            count += 1
    return count
