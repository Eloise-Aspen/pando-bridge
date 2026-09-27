"""会话更新通知走真实 WS，生成子进程一律禁止。"""
import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pando import create_app
from starlette.websockets import WebSocket


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
        assert client.get("/sessions/old/messages").headers["x-session-id"] == "old"
        assert client.get("/sessions/old/messages?resolve_tail=true").headers["x-session-id"] == "tail"
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


def test_resolved_history_includes_all_successors_but_not_root(tmp_path):
    app = create_app({"DATA_DIR": tmp_path, "PLUGINS": [], "PORT": 8767})
    with TestClient(app) as client:
        conn = sqlite3.connect(tmp_path / "chat.db")
        # root->A->B->C：从A补查必须收A/B/C，不能重拉root，也不能只取C。
        for sid, parent in [("root", None), ("a", "root"), ("b", "a"), ("c", "b")]:
            conn.execute("INSERT INTO sessions (id, parent_session_id, created_at, updated_at) "
                         "VALUES (?, ?, '2026-01-01', '2026-01-01')", (sid, parent))
            conn.execute("INSERT INTO messages (session_id, role, content, created_at) "
                         "VALUES (?, 'assistant', ?, '2026-01-01')", (sid, sid))
        conn.commit(); conn.close()
        original = client.get("/sessions/a/messages").json()
        assert [x["content"] for x in original if x["role"] == "assistant"] == ["a"]
        response = client.get("/sessions/a/messages?resolve_tail=true")
        assert response.headers["x-session-id"] == "c"
        messages = response.json()
        assert [x["session_id"] for x in messages if x["role"] == "assistant"] == ["a", "b", "c"]
        assert [x["parent_session_id"] for x in messages if x["role"] == "carryover_boundary"] == ["root", "a", "b"]


def test_slow_connection_times_out_without_blocking_other_client(tmp_path, monkeypatch):
    original_send = WebSocket.send_text
    slow = []
    cancelled = threading.Event()
    async def send(self, text):
        frame = json.loads(text)
        if frame["type"] == "hello" and not slow:
            slow.append(self)
        if frame["type"] == "session_updated" and self is slow[0]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            await original_send(self, text)
    monkeypatch.setattr(WebSocket, "send_text", send)
    app = create_app({"DATA_DIR": tmp_path, "PLUGINS": [], "PORT": 8767,
                      "SESSION_UPDATE_TIMEOUT": 0.5})
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as stalled, client.websocket_connect("/ws") as fast:
            stalled.receive_json(); fast.receive_json()
            for connection in (stalled, fast):
                connection.send_json({"switch_session": "target"}); connection.receive_json()
            app.state.notify_session_updated("target")
            assert fast.receive_json()["type"] == "session_updated"
            assert not cancelled.is_set(), "fast client waited for slow-client timeout"
            assert cancelled.wait(timeout=2), "slow send was not cancelled within its timeout"
