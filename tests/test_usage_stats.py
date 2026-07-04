"""usage 表 + /usage/stats 端点:空态、按模型分组、今日/累计切分、旧库兼容迁移。"""

import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

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


def _insert_usage(db_path, model, inp, out, when=None):
    """直接往 usage 表塞一行,模拟 record_usage 在 result 事件里的落库。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO usage (session_id, model, input_tokens, output_tokens, "
        "cache_read, cache_create, created_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
        ("s1", model, inp, out, when or datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def test_usage_stats_empty(tmp_path):
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/usage/stats")
        assert resp.status_code == 200
        assert resp.json() == {"today": [], "total": []}


def test_usage_stats_groups_by_model_today_and_total(tmp_path):
    app = create_app(_config(tmp_path))
    db = tmp_path / "data" / "chat.db"
    with TestClient(app) as client:
        # 今日:两条 sonnet + 一条 opus
        _insert_usage(db, "claude-sonnet-4-6", 100, 20)
        _insert_usage(db, "claude-sonnet-4-6", 50, 10)
        _insert_usage(db, "claude-opus-4-8", 200, 40)
        # 历史(2020 年那条):只进 total,不进 today
        _insert_usage(db, "claude-sonnet-4-6", 999, 999, when="2020-01-01T00:00:00+00:00")

        data = client.get("/usage/stats").json()
        total = {r["model"]: r for r in data["total"]}
        today = {r["model"]: r for r in data["today"]}

        assert total["claude-sonnet-4-6"]["input_tokens"] == 100 + 50 + 999
        assert total["claude-sonnet-4-6"]["requests"] == 3
        assert total["claude-opus-4-8"]["input_tokens"] == 200
        # 今日排除历史那条
        assert today["claude-sonnet-4-6"]["input_tokens"] == 150
        assert today["claude-sonnet-4-6"]["requests"] == 2
        assert "claude-sonnet-4-6" in today and "claude-opus-4-8" in today


def test_usage_stats_survives_legacy_db_without_usage_table(tmp_path):
    """旧 chat.db(只有 sessions/messages,无 usage 表)启动不崩,端点正常返回。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db = data_dir / "chat.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, model TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            role TEXT, content TEXT, metadata TEXT, created_at TEXT);
    """)
    conn.commit()
    conn.close()

    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/usage/stats")
        assert resp.status_code == 200
        assert resp.json() == {"today": [], "total": []}
