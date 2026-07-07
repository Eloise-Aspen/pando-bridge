"""附件注入 /ws 编排（feat-attachment-upload Task 3）：
校验过的附件路径按固定格式注入消息、cwd 外/伪造路径被丢弃、纯附件（无文字）也能发。

用假子进程（monkeypatch create_subprocess_exec）回显 run_claude 拼出的 message
（run_claude 把 message 作为最后一个 argv），据此断言注入内容，不依赖真实 claude CLI。
真实 CC 读图/读 PDF 的能力已在 Task 1 spike 实测，这里只覆盖服务端注入/校验编排。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app


def _config(tmp_path):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }


class _EchoStdout:
    """吐三行 stream-json：init → assistant(text=回显的 message) → result。"""

    def __init__(self, message):
        self._lines = [
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "sess-1", "model": "stub"}).encode(),
            json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": message}]}}).encode(),
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0,
                        "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(),
        ]
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            return line
        raise StopAsyncIteration


class _Stderr:
    async def read(self):
        return b""


class _EchoProc:
    def __init__(self, message):
        self.stdout = _EchoStdout(message)
        self.stderr = _Stderr()
        self.returncode = 0

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


def _install_echo(monkeypatch):
    async def fake_exec(*args, **kwargs):
        message = args[-1]  # run_claude 把 message 作为最后一个 arg
        return _EchoProc(message)
    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)


def _upload_png(client):
    resp = client.post("/attachments", files={"file": ("a.png", b"\x89PNG\r\n\x1a\nx", "image/png")})
    assert resp.status_code == 200, resp.text
    return resp.json()["path"]


def _run_ws(client, payload):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello
        ws.send_json(payload)
        for _ in range(50):
            m = ws.receive_json()
            if m.get("type") == "result":
                return m
            if m.get("type") == "error":
                raise AssertionError(f"error frame: {m}")
    raise AssertionError("未收到 result 帧")


_NOTE_HEADER = "[用户上传了附件，请用 Read 工具查看以下文件]"


def test_valid_attachment_injected(tmp_path, monkeypatch):
    """完成标准 1/2 前置：有效附件路径按固定格式注入进 message，且以「相对 CLAUDE_CWD」
    形式注入（绝对挂载路径喂给 Windows claude.exe 会被 Read 误判 workspace 外 → 门控）。"""
    _install_echo(monkeypatch)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        path = _upload_png(client)
        result = _run_ws(client, {"text": "看看这个", "attachments": [path]})
        echoed = result["text"]  # = run_claude 收到的 message
        assert _NOTE_HEADER in echoed
        # 注入的是相对 CLAUDE_CWD 的路径（uploads/YYYYMM/uuid.png），不是绝对路径
        rel = Path(path).resolve().relative_to(tmp_path.resolve()).as_posix()
        assert rel in echoed
        assert str(Path(path).resolve()) not in echoed  # 绝对路径不得出现（回归锁）


def test_forged_path_dropped(tmp_path, monkeypatch):
    """安全：upload_dir 外的伪造路径被丢弃，不注入（防任意路径注入让 CC 读 /etc/passwd）。"""
    _install_echo(monkeypatch)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        result = _run_ws(client, {"text": "hi", "attachments": ["/etc/passwd", "../../secret.txt"]})
        echoed = result["text"]
        assert "/etc/passwd" not in echoed
        assert "secret.txt" not in echoed
        assert _NOTE_HEADER not in echoed  # 无有效附件 → 整段不注入


def test_pure_attachment_no_text(tmp_path, monkeypatch):
    """完成标准 4：纯附件（空文字）不被跳过，仍走 run_claude 且注入附件。"""
    _install_echo(monkeypatch)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        path = _upload_png(client)
        result = _run_ws(client, {"text": "", "attachments": [path]})
        echoed = result["text"]
        assert _NOTE_HEADER in echoed
        rel = Path(path).resolve().relative_to(tmp_path.resolve()).as_posix()
        assert rel in echoed


def test_pure_attachment_persists_placeholder(tmp_path, monkeypatch):
    """纯附件存库：user 内容存占位符 [附件]，metadata 记录附件文件名。"""
    _install_echo(monkeypatch)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        path = _upload_png(client)
        result = _run_ws(client, {"text": "", "attachments": [path]})
        sid = result["session_id"]
        msgs = client.get(f"/sessions/{sid}/messages").json()
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["content"] == "[附件]"
        assert user_msg["metadata"].get("attachments") == [Path(path).name]
