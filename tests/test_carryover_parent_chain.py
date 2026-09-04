"""parent 链（feat-carryover-auto-trigger Task 3，裁决 5）。

断言：
1. carryover 成功时新会话行写入 parent_session_id
2. 历史端点**按段分页**（fix-carryover-chat-key Fix D）：初始只回链尾段，开头带
   `carryover_boundary` 标记携带 parent id；按 parent id 再请求即取那一段
3. 链根无标记；会话列表标题固定取链根、条数整链
4. 防环：parent 指回自己/形成环时不死循环（列表折叠与逐段请求都不卡死）
5. 干净重开（clean）不写 parent；降级路径同样不写
6. 消息仍归属各自会话——不往库里复制
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


def test_history_paged_by_segment_after_forge(tmp_path, monkeypatch):
    """换窗后刷新页面：历史端点只回链尾段 + 开头边界标记；按标记里的 parent
    再请求一次即得换窗前那一段（链根，无标记）。"""
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

        tail = client.get(f"/sessions/{forged['session_id']}/messages").json()
        assert tail[0]["role"] == "carryover_boundary"
        assert tail[0]["parent_session_id"] == "sess-old"
        assert tail[0]["metadata"]["carryover_parent"] == "sess-old"
        src = client.get(f"/sessions/{tail[0]['parent_session_id']}/messages").json()

    # 尾段只含自己的消息，且归属是自己
    assert [m["content"] for m in tail if m["role"] == "user"] == ["换窗后的话"]
    assert {m["session_id"] for m in tail} == {forged["session_id"]}
    # 源段只含换窗前的消息，是链根 → 没有边界标记
    assert [m["content"] for m in src if m["role"] == "user"] == ["换窗前的话"]
    assert all(m["role"] != "carryover_boundary" for m in src)
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


def _walk(client, sid):
    """模拟前端逐段向前加载：按标记里的 parent 一段段请求，已加载集合防环。
    返回 (由早到晚的段 id 序列, 每段消息数)。"""
    order, counts, loaded = [], {}, set()
    cur = sid
    while cur and cur not in loaded:
        seg = client.get(f"/sessions/{cur}/messages").json()
        loaded.add(cur)
        order.append(cur)
        marker = seg[0] if seg and seg[0]["role"] == "carryover_boundary" else None
        counts[cur] = len(seg) - (1 if marker else 0)
        cur = marker["parent_session_id"] if marker else None
    order.reverse()
    return order, counts


def test_long_chain_paged_segment_by_segment(tmp_path, monkeypatch):
    """8 段链：初始只回链尾段（1 条 + 标记），逐段请求可一直走到链根，链根无标记；
    没有任何深度上限把开头截掉。列表标题固定取链根、条数整链。"""
    ids = [f"s{i}" for i in range(8)]
    _seed_chain(tmp_path, ids)
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        tail = client.get(f"/sessions/{ids[-1]}/messages").json()
        assert [m["role"] for m in tail] == ["carryover_boundary", "user"]
        assert tail[0]["parent_session_id"] == "s6"
        assert tail[1]["session_id"] == "s7"
        order, counts = _walk(client, ids[-1])
        root = client.get("/sessions/s0/messages").json()
        listed = client.get("/sessions").json()

    assert order == ids
    assert all(counts[s] == 1 for s in ids)
    assert root[0]["role"] == "user"               # 链根无标记
    assert [s["id"] for s in listed] == ["s7"]
    assert listed[0]["title"] == "消息 s0"
    assert listed[0]["msg_count"] == 8


def test_chain_cycle_guarded(tmp_path, monkeypatch):
    """parent 成环（外部写脏）：按段请求由已加载集合止步，每段只出现一次；
    会话列表折叠同样不死循环。"""
    _seed_chain(tmp_path, ["a", "b", "c"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'c' WHERE id = 'a'")  # a→c→b→a
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        order, _ = _walk(client, "c")
        listed = client.get("/sessions").json()

    assert sorted(order) == ["a", "b", "c"]
    assert len(order) == len(set(order))
    assert len(listed) == len({s["id"] for s in listed})


def test_self_parent_guarded(tmp_path, monkeypatch):
    """parent 指回自己：不当作有前驱，历史无标记，列表仍列出自己。"""
    _seed_chain(tmp_path, ["solo"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'solo' WHERE id = 'solo'")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        history = client.get("/sessions/solo/messages").json()
    assert [m["role"] for m in history] == ["user"]
    assert history[0]["session_id"] == "solo"


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
