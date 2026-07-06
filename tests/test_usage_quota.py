"""/usage/quota 端点:响应清洗最小形状、凭证发现与过期短路、无凭证降级不 500。
全离线(不触网):只覆盖凭证读取 + 清洗 + 降级路由;真实官方端点调用归真机验收。"""

import json
import time

from fastapi.testclient import TestClient

from pando import create_app
from pando import server as S


def _config(tmp_path):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }


def _write_creds(dir_path, access_token="tok-abc", expires_at=None):
    dir_path.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": access_token}
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    (dir_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def test_clean_quota_keeps_minimal_shape_and_drops_extras():
    """只保留 available + 两条 {utilization, resets_at},limit_dollars/limits/spend 等一律丢弃。"""
    raw = {
        "five_hour": {"utilization": 61.0, "resets_at": "2026-07-06T02:59:59Z",
                      "limit_dollars": None, "used_dollars": 3.2},
        "seven_day": {"utilization": 30.0, "resets_at": "2026-07-08T18:59:59Z"},
        "limits": [{"kind": "session", "percent": 61}],
        "spend": {"used": 0},
    }
    assert S._clean_quota(raw) == {
        "available": True,
        "five_hour": {"utilization": 61.0, "resets_at": "2026-07-06T02:59:59Z"},
        "seven_day": {"utilization": 30.0, "resets_at": "2026-07-08T18:59:59Z"},
    }


def test_clean_quota_shape_change_gives_none_fields():
    """响应结构变化(字段缺失)时给 None,不抛错、不透传意外结构。"""
    cleaned = S._clean_quota({})
    assert cleaned["available"] is True
    assert cleaned["five_hour"] == {"utilization": None, "resets_at": None}
    assert cleaned["seven_day"] == {"utilization": None, "resets_at": None}


def test_read_token_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert S._read_oauth_token() is None


def test_read_token_fresh_ok_expired_and_noexp(tmp_path, monkeypatch):
    cfg = tmp_path / "cc"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    # 新鲜(1h 后过期)→ 读到 token
    _write_creds(cfg, "fresh-tok", expires_at=int((time.time() + 3600) * 1000))
    assert S._read_oauth_token() == "fresh-tok"
    # 已过期(1h 前)→ 短路 None,不拿死 token 打端点
    _write_creds(cfg, "stale-tok", expires_at=int((time.time() - 3600) * 1000))
    assert S._read_oauth_token() is None
    # 无 expiresAt 字段 → 放行,交端点判定
    _write_creds(cfg, "no-exp-tok")
    assert S._read_oauth_token() == "no-exp-tok"


def test_quota_route_degrades_without_creds(tmp_path, monkeypatch):
    """无凭证:200 + {available:false},绝不 500(降级不影响聊天)。"""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/usage/quota")
        assert resp.status_code == 200
        assert resp.json() == {"available": False}
