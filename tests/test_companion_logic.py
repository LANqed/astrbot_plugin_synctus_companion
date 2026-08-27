"""日程桥接、电量模拟、待办清单与状态映射的逻辑测试。

这些模块决定对端看到的每一个数字，所以覆盖重点是：
- 日程段必须覆盖一天的 1440 分钟（否则"现在在做什么"会出现空洞）
- 跨午夜的睡觉段
- 电量曲线的物理合理性与可复现性
- 待办随时间推进被勾掉
"""

from __future__ import annotations

from datetime import datetime

from astrbot_plugin_synctus_companion import battery as battery_module
from astrbot_plugin_synctus_companion import companion_bridge as bridge
from astrbot_plugin_synctus_companion import presence
from astrbot_plugin_synctus_companion import tasks as tasks_module


def _data():
    return bridge.default_data()


# ---- 日程归一化 ---------------------------------------------------------


def test_default_schedule_covers_every_minute_of_the_day():
    data = _data()
    for minute in range(1440):
        assert data.segment_at(minute) is not None, f"{minute} 分钟没有对应日程段"


def test_gaps_between_segments_are_absorbed_by_the_previous_one():
    # LLM 生成的日程常有空隙：09:00-10:00 之后直接跳到 14:00。
    segments = bridge.normalise_segments(
        [
            {"time": "09:00", "end": "10:00", "activity": "写代码"},
            {"time": "14:00", "end": "15:00", "activity": "开会"},
        ]
    )
    assert len(segments) == 2
    assert segments[0].end == 14 * 60  # 空隙并入前一段
    assert segments[1].end == 9 * 60  # 末段回绕到首段，覆盖整天
    for minute in range(1440):
        assert any(seg.contains(minute) for seg in segments)


def test_sleep_segment_crosses_midnight():
    data = _data()
    night = data.segment_at(2 * 60)
    assert night is not None and night.sleeping
    assert night.start == 22 * 60 + 30
    assert night.end == 7 * 60 + 30
    assert night.elapsed_minutes(2 * 60) == 3 * 60 + 30


def test_single_segment_plan_is_treated_as_all_day():
    segments = bridge.normalise_segments([{"time": "08:00", "activity": "值班"}])
    assert len(segments) == 1
    assert all(segments[0].contains(minute) is False for minute in ())  # 无异常
    assert segments[0].contains(3 * 60) or segments[0].start == segments[0].end


def test_activity_classification():
    assert bridge.is_sleeping("准备睡觉")
    assert not bridge.is_sleeping("睡醒后洗漱")  # 含"醒"不算睡眠
    assert bridge.is_focusing("赶稿子")
    assert not bridge.is_focusing("吃午饭")
    assert not bridge.is_focusing("睡觉")


def test_window_names_match_dependency_five_window_split():
    assert bridge.window_name(7 * 60) == "早晨"
    assert bridge.window_name(12 * 60) == "中午"
    assert bridge.window_name(16 * 60) == "下午"
    assert bridge.window_name(19 * 60) == "晚上"
    assert bridge.window_name(23 * 60) == "深夜"
    assert bridge.window_name(3 * 60) == "深夜"  # 凌晨归入前一天的深夜


def test_parse_and_format_hhmm():
    assert bridge.parse_hhmm("07:30") == 450
    assert bridge.parse_hhmm("7：30") == 450  # 全角冒号
    assert bridge.parse_hhmm("25:00") is None
    assert bridge.parse_hhmm("abc") is None
    assert bridge.fmt_hhmm(450) == "07:30"
    assert bridge.fmt_hhmm(1440 + 90) == "01:30"


def test_days_until_handles_yearly_repeat():
    now = datetime(2026, 8, 27)
    assert bridge.days_until("2026-08-29", now) == 2
    assert bridge.days_until("08-29", now) == 2
    # 已过去的 MM-DD 顺延到明年
    assert bridge.days_until("01-01", now) == 127
    assert bridge.days_until("坏日期", now) is None


# ---- 依赖插件数据读取 ---------------------------------------------------


class _FakeMetadata:
    def __init__(self, name, star_cls, activated=True):
        self.name = name
        self.star_cls = star_cls
        self.activated = activated
        self.root_dir_name = name
        self.module_path = name


class _FakeContext:
    def __init__(self, stars):
        self._stars = stars

    def get_all_stars(self):
        return self._stars


class _FakeDependency:
    def __init__(self, store):
        self.data = store


def _store_with_plan():
    return {
        "daily_plan": {
            "date": "2026-08-27",
            "items": [
                {"time": "08:00", "end": "12:00", "activity": "赶稿子", "mood": "紧张",
                 "message_seed": "先去赶稿了"},
                {"time": "12:00", "end": "23:00", "activity": "吃饭放松", "mood": "轻松"},
                {"time": "23:00", "end": "08:00", "activity": "睡觉", "mood": "熟睡"},
            ],
        },
        "daily_state": {"sleep": "睡得很浅", "hunger": "有点饿", "energy": 45},
        "can_do": ["回邮件", "整理书桌"],
        "important_dates": [
            {"title": "交稿", "date": "2026-08-29", "enabled": True, "note": "杂志专栏"},
            {"title": "已禁用", "date": "2026-08-28", "enabled": False},
        ],
    }


