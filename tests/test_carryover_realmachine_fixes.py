"""真机复验批次（2026-08-30）的三个修正项。

用户真机验收七站过六站半，抓出三项：

- Fix A：压缩分隔线要**驻留**在压缩点，刷新后仍在 → 服务端随历史下发边界标记
  `carryover_seam_before` / `carryover_seam_after`（读侧注入，不落库）
- Fix B：会话列表沿 parent 链折叠，链上只列最新一段，旧段不单独成行
- Fix C：上下文用量弹窗「输入/输出」恒为个位数 → 输入改 total_input 口径，
  输出改用 result 帧的真实 output_tokens
"""

import json

from fastapi.testclient import TestClient

from pando import create_app

from tests.test_carryover_auto_trigger import (  # 复用假子进程与装置
    _FakeProc, _config, _drain_to, _enc, _install,
)
from tests.test_carryover_parent_chain import _chat_db, _seed_chain


# ---------------------------------------------------------------- Fix A 边界标记

def test_seam_marked_at_chain_boundary(tmp_path, monkeypatch):
    """三段链各有一条消息 → 后两段的首条消息各带 seam_before，第一条不带。"""
    _seed_chain(tmp_path, ["s0", "s1", "s2"])
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        history = client.get("/sessions/s2/messages").json()

    assert [m["session_id"] for m in history] == ["s0", "s1", "s2"]
    marks = [bool(m["metadata"].get("carryover_seam_before")) for m in history]
    assert marks == [False, True, True]
    # 尾段自己有消息 → 不需要末尾那条线
    assert not any(m["metadata"].get("carryover_seam_after") for m in history)


def test_seam_after_when_tail_segment_empty(tmp_path, monkeypatch):
    """刚换完窗、新会话一条消息都没有 → 分隔线落在整段历史末尾（刷新后仍在）。"""
    _seed_chain(tmp_path, ["s0"])
    conn = _chat_db(tmp_path)
    conn.execute("INSERT INTO sessions (id, created_at, updated_at, parent_session_id) "
                 "VALUES ('s1', 'x', 'x', 's0')")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        history = client.get("/sessions/s1/messages").json()

    assert [m["session_id"] for m in history] == ["s0"]
    assert history[-1]["metadata"]["carryover_seam_after"] is True


def test_seam_marks_not_persisted(tmp_path, monkeypatch):
    """标记只在读侧注入：库里的 metadata 不该被写脏。"""
    _seed_chain(tmp_path, ["s0", "s1"])
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        client.get("/sessions/s1/messages")

    conn = _chat_db(tmp_path)
    stored = [json.loads(r[0] or "{}") for r in
              conn.execute("SELECT metadata FROM messages").fetchall()]
    conn.close()
    assert all("carryover_seam_before" not in m and "carryover_seam_after" not in m
               for m in stored)


def test_single_session_has_no_seam(tmp_path, monkeypatch):
    """没换过窗的普通会话:一条线都不该有。"""
    _seed_chain(tmp_path, ["solo"])
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        history = client.get("/sessions/solo/messages").json()
    assert history and not any(
        m["metadata"].get("carryover_seam_before") or m["metadata"].get("carryover_seam_after")
        for m in history)


# ---------------------------------------------------------------- Fix B 列表折叠

def test_session_list_folds_chain(tmp_path, monkeypatch):
    """三段链只在列表里占一行，且那行是链尾（最新那段）。"""
    _seed_chain(tmp_path, ["s0", "s1", "s2"])
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        rows = client.get("/sessions").json()

    assert [r["id"] for r in rows] == ["s2"]
    # 标题按整条链取最早那句，条数按整条链算——否则刚换完窗会冒出一行空条目
    assert rows[0]["title"] == "消息 s0"
    assert rows[0]["msg_count"] == 3


def test_folded_head_with_no_own_messages(tmp_path, monkeypatch):
    """链尾还没有自己的消息（刚换完窗）：仍显示成一行，标题取链上最早那句。"""
    _seed_chain(tmp_path, ["s0"])
    conn = _chat_db(tmp_path)
    conn.execute("INSERT INTO sessions (id, created_at, updated_at, parent_session_id) "
                 "VALUES ('s1', 'x', 'y', 's0')")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        rows = client.get("/sessions").json()

    assert [r["id"] for r in rows] == ["s1"]
    assert rows[0]["title"] == "消息 s0"
    assert rows[0]["msg_count"] == 1


def test_clean_restart_stays_separate(tmp_path, monkeypatch):
    """「开新对话」是干净重开、不写 parent → 天然自成一条，不被折叠掉。"""
    _seed_chain(tmp_path, ["s0", "s1"])
    conn = _chat_db(tmp_path)
    conn.execute("INSERT INTO sessions (id, created_at, updated_at, parent_session_id) "
                 "VALUES ('fresh', 'x', 'z', '')")
    conn.execute("INSERT INTO messages (session_id, role, content, metadata, created_at) "
                 "VALUES ('fresh', 'user', '全新的话', '{}', 'x')")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path, CARRYOVER_CHAIN_MAX_DEPTH=10))
    with TestClient(app) as client:
        rows = client.get("/sessions").json()

    assert sorted(r["id"] for r in rows) == ["fresh", "s1"]


def test_folding_survives_dirty_self_parent(tmp_path, monkeypatch):
    """parent 指回自己（外部写脏）不能让会话把自己折叠没了。"""
    _seed_chain(tmp_path, ["solo"])
    conn = _chat_db(tmp_path)
    conn.execute("UPDATE sessions SET parent_session_id = 'solo' WHERE id = 'solo'")
    conn.commit()
    conn.close()
    _install(monkeypatch, [])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        rows = client.get("/sessions").json()
    assert [r["id"] for r in rows] == ["solo"]


# ---------------------------------------------------------------- Fix C 用量口径

class _UsageSpy:
    """伪造真实缓存命中形态：assistant 事件的 usage 是流式起始快照
    （input_tokens=2 / output_tokens=2），result 帧才是本轮权威账单。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        return _FakeProc([
            _enc({"type": "system", "subtype": "init", "session_id": "sess-u", "model": "m"}),
            _enc({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "好的"}],
                              "usage": {"input_tokens": 2, "output_tokens": 2,
                                        "cache_read_input_tokens": 48128,
                                        "cache_creation_input_tokens": 280}}}),
            _enc({"type": "result", "total_cost_usd": 0.0, "duration_ms": 1,
                  "modelUsage": {"m": {"contextWindow": 200000}},
                  "usage": {"input_tokens": 2, "output_tokens": 396,
                            "cache_read_input_tokens": 48128,
                            "cache_creation_input_tokens": 280}}),
        ])


def test_context_usage_counts_cache_and_real_output(tmp_path, monkeypatch):
    """弹窗四项：输入=总输入（含缓存读写），输出=result 帧真实输出，used=两者之和。"""
    import pando.server as server_mod
    spy = _UsageSpy()
    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", spy)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "一句话"})
            result = _drain_to(wsc, "result")

    ctx = result["context"]
    # 修前这两项是 2 和 2（缓存命中时 input_tokens 本就极小；
    # assistant 事件的 output_tokens 是流式起始快照，与本轮输出无关）
    assert ctx["input"] == 2 + 48128 + 280
    assert ctx["output"] == 396
    assert ctx["used"] == ctx["input"] + ctx["output"]
    assert ctx["window"] == 200000
    # 账单口径（usage）不受影响，仍是「这轮花了多少」
    assert result["usage"]["output_tokens"] == 396
    assert result["usage"]["total_input"] == 2 + 48128 + 280
