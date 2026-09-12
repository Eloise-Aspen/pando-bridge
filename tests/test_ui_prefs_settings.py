"""界面偏好搬服务端（fix-settings-persistence Task 1，裁决 4/5/6）。

断言：
1. POST /settings 写入四个字符串键后 GET /settings 原样读回
2. 超长昵称（>64 字符）被截断入库，不抛错
3. 非法 defaultEffort（如 "turbo"）写入后读回空串
4. 既有行为参数（auto_carryover_*）的读写与类型强制完全未受影响
"""

import pytest
from fastapi.testclient import TestClient

from pando import create_app

UI_KEYS = ("defaultModel", "defaultEffort", "userNickname", "assistantName")


def _config(tmp_path, **overrides):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path / "cwd"),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 3600,
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }
    cfg.update(overrides)
    return cfg


def test_ui_prefs_default_to_empty_strings(tmp_path):
    """出厂默认：四个界面偏好键都在，且都是空串（裁决 4）。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        body = client.get("/settings").json()
    for key in UI_KEYS:
        assert body[key] == "", key


def test_ui_prefs_round_trip(tmp_path):
    """完成标准 1：四个字符串键写进去，原样读回来，且重建 app（= 重启）后仍在。"""
    cfg = _config(tmp_path)
    payload = {
        "defaultModel": "claude-sonnet-4-5",
        "defaultEffort": "high",
        "userNickname": "阿云",
        "assistantName": "Aria",
    }
    with TestClient(create_app(cfg)) as client:
        out = client.post("/settings", json=payload).json()
        for key, value in payload.items():
            assert out[key] == value, key
        got = client.get("/settings").json()
        for key, value in payload.items():
            assert got[key] == value, key
    # 同一 DATA_DIR 重建 app = 重启服务，设置仍在（这正是本 bug 要解决的事）
    with TestClient(create_app(cfg)) as client:
        got = client.get("/settings").json()
    for key, value in payload.items():
        assert got[key] == value, key


def test_long_nickname_truncated_not_error(tmp_path):
    """完成标准 2：超长昵称截断到 64 字符入库，返回 200 不抛错。"""
    long_name = "喵" * 200
    with TestClient(create_app(_config(tmp_path))) as client:
        r = client.post("/settings", json={"userNickname": long_name})
        assert r.status_code == 200
        assert r.json()["userNickname"] == long_name[:64]
        assert client.get("/settings").json()["userNickname"] == long_name[:64]


@pytest.mark.parametrize("bad", ["turbo", "TURBO", "低", "high " * 30, None, 123.5, {"a": 1}])
def test_invalid_effort_reads_back_empty(tmp_path, bad):
    """完成标准 3：非白名单 defaultEffort 一律读回空串，不报错、不拦截。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        r = client.post("/settings", json={"defaultEffort": bad})
        assert r.status_code == 200
        assert r.json()["defaultEffort"] == ""
        assert client.get("/settings").json()["defaultEffort"] == ""


@pytest.mark.parametrize("good", ["", "low", "medium", "high", "xhigh", "max"])
def test_valid_efforts_accepted(tmp_path, good):
    """白名单内档位（含空串 = 默认）原样保留。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        assert client.post("/settings", json={"defaultEffort": good}).json()["defaultEffort"] == good


def test_model_id_is_free_text(tmp_path):
    """裁决 6：服务端不校验模型 id 是否在候选列表里，自定义值原样存。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        out = client.post("/settings", json={"defaultModel": "my-custom-model-x"}).json()
    assert out["defaultModel"] == "my-custom-model-x"


def test_non_string_ui_pref_falls_back_to_empty(tmp_path):
    """裁决 6：非法值回落空串，不抛错（None / 结构体）。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        r = client.post("/settings", json={"userNickname": None, "assistantName": ["x"]})
        assert r.status_code == 200
        assert r.json()["userNickname"] == "" and r.json()["assistantName"] == ""


def test_behaviour_params_unaffected(tmp_path):
    """完成标准 4：既有行为参数的读写与类型强制完全没被字符串分支影响。"""
    cfg = _config(
        tmp_path,
        AUTO_CARRYOVER_ENABLED=True,
        AUTO_CARRYOVER_SOFT_TOKENS=111,
        AUTO_CARRYOVER_HARD_TOKENS=222,
        AUTO_CARRYOVER_IDLE_MINUTES=3,
    )
    with TestClient(create_app(cfg)) as client:
        body = client.get("/settings").json()
        assert body["auto_carryover_enabled"] is True
        assert body["auto_carryover_soft_tokens"] == 111
        assert body["auto_carryover_hard_tokens"] == 222
        assert body["auto_carryover_idle_minutes"] == 3

        # 同一次请求里混写界面偏好，行为参数照常生效、类型照常强制
        out = client.post("/settings", json={
            "auto_carryover_enabled": False,
            "auto_carryover_soft_tokens": "5000",      # 字符串数字仍按 int 强制
            "auto_carryover_idle_minutes": 7,          # int 仍按 float 强制
            "userNickname": "阿云",
        }).json()
        assert out["auto_carryover_enabled"] is False
        assert out["auto_carryover_soft_tokens"] == 5000
        assert isinstance(out["auto_carryover_soft_tokens"], int)
        assert out["auto_carryover_idle_minutes"] == 7.0
        assert isinstance(out["auto_carryover_idle_minutes"], float)
        assert out["userNickname"] == "阿云"

        # 行为参数的坏值仍静默忽略、回落默认，不因新分支变成 "不是数字" 这种字符串
        bad = client.post("/settings", json={"auto_carryover_hard_tokens": "不是数字"}).json()
        assert bad["auto_carryover_hard_tokens"] == 222


def test_unknown_keys_still_ignored(tmp_path):
    """白名单扩表后，表外键（尤其是配置/凭证类）仍然进不来。"""
    with TestClient(create_app(_config(tmp_path))) as client:
        out = client.post("/settings", json={
            "MEMORY_SERVICE_URL": "http://evil",
            "claude_exe": "/bin/sh",
            "theme": "dark",
        }).json()
    assert "MEMORY_SERVICE_URL" not in out
    assert "claude_exe" not in out
    assert "theme" not in out