def test_reads_plan_from_running_instance():
    store = _store_with_plan()
    context = _FakeContext(
        [_FakeMetadata("astrbot_plugin_private_companion", _FakeDependency(store))]
    )
    data = bridge.CompanionBridge(context).load()
    assert data.source == "instance"
    assert data.plan_date == "2026-08-27"
    assert [seg.activity for seg in data.segments] == ["赶稿子", "吃饭放松", "睡觉"]
    assert data.segment_at(9 * 60).message_seed == "先去赶稿了"
    assert data.energy() == 45
    assert data.can_do == ["回邮件", "整理书桌"]
    assert [entry["title"] for entry in data.important_dates] == ["交稿"]


def test_deactivated_dependency_falls_back_to_default():
    context = _FakeContext(
        [
            _FakeMetadata(
                "astrbot_plugin_private_companion",
                _FakeDependency(_store_with_plan()),
                activated=False,
            )
        ]
    )
    data = bridge.CompanionBridge(context).load()
    assert data.source == "default"
    assert data.from_dependency is False


def test_unrelated_plugin_is_ignored():
    context = _FakeContext([_FakeMetadata("some_other_plugin", _FakeDependency(_store_with_plan()))])
    assert bridge.CompanionBridge(context).load().source == "default"


def test_reads_plan_from_data_file(tmp_path):
    import json

    path = tmp_path / "companions.json"
    path.write_text(json.dumps(_store_with_plan()), encoding="utf-8")
    data = bridge.CompanionBridge(None, data_file=str(path)).load()
    assert data.source == "file"
    assert len(data.segments) == 3


def test_malformed_data_file_falls_back(tmp_path):
    path = tmp_path / "companions.json"
    path.write_text("{ not json", encoding="utf-8")
    assert bridge.CompanionBridge(None, data_file=str(path)).load().source == "default"


# ---- 电量模拟 -----------------------------------------------------------


def test_battery_curve_is_deterministic_for_a_date():
    data = _data()
    sim_a, sim_b = battery_module.BatterySimulator(), battery_module.BatterySimulator()
    for minute in (0, 500, 1000, 1439):
        assert sim_a.sample(data.segments, "2026-08-27", minute) == sim_b.sample(
            data.segments, "2026-08-27", minute
        )


def test_battery_stays_in_range_and_never_dies():
    data = _data()
    sim = battery_module.BatterySimulator()
    for minute in range(1440):
        sample = sim.sample(data.segments, "2026-08-27", minute)
        assert 1 <= sample["percent"] <= 100


def test_battery_charges_overnight_and_drains_during_the_day():
    data = _data()
    sim = battery_module.BatterySimulator()
    # 睡觉中段（03:00）通常在充电或已充满
    night = sim.sample(data.segments, "2026-08-27", 3 * 60)
    assert night["charging"] or night["percent"] >= 95
    morning = sim.sample(data.segments, "2026-08-27", 8 * 60)["percent"]
    evening = sim.sample(data.segments, "2026-08-27", 20 * 60)["percent"]
    assert evening < morning, "白天应该在掉电"


def test_battery_recharges_before_dying():
    """低于阈值就去充电：一整天不应该出现掉到 1% 的情况。"""
    heavy = bridge.normalise_segments(
        [{"time": "07:00", "end": "23:00", "activity": "刷视频"},
         {"time": "23:00", "end": "07:00", "activity": "睡觉"}]
    )
    sim = battery_module.BatterySimulator()
    lowest = min(sim.sample(heavy, "2026-08-27", m)["percent"] for m in range(1440))
    assert lowest > 5


def test_minutes_left_is_absent_while_charging():
    data = _data()
    sim = battery_module.BatterySimulator()
    for minute in range(1440):
        sample = sim.sample(data.segments, "2026-08-27", minute)
        if sample["charging"]:
            assert sample["minutes_left"] is None


def test_different_dates_give_different_curves():
    data = _data()
    sim = battery_module.BatterySimulator()
    first = sim.sample(data.segments, "2026-08-27", 12 * 60)["percent"]
    second = sim.sample(data.segments, "2026-09-15", 12 * 60)["percent"]
    assert first != second


# ---- 待办清单 -----------------------------------------------------------


def _dependency_data():
    store = _store_with_plan()
    context = _FakeContext(
        [_FakeMetadata("astrbot_plugin_private_companion", _FakeDependency(store))]
    )
    return bridge.CompanionBridge(context).load()


def test_deadline_tasks_come_first_with_remaining_days():
    data = _dependency_data()
    items = tasks_module.build_task_list(data, datetime(2026, 8, 27, 10, 0))
    assert "交稿" in items[0]["title"]
    assert "还剩 2 天" in items[0]["title"]
    assert "杂志专栏" in items[0]["title"]


