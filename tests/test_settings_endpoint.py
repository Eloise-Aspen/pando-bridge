"""行为设置端点（feat-carryover-auto-trigger Task 1，裁决 6）。

断言：
1. GET /settings 返回 config 出厂默认
2. POST /settings 写入后 GET 生效，且只认白名单键
3. 设置持久化——同一 DATA_DIR 重建 app（= 重启）后仍在
4. 非法值/非法键静默忽略，不抛 500
"""

import pytest
from fastapi.testclient import TestClient

from pando import create_app


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


def test_defaults_from_config(tmp_path):
    app = create_app(_config(
        tmp_path,
        AUTO_CARRYOVER_ENABLED=True,
        AUTO_CARRYOVER_SOFT_TOKENS=111,
        AUTO_CARRYOVER_HARD_TOKENS=222,
        AUTO_CARRYOVER_IDLE_MINUTES=3,
    ))
    with TestClient(app) as client:
        body = client.get("/settings").json()
    assert body["auto_carryover_enabled"] is True
    assert body["auto_carryover_soft_tokens"] == 111
    assert body["auto_carryover_hard_tokens"] == 222
    assert body["auto_carryover_idle_minutes"] == 3


def test_post_overrides_and_reads_back(tmp_path):
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=120_000))
    with TestClient(app) as client:
        out = client.post("/settings", json={"auto_carryover_soft_tokens": 5000}).json()
        assert out["auto_carryover_soft_tokens"] == 5000
        assert client.get("/settings").json()["auto_carryover_soft_tokens"] == 5000


def test_settings_survive_restart(tmp_path):
    cfg = _config(tmp_path, AUTO_CARRYOVER_ENABLED=True)
    with TestClient(create_app(cfg)) as client:
        client.post("/settings", json={"auto_carryover_enabled": False})
    # 同一 DATA_DIR 重建 app = 重启服务
    with TestClient(create_app(cfg)) as client:
        assert client.get("/settings").json()["auto_carryover_enabled"] is False


def test_unknown_keys_ignored(tmp_path):
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        out = client.post("/settings", json={
            "MEMORY_SERVICE_URL": "http://evil",
            "claude_exe": "/bin/sh",
            "auto_carryover_hard_tokens": 999,
        }).json()
    assert "MEMORY_SERVICE_URL" not in out and "claude_exe" not in out
    assert out["auto_carryover_hard_tokens"] == 999


@pytest.mark.parametrize("bad", [
    {"auto_carryover_soft_tokens": "不是数字"},
    {"auto_carryover_idle_minutes": None},
])
def test_bad_values_ignored_not_500(tmp_path, bad):
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=120_000))
    with TestClient(app) as client:
        r = client.post("/settings", json=bad)
        assert r.status_code == 200
        assert r.json()["auto_carryover_soft_tokens"] == 120_000


def test_non_object_body_rejected(tmp_path):
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        assert client.post("/settings", json=[1, 2]).status_code == 400
