"""模拟 Bot 那台"手机"的电量。

真机电量是一条有物理意义的曲线：夜里插着电充满，白天按使用强度掉，掉到没电
之前会去充。这里用同样的规则从今天的日程积分出来，而不是每次采样随机取数——
随机数会让对端看到电量上下乱跳，一眼假。

结果只依赖「日期 + 日程」，因此插件重启、多次读取都得到同一条曲线。
"""

from __future__ import annotations

import hashlib
from typing import Optional

# 每小时耗电百分比。睡觉时手机放着不动，摸鱼刷视频最费电。
DRAIN_SLEEP = 0.6
DRAIN_FOCUS = 2.2
DRAIN_IDLE = 4.0
DRAIN_LEISURE = 8.5

CHARGE_PER_HOUR = 32.0  # 约 3 小时充满，和普通快充差不多
CHARGE_TARGET = 100.0
CHARGE_TRIGGER = 18.0  # 白天低于这个百分比就会去找充电器

# 每天的耗电快慢略有不同：同一份日程在不同日子不该给出一模一样的曲线。
DRAIN_JITTER_RANGE = (0.85, 1.15)

_LEISURE_WORDS = ("摸鱼", "刷", "视频", "游戏", "休息", "放松", "自由", "散步", "逛")


def _daily_seed(date_key: str) -> float:
    """按日期取一个稳定的 0~1 抖动，让每天的起始电量与耗电略有不同。"""
    digest = hashlib.sha256(date_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def _drain_rate(segment) -> float:
    if segment is None:
        return DRAIN_IDLE
    activity = segment.activity
    if segment.sleeping:
        return DRAIN_SLEEP
    if segment.focusing:
        return DRAIN_FOCUS
    if any(word in activity for word in _LEISURE_WORDS):
        return DRAIN_LEISURE
    return DRAIN_IDLE


class BatterySimulator:
    """按分钟积分出一整天的电量曲线，按日期缓存。"""

    def __init__(self) -> None:
        self._date_key = ""
        self._curve: list = []

    def _build(self, segments: list, date_key: str) -> list:
        seed = _daily_seed(date_key)
        # 00:00 的电量：睡前通常没充满，给一个偏中间的区间。
        percent = 42.0 + seed * 38.0
        low, high = DRAIN_JITTER_RANGE
        drain_scale = low + seed * (high - low)
        charging = False
        curve: list = []
        for minute in range(1440):
            segment = None
            for candidate in segments:
                if candidate.contains(minute):
                    segment = candidate
                    break
            sleeping = segment is not None and segment.sleeping

            if sleeping:
                # 睡觉时手机插着充电器：没充满就在充，充满了也仍然插着。
                charging = True
                percent = min(CHARGE_TARGET, percent + CHARGE_PER_HOUR / 60.0)
            elif charging:
                percent += CHARGE_PER_HOUR / 60.0
                if percent >= CHARGE_TARGET:
                    percent = CHARGE_TARGET
                    charging = False  # 白天充满即拔
            else:
                percent -= _drain_rate(segment) * drain_scale / 60.0
                if percent <= CHARGE_TRIGGER:
                    charging = True
            percent = max(1.0, min(CHARGE_TARGET, percent))
            curve.append((int(round(percent)), charging))
        return curve

    def sample(self, segments: list, date_key: str, minute: int) -> dict:
        if date_key != self._date_key or not self._curve:
            self._curve = self._build(segments, date_key)
            self._date_key = date_key
        percent, charging = self._curve[minute % 1440]
        return {"percent": percent, "charging": charging, "minutes_left": self._minutes_left(minute)}

    def _minutes_left(self, minute: int) -> Optional[int]:
        """还能用多久：沿曲线往前走到需要充电的那一刻。

        不要用"当前掉电速度 × 剩余电量"外推——专注时每小时只掉 2%，
        那样会算出「还能用 45 小时」这种一眼假的数字。真机报的是按当前
        使用情况的估计，而我们恰好有整条曲线，直接查即可。

        充电时返回 None，与真机上报一致。
        """
        if not self._curve:
            return None
        if self._curve[minute % 1440][1]:
            return None
        for ahead in range(1, 1441):
            percent, charging = self._curve[(minute + ahead) % 1440]
            if charging or percent <= CHARGE_TRIGGER:
                return ahead
        return None
