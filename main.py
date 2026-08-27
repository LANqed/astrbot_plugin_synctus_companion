"""让 Bot 作为 Synctus 房间里的一台虚拟设备出现。

数据流：

    astrbot_plugin_private_companion (可选依赖)
        │  companions.json / 运行实例  → 今日日程、拟人状态、重要日期
        ▼
    companion_bridge  →  日程段（起床/专注/吃饭/摸鱼/睡觉）
        ├─ presence.py  → presence + 前台应用 + 番茄钟 + 专注分钟
        ├─ battery.py   → 按日程积分出的手机电量曲线
        └─ tasks.py     → 带 deadline 的待办清单
        ▼
    synctus/ 客户端（端到端加密）  →  中继  →  你的电脑悬浮窗 / 手机通知栏

于是在 Synctus 里，Bot 和真人搭档长得一样：
「在忙 · 备忘录：赶稿子」「🔋68%」「🍅专注 23:10」「☑4/10」。

对端的「敲一敲」「别摸鱼了」会被转达到 QQ 私聊，Bot 的日程切换也会顺带
在 QQ 里说一句，两边是同一个人格的两个出口。
"""

import asyncio
import contextlib
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from .battery import BatterySimulator
from .companion_bridge import (
    CompanionBridge,
    days_until,
    fmt_hhmm,
    window_name,
)
from .presence import (
    completed_focus_rounds,
    focus_goal_minutes,
    focus_minutes_so_far,
    foreground_for,
    pomodoro_for,
    presence_for,
)
from .synctus import SynctusClient, SynctusClientConfig, model, random_id
from .synctus.crypto import MissingDependency
from .tasks import build_task_list, counts, render_task_lines

PLUGIN_NAME = "astrbot_plugin_synctus_companion"

TICK_SECONDS = 30
DEFAULT_BOT_NAME = "小澈"
DEFAULT_DEVICE_NAME = "手机"
DEFAULT_MAX_DAILY = 12
DEFAULT_MIN_GAP_MINUTES = 45
TRANSITION_JITTER_RANGE = (10, 180)
TRANSITION_MIN_GAP_SECONDS = 300

IDLE_TEMPLATES = [
    "刚刚在{activity}，突然想看看你在干嘛",
    "{activity}的间隙里想到你啦，随便聊聊？",
    "在{activity}，有点想找人说话……你在忙吗",
    "趁着{activity}歇一下～你那边怎么样呀",
    "在{activity}的时候突然好奇：你今天过得还好吗",
]


def _parse_hhmm_span(value: str):
    text = (value or "").strip()
    if not text or text == "-":
        return None
    left, sep, right = text.partition("-")
    if not sep:
        return None
    from .companion_bridge import parse_hhmm

    start, end = parse_hhmm(left), parse_hhmm(right)
    if start is None or end is None or start == end:
        logger.warning(f"[SynctusCompanion] 免打扰时段配置无效: {value}，已忽略")
        return None
    return (start, end)


def _default_user_state() -> dict:
    return {
        "muted": False,
        "date": "",
        "sent_today": 0,
        "last_sent_ts": 0.0,
        "last_segment": None,
        "pending_at": None,
        "pending_msg": None,
    }


class SynctusCompanionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._state_path = self.data_dir / "state.json"
        self._state = self._load_state()
        self._bridge = CompanionBridge(
            context,
            data_file=str(self._cfg("dependency_data_file", "")),
            logger=logger,
        )
        self._battery = BatterySimulator()
        self._client: Optional[SynctusClient] = None
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._last_published: Optional[dict] = None
        self._last_todo_ids: Optional[str] = None
        self._last_publish_ts = 0.0
        self._quiet = _parse_hhmm_span(str(self._cfg("quiet_hours", "")))

    def _cfg(self, key: str, default=None):
        """读配置项。

        _conf_schema.json 用 object 分组承载 synctus / companion 两组设置，
        AstrBotConfig 里它们是嵌套 dict。这里先看顶层（兼容旧的扁平配置），
        再逐个分组找，于是调用方不必关心某一项被放在哪一组。
        """
        config = self.config
        if not isinstance(config, dict):
            return default
        if key in config and not isinstance(config.get(key), dict):
            return config.get(key)
        for group in ("synctus", "companion"):
            section = config.get(group)
            if isinstance(section, dict) and key in section:
                return section.get(key)
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        return default if value is None else bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            value = self._cfg(key, default)
            return default if value is None else int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            value = self._cfg(key, default)
            return default if value is None else float(value)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self._cfg(key, default)
        return default if value is None else str(value).strip()

    # ---- 持久化 ------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("device_id", random_id(8))
                    data.setdefault("users", {})
                    return data
        except Exception as exc:
            logger.warning(f"[SynctusCompanion] 状态文件损坏，将重建: {exc}")
        return {"device_id": random_id(8), "users": {}}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[SynctusCompanion] 保存状态失败: {exc}")

    # ---- 生命周期 ----------------------------------------------------

    async def initialize(self):
        self._bridge.load()
        invite = self._cfg_str("invite_code")
        if invite and self._cfg_bool("enable_synctus", True):
            self._start_client(invite)
        else:
            logger.info(
                "[SynctusCompanion] 未配置配对码或已关闭 Synctus 上报，"
                "仅提供 QQ 侧的日程陪伴"
            )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="synctus-companion-loop")

    async def terminate(self):
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait({task}, timeout=3)
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._save_state()

    def _start_client(self, invite_code: str) -> None:
        client_config = SynctusClientConfig(
            server=self._cfg_str("server", "127.0.0.1:8787") or "127.0.0.1:8787",
            invite_code=invite_code,
            device_id=str(self._state.get("device_id") or random_id(8)),
            device_name=self._cfg_str("device_name", DEFAULT_DEVICE_NAME)
            or DEFAULT_DEVICE_NAME,
            user=self._synctus_user(),
            tls=self._cfg_bool("tls", True),
            tls_verify=self._cfg_bool("tls_verify", True),
        )
        self._client = SynctusClient(
            client_config,
            on_nudge=self._handle_nudge,
            on_state_change=self._handle_client_state,
            logger=logger,
        )
        self._client.start()

    def _synctus_user(self) -> str:
        return self._cfg_str("synctus_user") or self._bot_name()

    # ---- 配置读取 ----------------------------------------------------

    def _bot_name(self) -> str:
        configured = self._cfg_str("bot_name")
        if configured:
            return configured
        from_dependency = self._bridge.bot_name()
        return from_dependency or DEFAULT_BOT_NAME

    def _target_umos(self) -> list:
        seen, result = set(), []
        for entry in (self.config.get("target_users") or []):
            raw = str(entry).strip()
            if not raw:
                continue
            umo = raw if ":" in raw else f"aiocqhttp:FriendMessage:{raw}"
            if umo not in seen:
                seen.add(umo)
                result.append(umo)
        return result

    def _user_state(self, umo: str) -> dict:
        users = self._state.setdefault("users", {})
        state = users.get(umo)
        if not isinstance(state, dict):
            state = _default_user_state()
            users[umo] = state
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("date") != today:
            state["date"] = today
            state["sent_today"] = 0
        return state

    def _in_quiet(self, now: datetime) -> bool:
        if not self._quiet:
            return False
        from .companion_bridge import span_contains

        return span_contains(self._quiet[0], self._quiet[1], now.hour * 60 + now.minute)

    # ---- Synctus 上报 ------------------------------------------------

    def _build_snapshot(self, data, now: datetime) -> tuple:
        minute = now.hour * 60 + now.minute
        segment = data.segment_at(minute)
        energy = data.energy()
        date_key = now.strftime("%Y-%m-%d")
        now_ms = model.now_ms()
        day_start_ms = int(
            now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        )

        battery_state = None
        if self._cfg_bool("share_battery", True):
            battery_state = self._battery.sample(data.segments, date_key, minute)

        todos = build_task_list(
            data,
            now,
            goal_count=self._cfg_int("task_count", 8),
            day_start_ts_ms=day_start_ms,
        )
        open_count, done_count = counts(todos)
        rounds = completed_focus_rounds(data.segments, minute)

        snapshot = model.status_snapshot(
            device_id=str(self._state.get("device_id") or ""),
            name=self._device_display_name(),
            platform=model.PLATFORM_ANDROID,
            user=self._synctus_user(),
            presence=presence_for(segment, energy),
            at=now_ms,
            foreground=foreground_for(segment)
            if self._cfg_bool("share_activity", True)
            else None,
            battery_state=battery_state,
            pomodoro=pomodoro_for(segment, minute, rounds, day_start_ms),
            todos_open=open_count,
            todos_done_today=done_count,
            focus_today_min=focus_minutes_so_far(data.segments, minute),
            goal_min=focus_goal_minutes(data.segments),
            streak_days=int(self._state.get("streak_days") or 0),
        )
        return snapshot, todos

    def _device_display_name(self) -> str:
        name = self._cfg_str("device_name", DEFAULT_DEVICE_NAME) or DEFAULT_DEVICE_NAME
        return f"{self._bot_name()} · {name}"

    @staticmethod
    def _snapshot_changed(previous: Optional[dict], current: dict) -> bool:
        """`at` 每次都变，比较时忽略它，只在真的有变化时重发。"""
        if previous is None:
            return True
        keys = set(previous) | set(current)
        keys.discard("at")
        return any(previous.get(key) != current.get(key) for key in keys)

    async def _publish(self, data, now: datetime) -> None:
        client = self._client
        if client is None:
            return
        snapshot, todos = self._build_snapshot(data, now)
        heartbeat_due = time.time() - self._last_publish_ts > 60
        if self._snapshot_changed(self._last_published, snapshot) or heartbeat_due:
            await client.publish(snapshot)
            self._last_published = snapshot
            self._last_publish_ts = time.time()
        if self._cfg_bool("share_tasks", True):
            todo_ids = json.dumps(
                [(item["id"], item["done"]) for item in todos], ensure_ascii=False
            )
            if todo_ids != self._last_todo_ids:
                await client.publish_todos(todos)
                self._last_todo_ids = todo_ids

    async def _handle_client_state(self, state: str, detail: str) -> None:
        if state == "fatal":
            logger.warning(f"[SynctusCompanion] Synctus 连接终止: {detail}")

    async def _handle_nudge(self, nudge: dict) -> None:
        """把对端的敲一敲/别摸鱼了转达到 QQ 私聊。"""
        if not self._cfg_bool("forward_nudges", True):
            return
        text = model.describe_nudge(nudge)
        kind = str(nudge.get("kind") or "")
        now = datetime.now()
        data = self._bridge.load()
        segment = data.segment_at(now.hour * 60 + now.minute)
        if segment is not None:
            text = f"{text}（我正在{segment.activity}）"
        # 「别摸鱼了」是唯一允许穿透静音的互动，与 Synctus 桌面端一致。
        urgent = kind == "nag"
        for umo in self._target_umos():
            state = self._user_state(umo)
            if state.get("muted") and not urgent:
                continue
            await self._send(umo, text, count_quota=False)
        self._save_state()

    # ---- QQ 侧陪伴 ---------------------------------------------------

    async def _send(self, umo: str, text: str, *, count_quota: bool = True) -> bool:
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:
            logger.error(f"[SynctusCompanion] 消息发送失败 ({umo}): {exc}")
            return False
        if count_quota:
            state = self._user_state(umo)
            state["sent_today"] = int(state.get("sent_today") or 0) + 1
            state["last_sent_ts"] = time.time()
        logger.info(f"[SynctusCompanion] 已发送 {umo}: {text[:40]}")
        return True

    def _gate(self, state: dict, *, idle: bool, now: datetime) -> bool:
        if state.get("muted"):
            return False
        if int(state.get("sent_today") or 0) >= self._cfg_int(
            "max_daily_messages", DEFAULT_MAX_DAILY
        ):
            return False
        last = float(state.get("last_sent_ts") or 0)
        if last:
            gap = (
                self._cfg_int("min_gap_minutes", DEFAULT_MIN_GAP_MINUTES) * 60
                if idle
                else TRANSITION_MIN_GAP_SECONDS
            )
            if time.time() - last < gap:
                return False
        return not (idle and self._in_quiet(now))

    def _transition_text(self, segment) -> str:
        """日程切换时说的话。优先用依赖插件日程里的 message_seed。"""
        if segment.message_seed:
            return segment.message_seed
        if segment.sleeping:
            return f"要去{segment.activity}啦，晚安，好梦～"
        mood = f"（{segment.mood}）" if segment.mood else ""
        return f"我去{segment.activity}了{mood}"

    async def _tick_qq(self, data, now: datetime) -> None:
        targets = self._target_umos()
        if not targets:
            return
        minute = now.hour * 60 + now.minute
        segment = data.segment_at(minute)
        if segment is None:
            return
        dirty = False
        for umo in targets:
            state = self._user_state(umo)
            pending_at = state.get("pending_at")
            if pending_at is not None and time.time() >= float(pending_at):
                message = state.get("pending_msg")
                state["pending_at"] = None
                state["pending_msg"] = None
                dirty = True
                if message and self._gate(state, idle=False, now=now):
                    await self._send(umo, message)

            if state.get("last_segment") != segment.key:
                first_observation = state.get("last_segment") is None
                state["last_segment"] = segment.key
                dirty = True
                if (
                    not first_observation
                    and pending_at is None
                    and self._cfg_bool("enable_transition", True)
                    and self._gate(state, idle=False, now=now)
                ):
                    state["pending_at"] = time.time() + random.uniform(
                        *TRANSITION_JITTER_RANGE
                    )
                    state["pending_msg"] = self._transition_text(segment)

            if (
                self._cfg_bool("enable_idle_chat", True)
                and state.get("pending_at") is None
                and segment.interruptible
            ):
                chance = self._cfg_float("idle_chance_per_minute", 0.006)
                # tick 间隔不是一分钟，按比例折算，配置项含义才与名字一致。
                chance *= TICK_SECONDS / 60.0
                if (
                    chance > 0
                    and random.random() < chance
                    and self._gate(state, idle=True, now=now)
                ):
                    await self._send(
                        umo,
                        random.choice(IDLE_TEMPLATES).format(activity=segment.activity),
                    )
                    dirty = True
        if dirty:
            self._save_state()

    # ---- 主循环 ------------------------------------------------------

    async def _loop(self) -> None:
        logger.info("[SynctusCompanion] 陪伴循环运行中")
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            try:
                now = datetime.now()
                data = self._bridge.load()
                await self._publish(data, now)
                await self._tick_qq(data, now)
            except MissingDependency as exc:
                logger.warning(f"[SynctusCompanion] {exc}")
            except Exception:
                logger.exception("[SynctusCompanion] tick 异常")
        logger.info("[SynctusCompanion] 陪伴循环已退出")

    # ---- 文本渲染 ----------------------------------------------------

    def _render_status(self, umo: str, now: datetime) -> str:
        data = self._bridge.load()
        minute = now.hour * 60 + now.minute
        segment = data.segment_at(minute)
        name = self._bot_name()
        if segment is None:
            return f"{name}的日程表读不出来，检查一下依赖插件的日程吧"

        lines = [
            f"{name}现在：{segment.activity}"
            + (f"（{segment.mood}）" if segment.mood else "")
            + f" · {now.strftime('%H:%M')} · {window_name(minute)}",
            (
                f"这一段 {fmt_hhmm(segment.start)}-{fmt_hhmm(segment.end)}，"
                f"已经 {segment.elapsed_minutes(minute)} 分钟，"
                f"还有 {segment.remaining_minutes(minute)} 分钟"
            ),
        ]
        nxt = data.next_segment(minute)
        if nxt is not None:
            lines.append(
                f"下一段：{nxt.activity}（{fmt_hhmm(nxt.start)}，"
                f"还有 {(nxt.start - minute) % 1440} 分钟）"
            )

        battery_state = self._battery.sample(
            data.segments, now.strftime("%Y-%m-%d"), minute
        )
        icon = "⚡" if battery_state["charging"] else "🔋"
        battery_line = f"手机 {icon}{battery_state['percent']}%"
        left = battery_state.get("minutes_left")
        if left:
            battery_line += f"（约还能用 {left // 60} 小时 {left % 60} 分钟）"
        lines.append(battery_line)

        focused = focus_minutes_so_far(data.segments, minute)
        goal = focus_goal_minutes(data.segments)
        if goal:
            lines.append(f"今天专注 {focused}/{goal} 分钟")

        todos = build_task_list(data, now, goal_count=self._cfg_int("task_count", 8))
        open_count, done_count = counts(todos)
        lines.append(f"待办 已完成 {done_count} / 共 {open_count + done_count}")

        lines.append(
            f"状态：{data.state.get('sleep', '')}｜{data.state.get('hunger', '')}"
            f"｜精力 {data.energy()}"
        )
        lines.append(self._synctus_line())
        if umo:
            state = self._user_state(umo)
            if state.get("muted"):
                lines.append("你现在处于静音状态，我不会主动打扰")
        return "\n".join(line for line in lines if line)

    def _synctus_line(self) -> str:
        if not self._cfg_bool("enable_synctus", True):
            return "Synctus 上报：已关闭"
        client = self._client
        if client is None:
            return "Synctus 上报：未配置配对码"
        if client.connected:
            peers = sum(1 for view in client.peers.values() if view.online)
            return f"Synctus 上报：已连接（房间内其他设备 {peers} 台）"
        error = client.last_error or "正在重连"
        return f"Synctus 上报：未连接（{error}）"

    def _render_schedule(self, now: datetime) -> str:
        data = self._bridge.load()
        minute = now.hour * 60 + now.minute
        current = data.segment_at(minute)
        source = {
            "instance": "来自 astrbot_plugin_private_companion",
            "file": "来自 astrbot_plugin_private_companion（数据文件）",
            "default": "内置默认作息（未读到依赖插件日程）",
        }.get(data.source, data.source)
        lines = [f"{self._bot_name()}的一天 · {source}"]
        if data.plan_date:
            lines[0] += f" · {data.plan_date}"
        for segment in data.segments:
            mark = "  <- 现在" if segment is current else ""
            mood = f"（{segment.mood}）" if segment.mood else ""
            lines.append(
                f"{fmt_hhmm(segment.start)}-{fmt_hhmm(segment.end)} "
                f"{segment.activity}{mood}{mark}"
            )
        return "\n".join(lines)

    def _render_tasks(self, now: datetime) -> str:
        data = self._bridge.load()
        todos = build_task_list(data, now, goal_count=self._cfg_int("task_count", 8))
        open_count, done_count = counts(todos)
        lines = [f"{self._bot_name()}的待办（{done_count}/{open_count + done_count}）"]
        lines.extend(render_task_lines(todos, limit=12))
        upcoming = []
        for entry in data.important_dates:
            remaining = days_until(entry["date"], now)
            if remaining is None or remaining < 0 or remaining > 30:
                continue
            when = "今天" if remaining == 0 else f"{remaining} 天后"
            upcoming.append(f"· {entry['title']}｜{when}")
        if upcoming:
            lines.append("盯着的日子：")
            lines.extend(upcoming[:5])
        return "\n".join(lines)

    def _handle_sub_command(self, sub: str, umo: str) -> str:
        now = datetime.now()
        if sub in {"静音", "闭嘴", "安静"}:
            if umo:
                self._user_state(umo)["muted"] = True
                self._save_state()
            return "好……我安静陪着你。想我了就发「陪伴 恢复」"
        if sub in {"恢复", "开口"}:
            if umo:
                self._user_state(umo)["muted"] = False
                self._save_state()
            return "我又回来啦～刚刚有没有想我？"
        if sub in {"日程", "今日", "今天", "安排"}:
            return self._render_schedule(now)
        if sub in {"待办", "任务", "清单", "todo"}:
            return self._render_tasks(now)
        if sub in {"电量", "手机"}:
            data = self._bridge.load()
            minute = now.hour * 60 + now.minute
            battery_state = self._battery.sample(
                data.segments, now.strftime("%Y-%m-%d"), minute
            )
            icon = "⚡ 正在充电" if battery_state["charging"] else "🔋"
            left = battery_state.get("minutes_left")
            tail = f"，大概还能用 {left // 60} 小时 {left % 60} 分钟" if left else ""
            return f"手机 {battery_state['percent']}% {icon}{tail}"
        if sub in {"连接", "synctus", "上报"}:
            room = self._client.room_id_hex()[:8] if self._client else ""
            extra = f"\n房间 {room}…" if room else ""
            return self._synctus_line() + extra
        if sub in {"帮助", "help", "命令"}:
            return (
                "陪伴 状态 - 现在在做什么、电量、专注、待办\n"
                "陪伴 日程 - 今天的完整日程\n"
                "陪伴 待办 - 待办清单与盯着的日子\n"
                "陪伴 电量 - 手机电量\n"
                "陪伴 连接 - Synctus 上报状态\n"
                "陪伴 静音 / 陪伴 恢复 - 暂停或恢复主动消息"
            )
        return self._render_status(umo, now)

    # ---- 命令 --------------------------------------------------------

    @filter.command("陪伴", alias={"日程陪伴", "同步陪伴"})
    async def companion_command(self, event: AstrMessageEvent):
        """查看 Bot 的当前活动、电量、待办与 Synctus 上报状态。"""
        text = (
            str(getattr(event, "message_str", "") or "")
            .replace("／", "/")
            .replace("\u3000", " ")
            .strip()
        )
        parts = text.split()
        sub = parts[1] if len(parts) > 1 else "状态"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        yield event.plain_result(self._handle_sub_command(sub, umo))

    @filter.command("日程", alias={"今日日程", "bot日程"})
    async def schedule_command(self, event: AstrMessageEvent):
        """查看 Bot 今天的日程安排。"""
        yield event.plain_result(self._render_schedule(datetime.now()))

    @filter.command("待办", alias={"任务清单", "bot待办"})
    async def tasks_command(self, event: AstrMessageEvent):
        """查看 Bot 的待办清单与临近的重要日期。"""
        yield event.plain_result(self._render_tasks(datetime.now()))
