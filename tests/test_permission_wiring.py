"""Task 3：run_claude 的权限透传 CLI 接线 + 内部回调端点↔WS 往返。

- 开关默认关：run_claude 不追加任何 permission 参数（完成标准 4：存量用户零感知）。
- 开关开：追加 --permission-prompt-tool + --mcp-config，config 内含本连接 token 与回调地址。
- 端点往返：POST /internal/permission → WS 收到 permission_request → 回 permission_response
  → 端点返回对应 decision（用线程跑 POST，主线程读/写 WS，二者在同一 ASGI 事件循环上并发）。

用假子进程(monkeypatch create_subprocess_exec)捕获 run_claude 拼的 argv，不依赖真实 claude。"""

import json
import threading

from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app


def _config(tmp_path, **extra):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }
    cfg.update(extra)
    return cfg


class _EchoProc:
    """吐 init→result 两行即结束的假子进程；把 run_claude 拼的 argv 存到 captured。"""

    def __init__(self, captured, argv):
        captured["argv"] = list(argv)
        self._lines = [
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "s1", "model": "stub"}).encode(),
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0,
                        "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(),
        ]

    class _Stdout:
        def __init__(self, lines):
            self._lines = lines
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

    @property
    def stdout(self):
        return self._Stdout(self._lines)

    @property
    def stderr(self):
        return self._Stderr()

    returncode = 0

    async def wait(self):
        return 0


def _patch_exec(monkeypatch, captured):
    async def fake_exec(*argv, **kwargs):
        return _EchoProc(captured, argv)
    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)


def _drain(wsc, sent_text=True):
    """发一条消息并读到 result，返回中途收到的所有帧。"""
    wsc.send_json({"text": "hi"})
    frames = []
    for _ in range(60):
        m = wsc.receive_json()
        frames.append(m)
        if m.get("type") == "result":
            break
    return frames


def test_off_by_default_no_permission_flags(tmp_path, monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, captured)
    app = create_app(_config(tmp_path))  # 未开开关
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()  # hello
            _drain(wsc)
    argv = captured["argv"]
    assert "--permission-prompt-tool" not in argv
    assert "--mcp-config" not in argv


def test_on_appends_permission_flags(tmp_path, monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, captured)
    app = create_app(_config(tmp_path, PERMISSION_PASSTHROUGH=True,
                             PERMISSION_CALLBACK_URL="http://127.0.0.1:9/internal/permission"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            _drain(wsc)
    argv = captured["argv"]
    assert "--permission-prompt-tool" in argv
    i = argv.index("--permission-prompt-tool")
    assert argv[i + 1] == "mcp__pando_permission__approve"
    assert "--mcp-config" in argv
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    env = cfg["mcpServers"]["pando_permission"]["env"]
    assert env["PANDO_PERMISSION_CALLBACK_URL"] == "http://127.0.0.1:9/internal/permission"
    assert env["PANDO_PERMISSION_TOKEN"]  # 非空 token


def _run_endpoint_roundtrip(tmp_path, monkeypatch, allow):
    """开开关，跑 端点→WS→回帧 往返；返回 (POST 响应 dict, 收到的 permission_request 帧)。"""
    captured = {}
    _patch_exec(monkeypatch, captured)
    app = create_app(_config(tmp_path, PERMISSION_PASSTHROUGH=True, PERMISSION_TIMEOUT=5))
    result_box = {}
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()  # hello
            # 触发一轮 run_claude，从 mcp-config 里取出本连接 token
            _drain(wsc)
            token = json.loads(captured["argv"][captured["argv"].index("--mcp-config") + 1]) \
                ["mcpServers"]["pando_permission"]["env"]["PANDO_PERMISSION_TOKEN"]

            # POST 会阻塞等决策 → 放到线程里，主线程读 permission_request 并回 response
            def do_post():
                r = client.post("/internal/permission", json={
                    "token": token, "tool_name": "Write",
                    "input": {"file_path": "a.txt"}, "tool_use_id": "tu1"})
                result_box["resp"] = r.json()

            th = threading.Thread(target=do_post)
            th.start()
            req_frame = None
            for _ in range(60):
                m = wsc.receive_json()
                if m.get("type") == "permission_request":
                    req_frame = m
                    break
            assert req_frame is not None, "未收到 permission_request modal 帧"
            wsc.send_json({"type": "permission_response",
                           "request_id": req_frame["request_id"], "allow": allow})
            th.join(timeout=10)
    return result_box["resp"], req_frame


def test_endpoint_roundtrip_allow(tmp_path, monkeypatch):
    resp, frame = _run_endpoint_roundtrip(tmp_path, monkeypatch, allow=True)
    assert resp["decision"] == "allow"
    assert frame["tool"] == "Write"
    assert frame["input"] == {"file_path": "a.txt"}
    assert frame["tool_use_id"] == "tu1"


def test_endpoint_roundtrip_deny(tmp_path, monkeypatch):
    resp, _ = _run_endpoint_roundtrip(tmp_path, monkeypatch, allow=False)
    assert resp["decision"] == "deny"


def test_endpoint_unknown_token_denies(tmp_path, monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, captured)
    app = create_app(_config(tmp_path, PERMISSION_PASSTHROUGH=True))
    with TestClient(app) as client:
        r = client.post("/internal/permission", json={"token": "ghost", "tool_name": "Write"})
    assert r.json()["decision"] == "deny"
