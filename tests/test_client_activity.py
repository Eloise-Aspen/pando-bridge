"""客户端门槛全部使用临时库和假进程，不访问模型网络。"""
import asyncio
import threading
import time

from fastapi.testclient import TestClient

from pando.activity import ClientActivity
import pando.server as server
from tests.test_carryover_auto_trigger import _Spy, _config, _drain_to, _seed_transcript


def test_activity_waits_and_reconnect_wakes_hour_sleep():
    async def check():
        gate = ClientActivity(True)
        waiter = asyncio.create_task(gate.wait_until_allowed())
        await asyncio.sleep(0)
        assert not waiter.done()
        gate.connected("a")
        await asyncio.wait_for(waiter, .2)
        revision = gate.revision
        gate.connected("b")
        gate.disconnected("a")
        assert gate.allowed
        gate.disconnected("b")
        assert not gate.allowed
        gate.disconnected("b")
        reconnect = asyncio.create_task(gate.wait_for_check(3600, since=revision))
        await asyncio.sleep(0)
        assert not reconnect.done()
        gate.connected("c")
        await asyncio.wait_for(reconnect, .2)
    asyncio.run(check())
    assert ClientActivity().allowed


def test_real_websocket_last_client_controls_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_detect_lan_ip", lambda: None)
    app = server.create_app(_config(tmp_path, BACKGROUND_REQUIRES_CLIENT=True))
    with TestClient(app) as client:
        assert not app.state.background_activity.allowed
        client.get("/health")
        client.get("/models")
        assert not app.state.background_activity.allowed
        with client.websocket_connect("/ws") as first:
            first.receive_json()
            with client.websocket_connect("/ws") as second:
                second.receive_json()
                assert app.state.background_activity.allowed
            assert app.state.background_activity.allowed
        assert not app.state.background_activity.allowed


def test_reader_closes_gate_while_foreground_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_detect_lan_ip", lambda: None)
    started, release = threading.Event(), threading.Event()
    spy = _Spy(["busy-session"])
    async def spawn(*args, **kwargs):
        proc = await spy(*args, **kwargs)
        original = proc.stdout
        class BusyOutput:
            def __aiter__(self):
                return self.lines()
            async def lines(self):
                started.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
                async for line in original:
                    yield line
        proc.stdout = BusyOutput()
        return proc
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spawn)
    app = server.create_app(_config(tmp_path, BACKGROUND_REQUIRES_CLIENT=True))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "hello"})
            assert started.wait(2)
            ws.close()
            try:
                deadline = time.monotonic() + 2
                while app.state.background_activity.allowed and time.monotonic() < deadline:
                    time.sleep(.01)
                assert not app.state.background_activity.allowed
            finally:
                release.set()
        assert len(spy.calls) == 1


def test_disconnect_does_not_start_archive(tmp_path, monkeypatch):
    from pando.providers.null import NullMemoryProvider
    class Memory(NullMemoryProvider):
        def build_archive_prompt(self, messages, force=False):
            raise AssertionError("disconnected archive must not reach provider")
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: Memory())
    monkeypatch.setattr(server, "_detect_lan_ip", lambda: None)
    spy = _Spy(["session"])
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spy)
    app = server.create_app(_config(tmp_path, BACKGROUND_REQUIRES_CLIENT=True))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "hello"})
            _drain_to(ws, "result")
        assert len(spy.calls) == 1


def test_disconnected_pending_waits_then_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_detect_lan_ip", lambda: None)
    spy = _Spy(["old"], cache_read=5000)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", spy)
    app = server.create_app(_config(
        tmp_path, BACKGROUND_REQUIRES_CLIENT=True,
        AUTO_CARRYOVER_SOFT_TOKENS=1000, AUTO_CARRYOVER_HARD_TOKENS=1_000_000,
        AUTO_CARRYOVER_IDLE_MINUTES=.005,
    ))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            _seed_transcript(tmp_path, "old")
            ws.send_json({"text": "hello"})
            _drain_to(ws, "result")
        time.sleep(.4)
        assert "old" in app.state.auto_carryover_pending
        assert len(spy.calls) == 1
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            deadline = time.monotonic() + 2
            while "old" in app.state.auto_carryover_pending and time.monotonic() < deadline:
                time.sleep(.01)
            assert "old" not in app.state.auto_carryover_pending
