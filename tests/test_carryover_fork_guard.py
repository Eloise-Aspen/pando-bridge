"""链分叉防护（fix-carryover-chat-key Fix E）。

真机实证（data_test 2026-09-04 11:45–11:48）：会话被自动换窗两次、两个孩子共一个父，
会话列表出现两条同题同条数的行。成因是 forge 在途时结束的一轮，其 result 帧带旧
session_id 晚于 forged 帧到达，把前端会话 id 顶回旧的。

断言：
1. 已有后继的会话：一轮结束后超线也不自动换窗，日志见 `already superseded`
2. 已有后继的会话：手动 forge 不再造第二个孩子，回 superseded 帧（不重置）并跟到链尾
3. switch_session 指向已有后继的会话 → 重定向到链尾，日志见 `redirected to chain tail`
4. 消息发到已有后继的会话 → 用链尾处理本轮，result 帧带链尾 id
5. 前端「迟到 result 帧不覆盖新 id」的防御在 fe-forge.js，验证方式见该文件注释
"""

import logging
import sqlite3

from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app

from tests.test_carryover_auto_trigger import (  # 复用假子进程与装置
    _Spy, _collect_to, _config, _drain_to, _install, _seed_transcript,
)
from tests.test_carryover_parent_chain import _chat_db, _seed_chain


def _add_child(tmp_path, parent, child):
    """外部（另一条连接的 forge）给 parent 写一个孩子。"""
    conn = _chat_db(tmp_path)
    conn.execute("INSERT OR REPLACE INTO sessions (id, created_at, updated_at, parent_session_id) "
                 "VALUES (?, 'y', 'y', ?)", (child, parent))
    conn.commit()
    conn.close()


def _children(tmp_path, parent):
    conn = _chat_db(tmp_path)
    rows = conn.execute("SELECT id FROM sessions WHERE parent_session_id = ?", (parent,)).fetchall()
    conn.close()
    return sorted(r[0] for r in rows)


class _SpyForkMidTurn(_Spy):
    """模拟真机形态：这一轮跑着的时候，另一条连接的 forge 完成、给源会话写了孩子。"""

    def __init__(self, tmp_path, *a, **k):
        super().__init__(*a, **k)
        self._tmp = tmp_path

    async def __call__(self, *args, **kwargs):
        proc = await super().__call__(*args, **kwargs)
        _add_child(self._tmp, "sess-old", "sess-kid")
        return proc


# ---------------------------------------------------------------- 裁决 1：不再换窗

def test_superseded_session_skips_auto_trigger(tmp_path, monkeypatch, caplog):
    """轮中被别的连接接续了 → 本轮 result 后即使超硬顶也不再换窗。"""
    spy = _SpyForkMidTurn(tmp_path, ["sess-old", "sess-old"], cache_read=5000)
    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", spy)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=100,
                             AUTO_CARRYOVER_HARD_TOKENS=1000))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                types = [f.get("type") for f in _collect_to(wsc, "result")]
                # 证否：硬顶换窗会紧跟 result 帧发 forged；再发一个 switch_session，
                # 收到 session_switched 前这一路若无 forged，即证明没换窗
                wsc.send_json({"switch_session": "sess-kid"})
                types += [f.get("type") for f in _collect_to(wsc, "session_switched")]

    assert "forged" not in types
    assert "carryover skipped: session already superseded" in caplog.text
    assert "carryover auto-trigger (hard)" not in caplog.text
    assert _children(tmp_path, "sess-old") == ["sess-kid"]      # 没有第二个孩子


def test_manual_forge_on_superseded_session_does_not_fork(tmp_path, monkeypatch, caplog):
    """已有后继的会话手动压缩 → 回 superseded 帧（不重置），连接跟到链尾，不造新孩子。"""
    _install(monkeypatch, ["sess-old"], cache_read=10)
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                _seed_transcript(tmp_path, "sess-old")       # 精炼本可成功
                _add_child(tmp_path, "sess-old", "sess-kid")  # 但已经被接续了
                wsc.send_json({"forge": True})
                forged = _drain_to(wsc, "forged")
                # 后续消息落在链尾 sess-kid 上（--resume sess-kid）
                wsc.send_json({"text": "第二句"})
                result = _drain_to(wsc, "result")

    assert forged["superseded"] is True
    assert forged["carryover"] is False
    assert forged["session_id"] == "sess-kid"
    assert forged["source_session"] == "sess-old"
    assert result["session_id"] == "sess-kid"
    assert _children(tmp_path, "sess-old") == ["sess-kid"]
    assert "carryover skipped: session already superseded" in caplog.text


# ---------------------------------------------------------------- 裁决 2：跟到链尾

def test_switch_to_superseded_session_redirects_to_tail(tmp_path, monkeypatch, caplog):
    """switch_session 指向链中段 → 回帧带链尾 id。"""
    _seed_chain(tmp_path, ["s0", "s1", "s2"])
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                wsc.send_json({"switch_session": "s0"})
                switched = _drain_to(wsc, "session_switched")

    assert switched["session_id"] == "s2"
    assert "session redirected to chain tail: s0 -> s2" in caplog.text


def test_message_to_superseded_session_runs_on_tail(tmp_path, monkeypatch, caplog):
    """消息发到已有后继的会话 → 服务端用链尾跑本轮，result 帧带链尾 id。"""
    spy = _install(monkeypatch, ["sess-old"], cache_read=10)
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                wsc.send_json({"text": "第一句"})
                first = _drain_to(wsc, "result")
                assert first["session_id"] == "sess-old"
                _add_child(tmp_path, "sess-old", "sess-kid")   # 别的连接接续了它
                wsc.send_json({"text": "第二句"})
                second = _drain_to(wsc, "result")

    assert second["session_id"] == "sess-kid"
    assert "--resume" in spy.calls[-1]["argv"]
    assert spy.calls[-1]["argv"][spy.calls[-1]["argv"].index("--resume") + 1] == "sess-kid"
    assert "session redirected to chain tail: sess-old -> sess-kid" in caplog.text


def test_tail_redirect_survives_cycle(tmp_path, monkeypatch):
    """parent 写脏成环时跟链尾不死循环。"""
    _seed_chain(tmp_path, ["a", "b", "c"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'c' WHERE id = 'a'")  # a→c→b→a
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"switch_session": "a"})
            switched = _drain_to(wsc, "session_switched")
    assert switched["session_id"] in {"a", "b", "c"}
