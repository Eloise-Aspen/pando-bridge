"""Task 2：工具策略 API 端点的集成测试（GET/PUT/POST reset）。

用 TestClient 驱动 FastAPI app，验证 curl 正反用例。"""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from pando.server import create_app, TOOL_POLICY_DEFAULTS


def _make_client() -> TestClient:
    """构建一个最小配置的测试 app + TestClient。"""
    tmp = tempfile.mkdtemp()
    config = {
        "CLAUDE_EXE": "echo",
        "CLAUDE_CWD": tmp,
        "DATA_DIR": tmp,
    }
    app = create_app(config)
    return TestClient(app)


def test_get_returns_defaults():
    """GET /tool-policy 首次请求返回缺省策略。"""
    client = _make_client()
    resp = client.get("/tool-policy")
    assert resp.status_code == 200
    assert resp.json() == TOOL_POLICY_DEFAULTS


def test_put_updates_policy():
    """PUT /tool-policy 更新指定组的状态。"""
    client = _make_client()
    resp = client.put("/tool-policy", json={"file": "allow", "network": "ask"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["file"] == "allow"
    assert data["shell"] == "ask"  # 未更新的保持缺省
    assert data["network"] == "ask"
    # GET 验证持久化
    resp2 = client.get("/tool-policy")
    assert resp2.json() == data


def test_put_invalid_state_ignored():
    """PUT 传非法状态值被忽略，不报 500。"""
    client = _make_client()
    resp = client.put("/tool-policy", json={"file": "bogus"})
    assert resp.status_code == 200
    assert resp.json()["file"] == "ask"  # 保持缺省


def test_put_invalid_group_ignored():
    """PUT 传未知组名被忽略。"""
    client = _make_client()
    resp = client.put("/tool-policy", json={"alien": "allow"})
    assert resp.status_code == 200
    assert resp.json() == TOOL_POLICY_DEFAULTS


def test_put_non_json_returns_400():
    """PUT 发送非 JSON 内容返回 400。"""
    client = _make_client()
    resp = client.put("/tool-policy", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_put_non_object_returns_400():
    """PUT 发送 JSON 数组返回 400。"""
    client = _make_client()
    resp = client.put("/tool-policy", json=["file", "allow"])
    assert resp.status_code == 400


def test_reset_returns_defaults():
    """POST /tool-policy/reset 重置为缺省。"""
    client = _make_client()
    # 先改
    client.put("/tool-policy", json={"file": "allow", "shell": "deny", "network": "allow"})
    # 重置
    resp = client.post("/tool-policy/reset")
    assert resp.status_code == 200
    assert resp.json() == TOOL_POLICY_DEFAULTS
    # GET 确认
    resp2 = client.get("/tool-policy")
    assert resp2.json() == TOOL_POLICY_DEFAULTS