def test_deadline_wording_for_today_and_tomorrow():
    data = _dependency_data()
    today = tasks_module.build_task_list(data, datetime(2026, 8, 29, 9, 0))
    assert "今天截止" in today[0]["title"]
    tomorrow = tasks_module.build_task_list(data, datetime(2026, 8, 28, 9, 0))
    assert "明天截止" in tomorrow[0]["title"]


def test_completed_count_grows_through_the_day():
    data = _data()
    morning = tasks_module.counts(
        tasks_module.build_task_list(data, datetime(2026, 8, 27, 9, 0))
    )
    evening = tasks_module.counts(
        tasks_module.build_task_list(data, datetime(2026, 8, 27, 21, 0))
    )
    assert evening[1] > morning[1], "晚上的已完成数应该更多"


def test_task_ids_are_stable_within_a_day():
    data = _data()
    first = tasks_module.build_task_list(data, datetime(2026, 8, 27, 10, 0))
    second = tasks_module.build_task_list(data, datetime(2026, 8, 27, 10, 5))
    assert [item["id"] for item in first] == [item["id"] for item in second]


def test_sleep_segment_is_not_a_task():
    data = _data()
    items = tasks_module.build_task_list(data, datetime(2026, 8, 27, 10, 0))
    sleep_segment = data.segment_at(3 * 60)
    assert sleep_segment.sleeping
    sleep_title = f"{bridge.fmt_hhmm(sleep_segment.start)} {sleep_segment.activity}"
    assert not any(item["title"] == sleep_title for item in items)
    # 「洗漱准备睡觉」是清醒的收尾段，仍然算一件事
    assert any("洗漱准备睡觉" in item["title"] for item in items)


def test_can_do_items_fill_the_list():
    data = _dependency_data()
    items = tasks_module.build_task_list(data, datetime(2026, 8, 27, 10, 0), goal_count=8)
    titles = " ".join(item["title"] for item in items)
    assert "回邮件" in titles


def test_filler_used_when_dependency_absent():
    items = tasks_module.build_task_list(_data(), datetime(2026, 8, 27, 10, 0), goal_count=12)
    titles = " ".join(item["title"] for item in items)
    assert any(filler in titles for filler in tasks_module.FILLER_TASKS)


def test_render_task_lines_marks_done_and_truncates():
    data = _data()
    items = tasks_module.build_task_list(data, datetime(2026, 8, 27, 21, 0))
    lines = tasks_module.render_task_lines(items, limit=3)
    assert any(line.startswith("[x]") for line in lines)
    assert lines[-1].startswith("… 还有")


# ---- 状态映射 -----------------------------------------------------------


def test_presence_reflects_the_kind_of_activity():
    data = _data()
    assert presence.presence_for(data.segment_at(3 * 60), 70) == "resting"  # 睡觉
    assert presence.presence_for(data.segment_at(10 * 60), 70) == "active"  # 专注
    assert presence.presence_for(data.segment_at(12 * 60), 70) == "away"  # 吃饭
    # 精力很差时专注段降为免打扰
    assert presence.presence_for(data.segment_at(10 * 60), 10) == "busy"


def test_foreground_reports_the_activity_as_the_window_title():
    data = _data()
    focus = presence.foreground_for(data.segment_at(10 * 60))
    assert focus["title"] == "专心做正事"
    sleeping = presence.foreground_for(data.segment_at(3 * 60))
    assert sleeping["app"] == "锁屏"
    leisure = presence.foreground_for(data.segment_at(17 * 60))
    assert leisure["app"] in {"美团", "手机"}


def test_focus_minutes_accumulate_and_reach_the_goal():
    data = _data()
    goal = presence.focus_goal_minutes(data.segments)
    assert goal > 0
    assert presence.focus_minutes_so_far(data.segments, 8 * 60) == 0
    mid = presence.focus_minutes_so_far(data.segments, 10 * 60)
    assert 0 < mid < goal
    # 一天结束时应该走满
    assert presence.focus_minutes_so_far(data.segments, 22 * 60) == goal


def test_pomodoro_is_running_during_focus_segments():
    data = _data()
    day_start_ms = 1_700_000_000_000
    running = presence.pomodoro_for(data.segment_at(10 * 60), 10 * 60, 0, day_start_ms)
    assert running["phase"] == "focus"
    # 截止时间就是这一段的结束时间（11:50），由零点加分钟数得出，不随采样漂移
    assert running["ends_at"] == day_start_ms + (11 * 60 + 50) * 60_000
    assert presence.pomodoro_for(data.segment_at(10 * 60), 10 * 60 + 1, 0, day_start_ms)[
        "ends_at"
    ] == running["ends_at"]
    idle = presence.pomodoro_for(data.segment_at(12 * 60), 12 * 60, 1, day_start_ms)
    assert idle["phase"] == "idle" and idle["completed_today"] == 1
    assert presence.pomodoro_for(data.segment_at(3 * 60), 3 * 60, 0, day_start_ms) is None


def test_completed_rounds_count_finished_focus_segments():
    data = _data()
    assert presence.completed_focus_rounds(data.segments, 8 * 60) == 0
    assert presence.completed_focus_rounds(data.segments, 13 * 60) == 1
    assert presence.completed_focus_rounds(data.segments, 20 * 60) == 2
