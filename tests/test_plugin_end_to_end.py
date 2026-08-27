"""插件端到端测试：用 AstrBot 的最小替身驱动 main.py。

覆盖的是"陪伴"本身：日程切换会不会说话、额度与免打扰是否生效、
对端的敲一敲有没有转达、状态是否真的发到了中继。

AstrBot 不是 pip 包，所以这里注入替身模块。替身只提供插件实际用到的接口：
Star / Context.send_message / get_all_stars / StarTools.get_data_dir /
filter.command / MessageChain / Plain。
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- AstrBot 替身 -------------------------------------------------------


class _Star:
    def __init__(self, context):
        self.context = context


class _FakeContext:
    """记录所有发出的消息，并按需暴露依赖插件实例。"""

    def __init__(self, stars=None):
        self.sent: list = []
        self._stars = stars or []

    async def send_message(self, umo, chain):
        self.sent.append((umo, "".join(part[1] for part in chain)))
        return True

    def get_all_stars(self):
        return self._stars


def _install_astrbot_stub(tmp_path: Path) -> None:
    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    module("astrbot")
    module("astrbot.api", AstrBotConfig=dict, logger=logger)
    module(
        "astrbot.api.event",
        AstrMessageEvent=object,
        MessageChain=lambda chain: chain,
        filter=types.SimpleNamespace(command=lambda *a, **k: (lambda f: f)),
    )
    module("astrbot.api.message_components", Plain=lambda text: ("plain", text))
    module(
        "astrbot.api.star",
        Context=object,
        Star=_Star,
        StarTools=types.SimpleNamespace(get_data_dir=lambda name: tmp_path / name),
    )
    module("astrbot.core")
    module("astrbot.core.utils")
    module(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: str(tmp_path / "data"),
    )


@pytest.fixture
def plugin_module(tmp_path):
    _install_astrbot_stub(tmp_path)
    for name in list(sys.modules):
        if name.startswith("astrbot_plugin_synctus_companion"):
            del sys.modules[name]
    module = importlib.import_module("astrbot_plugin_synctus_companion.main")
    yield module
    for name in list(sys.modules):
        if name.startswith("astrbot") :
            del sys.modules[name]


# ---- 依赖插件替身 -------------------------------------------------------


class _FakeDependencyMetadata:
    def __init__(self, store):
        self.name = "astrbot_plugin_private_companion"
        self.root_dir_name = self.name
        self.module_path = self.name
        self.activated = True
        self.star_cls = types.SimpleNamespace(data=store, config={"basic_config": {"bot_name": "小澈"}})


def _dependency_store():
    return {
        "daily_plan": {
            "date": "2026-08-27",
            "items": [
                {"time": "07:30", "end": "09:00", "activity": "起床洗漱",
                 "mood": "迷糊", "message_seed": "早安～刚爬起来"},
                {"time": "09:00", "end": "12:00", "activity": "赶稿子",
                 "mood": "紧张", "message_seed": "我去赶稿啦"},
                {"time": "12:00", "end": "13:00", "activity": "吃午饭", "mood": "满足"},
                {"time": "13:00", "end": "23:00", "activity": "写下午的部分",
                 "mood": "专注"},
                {"time": "23:00", "end": "07:30", "activity": "睡觉", "mood": "熟睡"},
            ],
        },
        "daily_state": {"sleep": "睡得不错", "hunger": "有点饿", "energy": 66},
        "can_do": ["回邮件"],
        "important_dates": [
            {"title": "交稿", "date": "2026-08-29", "enabled": True, "note": "专栏"}
        ],
    }


def _config(**overrides) -> dict:
    config = {
        "bot_name": "",
        "target_users": ["10001"],
        "dependency_data_file": "",
        "synctus": {
            "enable_synctus": False,
            "server": "127.0.0.1:8787",
            "invite_code": "",
            "tls": False,
            "tls_verify": False,
            "synctus_user": "",
            "device_name": "手机",
            "share_activity": True,
            "share_battery": True,
            "share_tasks": True,
            "task_count": 8,
            "forward_nudges": True,
        },
        "companion": {
            "enable_transition": True,
            "enable_idle_chat": False,
            "idle_chance_per_minute": 0.0,
            "max_daily_messages": 12,
            "min_gap_minutes": 45,
            "quiet_hours": "23:00-08:00",
        },
    }
    for key, value in overrides.items():
        if key in config["synctus"]:
            config["synctus"][key] = value
        elif key in config["companion"]:
            config["companion"][key] = value
        else:
            config[key] = value
    return config


def _make_plugin(plugin_module, *, with_dependency=True, **overrides):
    stars = [_FakeDependencyMetadata(_dependency_store())] if with_dependency else []
    context = _FakeContext(stars)
    plugin = plugin_module.SynctusCompanionPlugin(context, _config(**overrides))
    return plugin, context


# ---- 配置读取 -----------------------------------------------------------


def test_nested_config_groups_are_readable(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    assert plugin._cfg_bool("enable_transition", False) is True
    assert plugin._cfg_int("max_daily_messages", 0) == 12
    assert plugin._cfg_str("device_name") == "手机"
    assert plugin._cfg_float("idle_chance_per_minute", 1.0) == 0.0


def test_bot_name_falls_back_to_dependency_config(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    assert plugin._bot_name() == "小澈"
    plugin_no_dep, _ = _make_plugin(plugin_module, with_dependency=False)
    assert plugin_no_dep._bot_name() == plugin_module.DEFAULT_BOT_NAME


def test_target_umo_accepts_bare_qq_and_full_session(plugin_module):
    plugin, _ = _make_plugin(
        plugin_module, target_users=["10001", "qq_official:FriendMessage:openid", ""]
    )
    assert plugin._target_umos() == [
        "aiocqhttp:FriendMessage:10001",
        "qq_official:FriendMessage:openid",
    ]


# ---- 文本渲染 -----------------------------------------------------------


def test_status_text_includes_activity_battery_and_tasks(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    text = plugin._render_status("aiocqhttp:FriendMessage:10001", datetime(2026, 8, 27, 10, 0))
    assert "赶稿子" in text
    assert "手机 " in text and "%" in text
    assert "今天专注" in text
    assert "待办" in text
    assert "Synctus 上报：已关闭" in text


def test_schedule_text_marks_the_dependency_as_the_source(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    text = plugin._render_schedule(datetime(2026, 8, 27, 10, 0))
    assert "astrbot_plugin_private_companion" in text
    assert "2026-08-27" in text
    assert "<- 现在" in text
    plugin_no_dep, _ = _make_plugin(plugin_module, with_dependency=False)
    assert "内置默认作息" in plugin_no_dep._render_schedule(datetime(2026, 8, 27, 10, 0))


def test_task_text_shows_deadline_pressure(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    text = plugin._render_tasks(datetime(2026, 8, 27, 10, 0))
    assert "交稿" in text and "还剩 2 天" in text
    assert "盯着的日子" in text


def test_battery_subcommand(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    text = plugin._handle_sub_command("电量", "")
    assert "手机" in text and "%" in text


def test_help_lists_subcommands(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    text = plugin._handle_sub_command("帮助", "")
    for expected in ("陪伴 状态", "陪伴 日程", "陪伴 待办", "陪伴 电量"):
        assert expected in text


# ---- 主动陪伴 -----------------------------------------------------------


def _tick(plugin, now: datetime) -> None:
    asyncio.run(plugin._tick_qq(plugin._bridge.load(), now))


def test_transition_message_uses_dependency_message_seed(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    # 第一次观察只记录所处段，不打扰（避免每次重载都发消息）
    _tick(plugin, datetime(2026, 8, 27, 8, 0))
    assert context.sent == []

    # 切换到「赶稿子」：排入待发消息，然后把时间拨到抖动之后
    _tick(plugin, datetime(2026, 8, 27, 9, 30))
    state = plugin._user_state(umo)
    assert state["pending_msg"] == "我去赶稿啦"
    state["pending_at"] = time.time() - 1
    _tick(plugin, datetime(2026, 8, 27, 9, 35))
    assert context.sent == [(umo, "我去赶稿啦")]


def test_transition_message_is_generated_when_seed_is_absent(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    _tick(plugin, datetime(2026, 8, 27, 9, 30))
    _tick(plugin, datetime(2026, 8, 27, 12, 30))  # 吃午饭段没有 message_seed
    state = plugin._user_state(umo)
    state["pending_at"] = time.time() - 1
    _tick(plugin, datetime(2026, 8, 27, 12, 35))
    assert any("吃午饭" in text for _umo, text in context.sent)


def test_sleep_transition_says_good_night(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    _tick(plugin, datetime(2026, 8, 27, 22, 0))
    _tick(plugin, datetime(2026, 8, 27, 23, 10))
    state = plugin._user_state(umo)
    state["pending_at"] = time.time() - 1
    _tick(plugin, datetime(2026, 8, 27, 23, 15))
    assert any("晚安" in text for _umo, text in context.sent)


def test_daily_quota_stops_further_messages(plugin_module):
    plugin, context = _make_plugin(plugin_module, max_daily_messages=1)
    umo = "aiocqhttp:FriendMessage:10001"
    state = plugin._user_state(umo)
    state["sent_today"] = 1
    state["date"] = datetime.now().strftime("%Y-%m-%d")
    _tick(plugin, datetime(2026, 8, 27, 9, 0))
    _tick(plugin, datetime(2026, 8, 27, 9, 30))
    assert context.sent == []


def test_muted_user_gets_no_proactive_message(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    assert "安静" in plugin._handle_sub_command("静音", umo)
    _tick(plugin, datetime(2026, 8, 27, 9, 0))
    _tick(plugin, datetime(2026, 8, 27, 9, 30))
    assert context.sent == []
    assert "回来" in plugin._handle_sub_command("恢复", umo)
    assert plugin._user_state(umo)["muted"] is False


def test_idle_chat_respects_quiet_hours_and_focus(plugin_module):
    plugin, _ = _make_plugin(
        plugin_module, enable_idle_chat=True, idle_chance_per_minute=1.0
    )
    state = plugin._user_state("aiocqhttp:FriendMessage:10001")
    # 免打扰时段内不随机搭话
    assert plugin._gate(state, idle=True, now=datetime(2026, 8, 27, 23, 30)) is False
    assert plugin._gate(state, idle=True, now=datetime(2026, 8, 27, 12, 30)) is True
    # 专注段本身不可打扰
    data = plugin._bridge.load()
    assert data.segment_at(10 * 60).interruptible is False
    assert data.segment_at(12 * 60 + 30).interruptible is True


def test_state_persists_across_restart(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    plugin._handle_sub_command("静音", umo)
    device_id = plugin._state["device_id"]

    revived, _ = _make_plugin(plugin_module)
    assert revived._state["device_id"] == device_id, "设备标识必须稳定，否则对端会看到新设备"
    assert revived._user_state(umo)["muted"] is True


# ---- nudge 转达 ---------------------------------------------------------


def test_nudge_is_forwarded_with_current_activity(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    asyncio.run(plugin._handle_nudge({"kind": "knock", "from_name": "我", "at": 0}))
    assert len(context.sent) == 1
    text = context.sent[0][1]
    assert "我" in text and "正在" in text


def test_nag_breaks_through_mute_but_knock_does_not(plugin_module):
    plugin, context = _make_plugin(plugin_module)
    umo = "aiocqhttp:FriendMessage:10001"
    plugin._handle_sub_command("静音", umo)
    asyncio.run(plugin._handle_nudge({"kind": "knock", "from_name": "我", "at": 0}))
    assert context.sent == []
    asyncio.run(plugin._handle_nudge({"kind": "nag", "text": "别摸鱼了", "from_name": "我", "at": 0}))
    assert len(context.sent) == 1 and "别摸鱼" in context.sent[0][1]


def test_nudge_forwarding_can_be_disabled(plugin_module):
    plugin, context = _make_plugin(plugin_module, forward_nudges=False)
    asyncio.run(plugin._handle_nudge({"kind": "knock", "from_name": "我", "at": 0}))
    assert context.sent == []


# ---- 快照构建 -----------------------------------------------------------


def test_snapshot_maps_focus_segment_onto_synctus_fields(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    snapshot, todos = plugin._build_snapshot(
        plugin._bridge.load(), datetime(2026, 8, 27, 10, 0)
    )
    assert snapshot["presence"] == "active"
    assert snapshot["foreground"]["title"] == "赶稿子"
    assert snapshot["pomodoro"]["phase"] == "focus"
    assert snapshot["goal_min"] > 0
    assert 0 < snapshot["battery"]["percent"] <= 100
    assert snapshot["name"] == "小澈 · 手机"
    assert snapshot["user"] == "小澈"
    assert any("交稿" in item["title"] for item in todos)
    assert snapshot["todos_open"] + snapshot["todos_done_today"] == len(todos)


def test_snapshot_marks_sleeping_as_resting(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    snapshot, _todos = plugin._build_snapshot(
        plugin._bridge.load(), datetime(2026, 8, 27, 3, 0)
    )
    assert snapshot["presence"] == "resting"
    assert snapshot["foreground"]["app"] == "锁屏"
    assert "pomodoro" not in snapshot or snapshot["pomodoro"]["phase"] == "idle"


def test_snapshot_omits_disabled_shares(plugin_module):
    plugin, _ = _make_plugin(plugin_module, share_battery=False, share_activity=False)
    snapshot, _todos = plugin._build_snapshot(
        plugin._bridge.load(), datetime(2026, 8, 27, 10, 0)
    )
    assert "battery" not in snapshot
    assert "foreground" not in snapshot


def test_snapshot_change_detection_ignores_timestamp(plugin_module):
    plugin, _ = _make_plugin(plugin_module)
    data = plugin._bridge.load()
    first, _ = plugin._build_snapshot(data, datetime(2026, 8, 27, 10, 0, 0))
    same_minute, _ = plugin._build_snapshot(data, datetime(2026, 8, 27, 10, 0, 30))
    assert plugin._snapshot_changed(None, first) is True
    # 同一分钟内只有 at 在变，不该重发
    assert plugin._snapshot_changed(first, same_minute) is False
    # 下一分钟专注时长 +1，这是真实变化，应当重发
    next_minute, _ = plugin._build_snapshot(data, datetime(2026, 8, 27, 10, 1, 0))
    assert next_minute["focus_today_min"] == first["focus_today_min"] + 1
    assert plugin._snapshot_changed(first, next_minute) is True


# ---- 与真实中继联通 -----------------------------------------------------


def _relay_binary():
    for profile in ("debug", "release"):
        for name in ("synctus-server.exe", "synctus-server"):
            candidate = REPO_ROOT / "target" / profile / name
            if candidate.is_file():
                return candidate
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.timeout(90)
def test_plugin_publishes_bot_state_to_a_real_relay(plugin_module, tmp_path):
    """插件启动后，房间里的另一台设备应能看到 Bot 的活动、电量与待办。"""
    binary = _relay_binary()
    if binary is None:
        pytest.skip("未构建中继：cargo build -p synctus-server --bin synctus-server")

    from astrbot_plugin_synctus_companion.synctus import (
        SynctusClient,
        SynctusClientConfig,
        random_id,
    )
    from astrbot_plugin_synctus_companion.synctus.crypto import MissingDependency

    port = _free_port()
    invite = "PLUGIN-E2E-TEST-CODE"
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "server.toml"
        config_path.write_text(f'bind = "127.0.0.1:{port}"\n', encoding="utf-8")
        process = subprocess.Popen(
            [str(binary), "--config", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=tmp,
        )
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if process.poll() is not None:
                    pytest.skip("中继启动失败")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.15)

            plugin, _context = _make_plugin(
                plugin_module,
                enable_synctus=True,
                invite_code=invite,
                server=f"127.0.0.1:{port}",
                tls=False,
            )

            async def scenario():
                observed: list = []

                def record(device_id, payload):
                    observed.append(payload)

                    async def _noop():
                        return None

                    return _noop()

                try:
                    watcher = SynctusClient(
                        SynctusClientConfig(
                            server=f"127.0.0.1:{port}",
                            invite_code=invite,
                            device_id=random_id(8),
                            device_name="我的电脑",
                            user="我",
                            tls=False,
                        ),
                        on_peer_status=record,
                    )
                except MissingDependency as exc:
                    pytest.skip(str(exc))

                watcher.start()
                await plugin.initialize()
                try:
                    deadline_ = time.monotonic() + 20
                    while time.monotonic() < deadline_ and not (
                        plugin._client is not None
                        and plugin._client.connected
                        and watcher.connected
                    ):
                        await asyncio.sleep(0.05)
                    assert plugin._client is not None and plugin._client.connected, (
                        plugin._client.last_error if plugin._client else "客户端未启动"
                    )

                    # 不等 30 秒的 tick，直接发布一次当前状态。
                    await plugin._publish(plugin._bridge.load(), datetime(2026, 8, 27, 10, 0))

                    deadline_ = time.monotonic() + 15
                    while time.monotonic() < deadline_ and not observed:
                        await asyncio.sleep(0.05)
                    assert observed, "对端没有看到 Bot 的状态"
                    payload = observed[-1]
                    assert payload["name"] == "小澈 · 手机"
                    assert payload["presence"] == "active"
                    assert payload["foreground"]["title"] == "赶稿子"
                    assert 0 < payload["battery"]["percent"] <= 100
                    assert payload["goal_min"] > 0

                    device_id = plugin._state["device_id"]
                    deadline_ = time.monotonic() + 10
                    while time.monotonic() < deadline_ and not (
                        watcher.peers.get(device_id) and watcher.peers[device_id].todos
                    ):
                        await asyncio.sleep(0.05)
                    todos = watcher.peers[device_id].todos
                    assert todos, "对端没有看到待办清单"
                    assert any("交稿" in item["title"] for item in todos)
                finally:
                    await watcher.stop()
                    await plugin.terminate()

            asyncio.run(scenario())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
