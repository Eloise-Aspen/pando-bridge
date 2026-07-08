"""Task 2：权限透传 MCP server 的回调链路 + JSON-RPC 路由单测。

用一个线程里的真 HTTP stub 冒充 bridge 回调端点，覆盖 allow / deny / 连不上（默拒）三条路；
再直接驱动 handle() 断言 initialize / tools/list / tools/call 的 JSON-RPC 形状。
不拉起真 claude —— Task 1 spike 已实测机制本身，这里只验本 server 的行为。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pando.permission_mcp as pm


class _BridgeStub(BaseHTTPRequestHandler):
    """回放固定 decision 的假 bridge；把收到的请求体存到类属性供断言。"""

    decision_response = {"decision": "allow"}
    last_body = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _BridgeStub.last_body = json.loads(self.rfile.read(length).decode("utf-8"))
        payload = json.dumps(self.decision_response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass  # 静音


def _serve():
    """起一个临时 HTTP server，返回 (server, url)。用完 shutdown。"""
    srv = HTTPServer(("127.0.0.1", 0), _BridgeStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}/internal/permission"


def test_request_decision_allow(monkeypatch):
    srv, url = _serve()
    try:
        _BridgeStub.decision_response = {"decision": "allow"}
        monkeypatch.setattr(pm, "CALLBACK_URL", url)
        monkeypatch.setattr(pm, "TOKEN", "tok-123")
        out = pm.request_decision("Write", {"file_path": "a.txt", "content": "x"}, "tu-1")
        assert out["behavior"] == "allow"
        assert out["updatedInput"] == {"file_path": "a.txt", "content": "x"}
        # bridge 收到的请求体带上了 token / tool_name / input / tool_use_id
        assert _BridgeStub.last_body["token"] == "tok-123"
        assert _BridgeStub.last_body["tool_name"] == "Write"
        assert _BridgeStub.last_body["tool_use_id"] == "tu-1"
    finally:
        srv.shutdown()


def test_request_decision_deny(monkeypatch):
    srv, url = _serve()
    try:
        _BridgeStub.decision_response = {"decision": "deny", "message": "用户拒绝"}
        monkeypatch.setattr(pm, "CALLBACK_URL", url)
        out = pm.request_decision("Bash", {"command": "rm -rf /"}, "tu-2")
        assert out["behavior"] == "deny"
        assert out["message"] == "用户拒绝"
    finally:
        srv.shutdown()


def test_request_decision_unreachable_defaults_deny(monkeypatch):
    # 指向一个没人监听的端口 → URLError → 默拒
    monkeypatch.setattr(pm, "CALLBACK_URL", "http://127.0.0.1:1/internal/permission")
    monkeypatch.setattr(pm, "HTTP_TIMEOUT", 1.0)
    out = pm.request_decision("Write", {"file_path": "a"}, "tu-3")
    assert out["behavior"] == "deny"


def test_request_decision_no_callback_defaults_deny(monkeypatch):
    monkeypatch.setattr(pm, "CALLBACK_URL", "")
    out = pm.request_decision("Write", {}, "tu-4")
    assert out["behavior"] == "deny"


def test_request_decision_malformed_defaults_deny(monkeypatch):
    # bridge 返回非法 JSON → 默拒
    class _BadStub(_BridgeStub):
        def do_POST(self):
            # 先读掉请求体，避免不 drain 触发连接重置（否则默拒路径由 OSError 而非 JSON 错误命中）
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = b"notjson"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), _BadStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/x"
    try:
        monkeypatch.setattr(pm, "CALLBACK_URL", url)
        out = pm.request_decision("Write", {}, "tu-5")
        assert out["behavior"] == "deny"
    finally:
        srv.shutdown()


def test_handle_initialize_echoes_protocol():
    resp = pm.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25"}})
    assert resp["id"] == 0
    assert resp["result"]["protocolVersion"] == "2025-11-25"
    assert resp["result"]["serverInfo"]["name"] == "pando-permission"


def test_handle_tools_list_exposes_tool():
    resp = pm.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert pm.TOOL_NAME in names


def test_handle_initialized_notification_no_reply():
    assert pm.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_handle_tools_call_returns_text_json(monkeypatch):
    monkeypatch.setattr(pm, "request_decision",
                        lambda *a, **k: {"behavior": "deny", "message": "no"})
    resp = pm.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": pm.TOOL_NAME,
                                 "arguments": {"tool_name": "Write", "input": {}, "tool_use_id": "z"}}})
    text = resp["result"]["content"][0]["text"]
    assert json.loads(text) == {"behavior": "deny", "message": "no"}
