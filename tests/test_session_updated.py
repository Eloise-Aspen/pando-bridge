"""会话更新通知走真实 WS，生成子进程一律禁止。"""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pando import create_app


def test_session_update_routes_tail_and_worker_thread(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("real CLI forbidden")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    app = create_app({"DATA_DIR": tmp_path, "PLUGINS": [], "PORT": 8767,
                      "CLAUDE_CWD": str(tmp_path / "chat"),
                      "WORKSPACES": {"chat": {"path": str(tmp_path / "chat")},
                                     "work": {"path": str(tmp_path / "work")}},
                      "ARCHIVE_INTERVAL": 99999})
    with TestClient(app) as client:
        conn = sqlite3.connect(tmp_path / "chat.db")
        # 客厅旧窗接新窗，工位单独一条河；通知须按核心链尾语义匹配。
        conn.executemany(
            "INSERT INTO sessions (id, created_at, updated_at, cwd_key, parent_session_id) "
            "VALUES (?, '2026-01-01', '2026-01-01', ?, ?)",
            [("old", "chat", None), ("tail", "", "old"), ("work", "work", None)])
        conn.commit(); conn.close()
        assert app.state.is_chat_session("old")
        assert app.state.is_chat_session("tail")
        assert not app.state.is_chat_session("work")
        assert app.state.chain_tail("old") == "tail"
        with client.websocket_connect("/ws") as target, client.websocket_connect("/ws") as other:
            target.receive_json(); other.receive_json()
            target.send_json({"switch_session": "old"})
            assert target.receive_json()["session_id"] == "tail"
            other.send_json({"switch_session": "work"}); other.receive_json()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(app.state.notify_session_updated, "old").result(timeout=2)
            assert target.receive_json() == {"type": "session_updated", "session_id": "tail",
                                             "viewed_session_id": "tail"}
            # 排在通知之后的切会话回执作为栅栏：目标以外的连接不可夹进更新帧。
            other.send_json({"switch_session": "work"})
            assert other.receive_json()["type"] == "session_switched"
            target.send_json({"switch_session": "work"}); target.receive_json()
            app.state.notify_session_updated("tail")
            target.send_json({"switch_session": "work"})
            assert target.receive_json()["type"] == "session_switched"
        app.state.notify_session_updated("tail")
    assert True  # 断开后通知不报错，不启动任何生成路径。
