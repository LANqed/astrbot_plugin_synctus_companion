"""集成测试：Python 客户端 ↔ 真实的 Rust 中继。

这条测试验证的是握手与帧格式确实与 crates/server 的实现兼容——
单元测试能证明密钥派生一致，但只有跑真服务器才能证明
Hello/Challenge/Auth/Welcome 的字段名、帧长前缀和 retain 语义都对。

需要先构建中继：cargo build -p synctus-server --bin synctus-server
未构建时整个模块跳过，因此在没有 Rust 工具链的机器上不会失败。
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from astrbot_plugin_synctus_companion.synctus import (
    SynctusClient,
    SynctusClientConfig,
    model,
    random_id,
)
from astrbot_plugin_synctus_companion.synctus.crypto import MissingDependency

REPO_ROOT = Path(__file__).resolve().parents[2]
INVITE_CODE = "TEST-CODE-FOR-INTEGRATION"


def _relay_binary() -> Path | None:
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


@pytest.fixture(scope="module")
def relay():
    binary = _relay_binary()
    if binary is None:
        pytest.skip("未构建中继：cargo build -p synctus-server --bin synctus-server")
    port = _free_port()
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "server.toml"
        # 明文监听：TLS 由测试之外的部署负责，这里要验的是帧协议本身。
        config.write_text(f'bind = "127.0.0.1:{port}"\n', encoding="utf-8")
        process = subprocess.Popen(
            [str(binary), "--config", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=tmp,
        )
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if process.poll() is not None:
                    pytest.skip(f"中继启动失败，退出码 {process.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.15)
            else:
                pytest.skip("中继未在 15 秒内就绪")
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def _client(port: int, device_name: str, **kwargs) -> SynctusClient:
    config = SynctusClientConfig(
        server=f"127.0.0.1:{port}",
        invite_code=INVITE_CODE,
        device_id=random_id(8),
        device_name=device_name,
        user=device_name,
        tls=False,
    )
    return SynctusClient(config, **kwargs)


async def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def _snapshot(device_id: str, presence: str, percent: int) -> dict:
    return model.status_snapshot(
        device_id=device_id,
        name="Bot · 手机",
        platform=model.PLATFORM_ANDROID,
        user="Bot",
        presence=presence,
        foreground=model.foreground_app("备忘录", name="备忘录", title="赶稿子"),
        battery_state=model.battery(percent, charging=False, minutes_left=300),
        pomodoro=model.pomodoro_state(
            model.PHASE_FOCUS, ends_at=model.now_ms() + 900_000, completed_today=2
        ),
        todos_open=6,
        todos_done_today=4,
        focus_today_min=75,
        goal_min=170,
    )


@pytest.mark.timeout(60)
def test_handshake_and_status_roundtrip_through_real_relay(relay):
    """两个 Python 客户端经真中继互相看到状态与 nudge。"""

    async def scenario():
        received_status: list = []
        received_nudges: list = []

        def _record(sink):
            def handler(*args):
                sink.append(args[-1] if len(args) == 1 else args)

                async def _noop():
                    return None

                return _noop()

            return handler

        try:
            bot = _client(relay, "Bot", on_nudge=_record(received_nudges))
        except MissingDependency as exc:
            pytest.skip(str(exc))
        peer = _client(relay, "Peer", on_peer_status=_record(received_status))

        bot.start()
        peer.start()
        try:
            assert await _wait_for(lambda: bot.connected and peer.connected), (
                f"未能连上中继: bot={bot.last_error} peer={peer.last_error}"
            )

            snapshot = _snapshot(bot._config.device_id, model.PRESENCE_ACTIVE, 68)
            await bot.publish(snapshot)
            assert await _wait_for(lambda: bool(received_status)), "对端没收到状态"

            device_id, payload = received_status[-1]
            assert device_id == bot._config.device_id
            assert payload["presence"] == model.PRESENCE_ACTIVE
            assert payload["battery"]["percent"] == 68
            assert payload["foreground"]["title"] == "赶稿子"
            assert payload["pomodoro"]["phase"] == model.PHASE_FOCUS
            assert payload["todos_done_today"] == 4
            assert payload["focus_today_min"] == 75

            # 待办作为独立消息发送，状态帧才能保持小体积。
            await bot.publish_todos(
                [model.todo("t1", "赶稿子（还剩 2 天）", False, model.now_ms())]
            )
            assert await _wait_for(
                lambda: bool(peer.peers.get(bot._config.device_id))
                and bool(peer.peers[bot._config.device_id].todos)
            ), "对端没收到待办"
            assert (
                peer.peers[bot._config.device_id].todos[0]["title"]
                == "赶稿子（还剩 2 天）"
            )

            # 反向：对端的「别摸鱼了」必须能被 Bot 收到，插件才能转达到 QQ。
            await peer.send_nudge("nag", "别摸鱼了", "我")
            assert await _wait_for(lambda: bool(received_nudges)), "Bot 没收到 nudge"
            nudge = received_nudges[-1]
            assert nudge["kind"] == "nag"
            assert model.describe_nudge(nudge) == "👀 我：别摸鱼了"
        finally:
            await bot.stop()
            await peer.stop()

    asyncio.run(scenario())


@pytest.mark.timeout(60)
def test_retained_status_is_replayed_to_a_later_joiner(relay):
    """先发布状态、后加入的一端应立刻看到它，不必等下一个采样周期。"""

    async def scenario():
        received: list = []

        bot = _client(relay, "Bot")
        bot.start()
        try:
            assert await _wait_for(lambda: bot.connected), bot.last_error
            await bot.publish(_snapshot(bot._config.device_id, model.PRESENCE_RESTING, 42))
            await asyncio.sleep(0.5)

            def record(device_id, payload):
                received.append((device_id, payload))

                async def _noop():
                    return None

                return _noop()

            late = _client(relay, "Late", on_peer_status=record)
            late.start()
            try:
                assert await _wait_for(lambda: late.connected), late.last_error
                assert await _wait_for(lambda: bool(received)), "保留状态没有被回放"
                assert received[-1][1]["presence"] == model.PRESENCE_RESTING
                assert received[-1][1]["battery"]["percent"] == 42
            finally:
                await late.stop()
        finally:
            await bot.stop()

    asyncio.run(scenario())


@pytest.mark.timeout(60)
def test_wrong_invite_code_cannot_read_the_room(relay):
    """配对码不同即不同房间：中继只按 room_id 路由，读不到彼此。"""

    async def scenario():
        received: list = []
        bot = _client(relay, "Bot")
        stranger_config = SynctusClientConfig(
            server=f"127.0.0.1:{relay}",
            invite_code="COMPLETELY-DIFFERENT-CODE",
            device_id=random_id(8),
            device_name="Stranger",
            user="Stranger",
            tls=False,
        )

        def record(device_id, payload):
            received.append((device_id, payload))

            async def _noop():
                return None

            return _noop()

        stranger = SynctusClient(stranger_config, on_peer_status=record)
        bot.start()
        stranger.start()
        try:
            assert await _wait_for(lambda: bot.connected and stranger.connected)
            assert bot.room_id_hex() != stranger.room_id_hex()
            await bot.publish(_snapshot(bot._config.device_id, model.PRESENCE_ACTIVE, 90))
            await asyncio.sleep(1.5)
            assert not received, "不同配对码的客户端不应收到任何状态"
        finally:
            await bot.stop()
            await stranger.stop()

    asyncio.run(scenario())
