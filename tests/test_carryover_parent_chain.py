"""parent 链（feat-carryover-auto-trigger Task 3，裁决 5）。

断言：
1. carryover 成功时新会话行写入 parent_session_id
2. 历史端点沿链向前拼接展示，顺序由早到晚
3. 限深：链再长也只回溯 CARRYOVER_CHAIN_MAX_DEPTH 段
4. 防环：parent 指回自己/形成环时不死循环
5. 干净重开（clean）不写 parent；降级路径同样不写
6. 消息仍归属各自会话——读侧拼接，不往库里复制
"""

import json
import sqlite3
import uuid

from fastapi.testclient import TestClient

from pando import create_app

from tests.test_carryover_auto_trigger import (  # 复用假子进程与装置
    _config, _drain_to, _install, _seed_transcript,
)


def _chat_db(tmp_path):
    return sqlite3.connect(str(tmp_path / "data" / "chat.db"))


def _parent_of(tmp_path, sid):
    conn = _chat_db(tmp_path)
    row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (sid,)).fetchone()
    conn.close()
    return (row[0] or "") if row else None


def test_carryover_writes_parent(tmp_path, monkeypatch):
    _install(monkeypatch, ["sess-old"], cache_read=10)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    assert forged["carryover"] is True
    assert _parent_of(tmp_path, forged["session_id"]) == "sess-old"


def test_history_spans_chain(tmp_path, monkeypatch):
    """换窗后刷新页面：历史端点把两段会话的消息按时序拼全。"""
    _install(monkeypatch, ["sess-old"], cache_read=10)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "换窗前的话"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            wsc.send_json({"text": "换窗后的话"})
            _drain_to(wsc, "result")

        history = client.get(f"/sessions/{forged['session_id']}/messages").json()

    contents = [m["content"] for m in history if m["role"] == "user"]
    assert "换窗前的话" in contents and "换窗后的话" in contents
    assert contents.index("换窗前的话") < contents.index("换窗后的话")
    # 归属未被改写：早的那条仍记在源会话名下
    owners = {m["content"]: m["session_id"] for m in history}
    assert owners["换窗前的话"] == "sess-old"
    assert owners["换窗后的话"] == forged["session_id"]
    # 源会话自己的历史不受影响（不含新会话的消息）
    src = client.get("/sessions/sess-old/messages").json()
    assert "换窗后的话" not in [m["content"] for m in src]
    # 归属会话字段来自 messages.session_id 本身，不是拼接时贴上的标签
    assert {m["session_id"] for m in src} == {"sess-old"}


def _seed_chain(tmp_path, ids):
    """直接造一条 parent 链：ids[0] 最早，每段一条消息。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    conn = _chat_db(tmp_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT DEFAULT '', model TEXT DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            last_archived_id INTEGER DEFAULT 0, cwd_key TEXT DEFAULT '',
            parent_session_id TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}', created_at TEXT NOT NULL);
    """)
    for i, sid in enumerate(ids):
        parent = ids[i - 1] if i else ""
        conn.execute("INSERT OR REPLACE INTO sessions (id, created_at, updated_at, parent_session_id) "
                     "VALUES (?, 'x', 'x', ?)", (sid, parent))
        conn.execute("INSERT INTO messages (session_id, role, content, metadata, created_at) "
                     "VALUES (?, 'user', ?, '{}', 'x')", (sid, f"消息 {sid}"))
    conn.commit()
    conn.close()


def test_chain_depth_limited(tmp_path, monkeypatch):
    """8 段长链、限深 3 → 只回溯到第 3 段，更早的不出现。"""
    ids = [f"s{i}" for i in range(8)]
    _seed_chain(tmp_path, ids)
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=3))
    with TestClient(app) as client:
        history = client.get(f"/sessions/{ids[-1]}/messages").json()

    owners = [m["session_id"] for m in history]
    assert owners == ["s5", "s6", "s7"]


def test_chain_cycle_guarded(tmp_path, monkeypatch):
    """parent 成环（外部写脏）时不死循环，每段只出现一次。"""
    _seed_chain(tmp_path, ["a", "b", "c"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'c' WHERE id = 'a'")  # a→c→b→a
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=50))
    with TestClient(app) as client:
        history = client.get("/sessions/c/messages").json()

    owners = [m["session_id"] for m in history]
    assert sorted(owners) == ["a", "b", "c"]
    assert len(owners) == len(set(owners))


def test_self_parent_guarded(tmp_path, monkeypatch):
    _seed_chain(tmp_path, ["solo"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'solo' WHERE id = 'solo'")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=50))
    with TestClient(app) as client:
        history = client.get("/sessions/solo/messages").json()
    assert [m["session_id"] for m in history] == ["solo"]


def test_degraded_forge_writes_no_parent(tmp_path, monkeypatch):
    """降级（纯重置）后开出的新会话不写 parent——本来就要从头开始。"""
    _install(monkeypatch, ["sess-old", "sess-fresh"], cache_read=10)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"forge": True})          # 无 transcript → 降级
            forged = _drain_to(wsc, "forged")
            assert forged["carryover"] is False
            wsc.send_json({"text": "降级后第一句"})
            _drain_to(wsc, "result")

        history = client.get("/sessions/sess-fresh/messages").json()

    assert _parent_of(tmp_path, "sess-fresh") == ""
    assert [m["content"] for m in history if m["role"] == "user"] == ["降级后第一句"]
