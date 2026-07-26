"""对话管理:PATCH /sessions/{id} 改名(含空串回退自动标题)、
/sessions 的 offset 分页、DELETE 的级联清理回归。"""

import sqlite3
from datetime import datetime, timedelta, timezone

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


def _seed_session(db_path, sid, first_msg=None, minutes_ago=0):
    """直接建一个会话行(+可选首条用户消息),模拟真实聊天落库的结果。
    minutes_ago 控制 updated_at,用于稳定列表排序。"""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO sessions (id, title, model, created_at, updated_at, cwd_key) "
        "VALUES (?, '', '', ?, ?, '')",
        (sid, ts, ts),
    )
    if first_msg is not None:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, metadata, created_at) "
            "VALUES (?, 'user', ?, '{}', ?)",
            (sid, first_msg, ts),
        )
    conn.commit()
    conn.close()


def _titles(client, **params):
    return {s["id"]: s["title"] for s in client.get("/sessions", params=params).json()}


def test_rename_then_revert_to_auto_title(tmp_path):
    app = create_app(_config(tmp_path))
    db = tmp_path / "data" / "chat.db"
    with TestClient(app) as client:
        _seed_session(db, "s1", first_msg="帮我看看这段代码")

        # 未改名:标题 = 首条用户消息
        assert _titles(client)["s1"] == "帮我看看这段代码"

        # 改名:生效标题即刻返回,列表随之变
        resp = client.patch("/sessions/s1", json={"title": "  代码review  "})
        assert resp.status_code == 200
        assert resp.json() == {"id": "s1", "title": "代码review", "manual": True}
        assert _titles(client)["s1"] == "代码review"

        # 传空串 = 清空手动标题,回退首条用户消息
        resp = client.patch("/sessions/s1", json={"title": ""})
        assert resp.status_code == 200
        assert resp.json() == {"id": "s1", "title": "帮我看看这段代码", "manual": False}
        assert _titles(client)["s1"] == "帮我看看这段代码"


def test_rename_does_not_reorder_list(tmp_path):
    """改名不碰 updated_at——老对话不该因为改个名就顶到列表最前。"""
    app = create_app(_config(tmp_path))
    db = tmp_path / "data" / "chat.db"
    with TestClient(app) as client:
        _seed_session(db, "new", first_msg="新的", minutes_ago=1)
        _seed_session(db, "old", first_msg="旧的", minutes_ago=999)

        client.patch("/sessions/old", json={"title": "改了名的老对话"})
        order = [s["id"] for s in client.get("/sessions").json()]
        assert order == ["new", "old"]


def test_rename_unknown_session_404_and_bad_body_400(tmp_path):
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        assert client.patch("/sessions/nope", json={"title": "x"}).status_code == 404

        db = tmp_path / "data" / "chat.db"
        _seed_session(db, "s1", first_msg="在")
        assert client.patch("/sessions/s1", json={}).status_code == 400
        assert client.patch("/sessions/s1", json={"title": 42}).status_code == 400


def test_list_sessions_offset_pagination(tmp_path):
    """35 个会话:首屏 30 + offset=30 拿到剩下 5 个,两页零重叠。"""
    app = create_app(_config(tmp_path))
    db = tmp_path / "data" / "chat.db"
    with TestClient(app) as client:
        for i in range(35):
            _seed_session(db, f"s{i:02d}", first_msg=f"第 {i} 个对话", minutes_ago=i)

        page1 = client.get("/sessions", params={"limit": 30}).json()
        page2 = client.get("/sessions", params={"limit": 30, "offset": 30}).json()

        assert len(page1) == 30
        assert len(page2) == 5
        ids1 = [s["id"] for s in page1]
        ids2 = [s["id"] for s in page2]
        assert not set(ids1) & set(ids2)
        # 全集齐了,且仍是 updated_at 倒序(s00 最新)
        assert ids1 + ids2 == [f"s{i:02d}" for i in range(35)]


def test_delete_session_removes_messages(tmp_path):
    """既有端点回归:删除后 sessions/messages 两张表都不留行。"""
    app = create_app(_config(tmp_path))
    db = tmp_path / "data" / "chat.db"
    with TestClient(app) as client:
        _seed_session(db, "s1", first_msg="要被删掉的对话")

        assert client.delete("/sessions/s1").json() == {"ok": True}

        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='s1'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='s1'"
        ).fetchone()[0] == 0
        conn.close()
        assert client.get("/sessions").json() == []
