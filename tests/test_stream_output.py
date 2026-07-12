"""流式输出（feat-stream-output）:验证 content_block_delta 增量下发 + 去重。

用假子进程（monkeypatch create_subprocess_exec）构造 delta + assistant 序列，
断言:
1. text 帧来自 delta 逐段累积（节流聚合），不重复
2. assistant 事件不重发 text 帧——用户不会看到全文闪现两遍
3. result.text 与 delta 累积全文一致（权威文本校对）
4. tool_use 仍正常从 assistant 事件提取
5. 停止时已生成的 delta 文本保留在 result
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
    """按序吐出预置行，全部吐完后根据 gate 决定行为:
    有 gate 则阻塞（模拟长回答），无 gate 则直接 EOF。"""

    def __init__(self, lines, gate=None):
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
        if self._gate is not None:
            await self._gate.wait()
        raise StopAsyncIteration


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines, gate=None):
        self._gate = gate or asyncio.Event()
        self.stdout = _FakeStdout(lines, gate)
        self.stderr = _FakeStderr()
        self.returncode = None
        self.terminated = False
        self.killed = False
        if gate is None:
            self._gate.set()  # 无 gate:吐完即 EOF

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


def _enc(obj):
    """把 dict 编码为 stream-json 行（bytes，末尾无换行——FakeStdout 不需要）。"""
    return json.dumps(obj, ensure_ascii=False).encode()


def _delta(delta):
    """按生产线真实格式构造增量事件:content_block_delta **嵌套在 stream_event.event 里**
    (CC CLI --include-partial-messages 的输出形状,server 63db6b8 起按此解包)。
    裸的顶层 content_block_delta 会被服务端静默忽略——别再用旧格式(fix-launch-smoke 教训)。"""
    return _enc({"type": "stream_event",
                 "event": {"type": "content_block_delta", "delta": delta}})


# ------------------------------------------------------------------
# 测试 1: delta 逐段下发 + assistant 不重发 + result.text 校对
# ------------------------------------------------------------------

def test_delta_streaming_no_duplicate(tmp_path, monkeypatch):
    """构造 delta → assistant → result 序列，验证:
    - text 帧来自 delta，按节流聚合后下发
    - assistant 事件不产生额外 text 帧
    - result.text == delta 累积全文 == assistant 权威全文"""
    lines = [
        _enc({"type": "system", "subtype": "init",
              "session_id": "sess-stream-1", "model": "claude-test"}),
        # 多个 text_delta 模拟逐 token 流式
        _delta({"type": "text_delta", "text": "Hello "}),
        _delta({"type": "text_delta", "text": "world"}),
        _delta({"type": "text_delta", "text": "! How "}),
        _delta({"type": "text_delta", "text": "are you?"}),
        # assistant 事件:完整文本（权威），不应产生新 text 帧
        _enc({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Hello world! How are you?"}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        # result 事件
        _enc({"type": "result", "total_cost_usd": 0.001,
              "duration_ms": 100,
              "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "say hello"})

            frames = []
            for _ in range(50):
                m = wsc.receive_json()
                frames.append(m)
                if m.get("type") == "result":
                    break

            text_frames = [f for f in frames if f["type"] == "text"]
            result_frame = next(f for f in frames if f["type"] == "result")

            # delta 产生的 text 帧拼接 = 完整文本
            delta_concat = "".join(f["text"] for f in text_frames)
            assert delta_concat == "Hello world! How are you?", (
                f"delta 拼接不一致: {delta_concat!r}"
            )

            # result.text = 权威全文
            assert result_frame["text"] == "Hello world! How are you?"

            # 没有重复:text 帧数 <= delta 事件数(节流可能合并)
            # 关键:不能出现完整文本作为单独一帧(那就是 assistant 事件重发了)
            for tf in text_frames:
                assert tf["text"] != "Hello world! How are you?", (
                    "text 帧不应包含完整文本(assistant 事件不应重发)"
                )


# ------------------------------------------------------------------
# 测试 2: thinking delta 同样只走 delta 通道
# ------------------------------------------------------------------

def test_thinking_delta_no_duplicate(tmp_path, monkeypatch):
    """thinking_delta 逐段下发，assistant 事件不重发 thinking 帧。"""
    lines = [
        _enc({"type": "system", "subtype": "init",
              "session_id": "sess-think-1", "model": "claude-test"}),
        _delta({"type": "thinking_delta", "thinking": "Let me "}),
        _delta({"type": "thinking_delta", "thinking": "think about this."}),
        _delta({"type": "text_delta", "text": "Here is my answer."}),
        _enc({"type": "assistant", "message": {
            "content": [
                {"type": "thinking", "thinking": "Let me think about this."},
                {"type": "text", "text": "Here is my answer."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        _enc({"type": "result", "total_cost_usd": 0.001,
              "duration_ms": 100,
              "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "think about it"})

            frames = []
            for _ in range(50):
                m = wsc.receive_json()
                frames.append(m)
                if m.get("type") == "result":
                    break

            think_frames = [f for f in frames if f["type"] == "thinking"]
            text_frames = [f for f in frames if f["type"] == "text"]
            result_frame = next(f for f in frames if f["type"] == "result")

            # thinking 帧来自 delta
            think_concat = "".join(f["text"] for f in think_frames)
            assert think_concat == "Let me think about this."

            # text 帧来自 delta
            text_concat = "".join(f["text"] for f in text_frames)
            assert text_concat == "Here is my answer."

            # result 带权威 thinking
            assert result_frame["thinking"] == "Let me think about this."
            assert result_frame["text"] == "Here is my answer."


# ------------------------------------------------------------------
# 测试 3: tool_use 仍从 assistant 事件提取
# ------------------------------------------------------------------

def test_tool_use_from_assistant(tmp_path, monkeypatch):
    """tool_use 没有 delta 通道,仍从 assistant 事件提取并发帧。"""
    lines = [
        _enc({"type": "system", "subtype": "init",
              "session_id": "sess-tool-1", "model": "claude-test"}),
        _delta({"type": "text_delta", "text": "Reading file..."}),
        _enc({"type": "assistant", "message": {
            "content": [
                {"type": "text", "text": "Reading file..."},
                {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/test"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5}}}),
        # 工具结果后继续生成
        _delta({"type": "text_delta", "text": " Done."}),
        _enc({"type": "assistant", "message": {
            "content": [{"type": "text", "text": " Done."}],
            "usage": {"input_tokens": 15, "output_tokens": 8}}}),
        _enc({"type": "result", "total_cost_usd": 0.002,
              "duration_ms": 200,
              "usage": {"input_tokens": 25, "output_tokens": 13}}),
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "read a file"})

            frames = []
            for _ in range(50):
                m = wsc.receive_json()
                frames.append(m)
                if m.get("type") == "result":
                    break

            tool_frames = [f for f in frames if f["type"] == "tool_use"]
            result_frame = next(f for f in frames if f["type"] == "result")

            # tool_use 帧存在
            assert len(tool_frames) == 1
            assert tool_frames[0]["tool"] == "Read"

            # result.text = 权威全文(两段 assistant 累积)
            assert result_frame["text"] == "Reading file... Done."


# ------------------------------------------------------------------
# 测试 4: 停止时 delta 已生成的文本保留
# ------------------------------------------------------------------

def test_stop_preserves_delta_text(tmp_path, monkeypatch):
    """用户按停止:已通过 delta 累积的 full_text 保留在 stopped result 帧中。"""
    gate = asyncio.Event()
    lines = [
        _enc({"type": "system", "subtype": "init",
              "session_id": "sess-stop-1", "model": "claude-test"}),
        _delta({"type": "text_delta", "text": "Partial "}),
        _delta({"type": "text_delta", "text": "output"}),
        # 此后阻塞——模拟长回答挂起,用户按停止
    ]

    created = {}

    async def fake_exec(*args, **kwargs):
        p = _FakeProc(lines, gate=gate)
        created["proc"] = p
        return p

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "write a long answer"})

            # 等 delta text 帧到达
            text_received = False
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "text":
                    text_received = True
                    break
                if m.get("type") in ("result", "error"):
                    break

            assert text_received, "应收到至少一帧 delta text"

            # 按停止
            wsc.send_json({"type": "stop"})

            # 收 stopped result 帧
            result = None
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "result":
                    result = m
                    break

            assert result is not None, "未收到 stopped result 帧"
            assert result.get("stopped") is True
            assert "Partial" in result.get("text", ""), (
                f"stopped result 应保留已生成文本: {result.get('text')!r}"
            )


# ------------------------------------------------------------------
# 测试 5: usage/metadata 落库不受影响(回归)
# ------------------------------------------------------------------

def test_usage_metadata_unchanged(tmp_path, monkeypatch):
    """usage 字段从 result 事件提取,与 delta 流式无关(回归)。"""
    lines = [
        _enc({"type": "system", "subtype": "init",
              "session_id": "sess-usage-1", "model": "claude-test"}),
        _delta({"type": "text_delta", "text": "test output"}),
        _enc({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "test output"}],
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 80,
                      "cache_creation_input_tokens": 10}}}),
        _enc({"type": "result", "total_cost_usd": 0.005,
              "duration_ms": 500,
              "usage": {"input_tokens": 100, "output_tokens": 50,
                        "cache_read_input_tokens": 80,
                        "cache_creation_input_tokens": 10}}),
    ]

    async def fake_exec(*args, **kwargs):
        return _FakeProc(lines)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "test"})

            result = None
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "result":
                    result = m
                    break

            assert result is not None
            assert result["cost_usd"] == 0.005
            assert result["duration_ms"] == 500
            assert result["usage"]["input_tokens"] == 100
            assert result["usage"]["output_tokens"] == 50
            assert result["usage"]["cache_read"] == 80
            assert result["usage"]["cache_create"] == 10


# ------------------------------------------------------------------
# 测试 6: --include-partial-messages 出现在 CLI 参数中
# ------------------------------------------------------------------

def test_include_partial_messages_flag(tmp_path, monkeypatch):
    """验证 spawn 的 CLI 命令包含 --include-partial-messages。"""
    captured_cmd = {}

    async def fake_exec(*args, **kwargs):
        captured_cmd["args"] = list(args)
        return _FakeProc([
            _enc({"type": "system", "subtype": "init",
                  "session_id": "sess-flag-1", "model": "test"}),
            _enc({"type": "result", "total_cost_usd": 0,
                  "duration_ms": 1, "usage": {}}),
        ])

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.send_json({"text": "hi"})
            for _ in range(50):
                m = wsc.receive_json()
                if m.get("type") == "result":
                    break

    assert "--include-partial-messages" in captured_cmd.get("args", []), (
        f"CLI 命令应包含 --include-partial-messages: {captured_cmd.get('args')}"
    )
