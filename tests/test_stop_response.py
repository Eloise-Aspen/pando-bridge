"""停止当前回答:验证 /ws 的 stop 消息能终止在途子进程、发 stopped 结束帧、回收登记。

用假子进程(monkeypatch create_subprocess_exec)驱动真实 /ws 路径,不依赖 claude CLI,
因此与操作系统无关(Windows 开发机也能跑)。注意:真实 POSIX SIGTERM/僵尸回收的行为
仍需在 WSL 测试服上真机验证,本测试只覆盖服务端编排逻辑(读协程 / 登记表 / stopped 帧)。
"""

import asyncio
import json

from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app


def _config(tmp_path):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }


class _FakeStdout:
    """先吐预置的 stream-json 行,吐完后阻塞在 gate 上(模拟长回答挂起),
    直到 terminate/kill 打开 gate → 抛 StopAsyncIteration(等价子进程被杀后 stdout EOF)。"""

    def __init__(self, lines, gate):
        self._lines = lines
        self._i = 0
        self._gate = gate

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            await asyncio.sleep(0.01)
            return line
        await self._gate.wait()
        raise StopAsyncIteration


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines):
        self._gate = asyncio.Event()
        self.stdout = _FakeStdout(lines, self._gate)
        self.stderr = _FakeStderr()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._gate.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._gate.set()

    async def wait(self):
        await self._gate.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_stop_terminates_proc_and_sends_stopped_frame(tmp_path, monkeypatch):
    # feat-stream-output: text 帧来自 content_block_delta,不再从 assistant 事件重发。
    # 每个 chunk ≥ 40 字符(节流阈值),确保每个 delta 都独立 flush 产生一帧 text。
    chunk_a = "A" * 50  # ≥ _STREAM_THROTTLE_CHARS
    chunk_b = "B" * 50
    lines = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "sess-1", "model": "claude-x"}).encode(),
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": chunk_a}}}).encode(),
        json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": chunk_b}}}).encode(),
    ]
    created = {}

    async def fake_exec(*args, **kwargs):
        p = _FakeProc(lines)
        created["proc"] = p
        return p

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "写一段很长的回答"})

            text_frames = 0
            stop_sent = False
            result = None
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "text":
                    text_frames += 1
                    # 收到两帧 text(预置行吐完、流已挂起)后按下停止
                    if text_frames >= 2 and not stop_sent:
                        wsc.send_json({"type": "stop"})
                        stop_sent = True
                elif m.get("type") == "result":
                    result = m
                    break
                elif m.get("type") == "error":
                    raise AssertionError(f"unexpected error frame: {m}")

            assert result is not None, "未收到结束帧"
            assert result.get("stopped") is True, "结束帧未标记 stopped"
            assert result.get("text") == chunk_a + chunk_b, "已生成文本未保留"
            assert "usage" not in result, "停止轮不应带 usage(result 事件未到达)"
            # 子进程已被回收(terminate 生效,未走到 kill 兜底)
            assert created["proc"].terminated is True
            assert created["proc"].killed is False


def test_stop_without_inflight_sends_idempotent_stopped(tmp_path, monkeypatch):
    """无在途轮次时发 stop:回一帧 stopped 确认(幂等),前端收到即复位;
    后续消息仍可正常发——不崩、不阻塞(feat-reconnect-resume Task 3)。"""
    lines = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "sess-2", "model": "claude-x"}).encode(),
        json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "hi"}]}}).encode(),
        json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.001,
                    "duration_ms": 10, "usage": {"input_tokens": 5, "output_tokens": 2}}).encode(),
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            # 无在途轮次先发一个 stop:应回一帧幂等 stopped 确认
            wsc.send_json({"type": "stop"})

            # 先收到幂等 stopped 帧
            idempotent = None
            for _ in range(10):
                m = wsc.receive_json()
                if m.get("type") == "result" and m.get("stopped"):
                    idempotent = m
                    break
            assert idempotent is not None, "无在途时 stop 应回 stopped 确认帧"
            assert idempotent.get("stopped") is True
            assert idempotent.get("text") == ""

            # 随后正常消息仍可发
            wsc.send_json({"text": "你好"})

            result = None
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "result" and not m.get("stopped"):
                    result = m
                    break
                elif m.get("type") == "error":
                    raise AssertionError(f"unexpected error frame: {m}")

            assert result is not None
            assert result.get("text") == "hi"
