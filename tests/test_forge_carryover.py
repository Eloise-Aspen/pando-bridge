"""forge 精炼续窗接线（feat-forge-carryover Task 2/3/4）。

用假 claude 子进程驱动真实 WS 回合，断言：
1. forge 成功 → forged 帧带 carryover=true + 新 session_id，新 JSONL 落在源目录
2. forge 后下一轮 CLI 参数是 `--resume <新 id>`，且沿用原 cwd
3. 源 transcript 缺失/损坏 → 降级为纯重置，forged 帧 carryover=false、session_id=null
4. 边界声明只注入换窗后的第一条消息，第二条不含（裁决 5）
5. CARRYOVER_TAIL_TURNS / CARRYOVER_MAX_CHARS / CARRYOVER_ENABLED 经 config 透传生效
"""

import asyncio
import json
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import carryover, create_app


# ---------------------------------------------------------------- 假子进程

class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i < len(self._lines):
            line = self._lines[self._i]
            self._i += 1
            await asyncio.sleep(0)
            return line
        raise StopAsyncIteration


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.returncode = 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return 0


def _enc(obj):
    return json.dumps(obj, ensure_ascii=False).encode()


class _Spy:
    """记录每次 spawn 的 argv 与 cwd，并按调用序返回预置的 session_id。"""

    def __init__(self, session_ids):
        self.calls = []
        self._session_ids = list(session_ids)

    async def __call__(self, *args, **kwargs):
        self.calls.append({"argv": list(args), "cwd": kwargs.get("cwd")})
        sid = self._session_ids.pop(0) if self._session_ids else "sess-fallback"
        return _FakeProc([
            _enc({"type": "system", "subtype": "init", "session_id": sid, "model": "m"}),
            _enc({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "好的"}],
                              "usage": {"input_tokens": 1, "output_tokens": 1}}}),
            _enc({"type": "result", "total_cost_usd": 0.0, "duration_ms": 1,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ])

    @property
    def messages(self):
        """每次 spawn 的最后一个位置参数即传给 CLI 的消息正文。"""
        return [c["argv"][-1] for c in self.calls]


# ---------------------------------------------------------------- 装置

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


def _seed_transcript(tmp_path, session_id, turns=6):
    """在 CLAUDE_PROJECTS_DIR 下按 cwd 编码建一份可精炼的源 transcript。"""
    cwd = str(tmp_path / "cwd")
    path = carryover.transcript_path(tmp_path / "projects", cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(turns):
        base = {"sessionId": session_id, "timestamp": "2026-08-20T10:00:00.000Z",
                "cwd": cwd, "version": "2.1.200", "parentUuid": None}
        frames.append({**base, "type": "user", "uuid": str(uuid.uuid4()),
                       "message": {"role": "user", "content": f"用户第 {i} 问"}})
        frames.append({**base, "type": "assistant", "uuid": str(uuid.uuid4()),
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": f"助手第 {i} 答"}]}})
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames),
                    encoding="utf-8")
    return path


def _drain_to(wsc, wanted, limit=60):
    """收帧直到拿到指定 type 的帧。"""
    for _ in range(limit):
        frame = wsc.receive_json()
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"没有收到 {wanted} 帧")


@pytest.fixture
def spy(monkeypatch):
    def _install(session_ids):
        s = _Spy(session_ids)
        monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", s)
        return s
    return _install


# ---------------------------------------------------------------- Task 2

def test_forge_carries_over(tmp_path, spy):
    s = spy(["sess-old"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()                      # hello
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")

            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    assert forged["carryover"] is True
    assert forged["message"] == "已换窗，上下文已接续"
    new_id = forged["session_id"]
    uuid.UUID(new_id)

    # 新 JSONL 与源文件同目录，源文件仍在
    proj = carryover.transcript_path(tmp_path / "projects", str(tmp_path / "cwd"), new_id)
    assert proj.is_file()
    assert carryover.transcript_path(
        tmp_path / "projects", str(tmp_path / "cwd"), "sess-old").is_file()
    lines = [json.loads(l) for l in proj.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines and all(f["sessionId"] == new_id for f in lines)


def test_next_turn_resumes_new_session(tmp_path, spy):
    s = spy(["sess-old", "ignored"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            wsc.send_json({"text": "换窗后第一句"})
            _drain_to(wsc, "result")

    new_id = forged["session_id"]
    argv = s.calls[-1]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == new_id
    # 工作目录必须与源会话一致，否则 CLI 找不到这份 transcript
    assert s.calls[-1]["cwd"] == str(tmp_path / "cwd")


def test_missing_transcript_degrades(tmp_path, spy):
    """源 transcript 不存在（被移走/损坏）→ 降级为现行纯重置，服务不报错。"""
    spy(["sess-old"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            # 刻意不建 transcript
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    assert forged["carryover"] is False
    assert forged["session_id"] is None
    assert forged["message"] == "已存档，新对话已开始"


def test_corrupt_transcript_degrades(tmp_path, spy):
    spy(["sess-old"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            path = _seed_transcript(tmp_path, "sess-old")
            path.write_text("这不是 jsonl\n{坏行\n", encoding="utf-8")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    assert forged["carryover"] is False
    assert forged["session_id"] is None


def test_forge_without_session_degrades(tmp_path, spy):
    """还没开始对话就 forge：无源会话，走降级路径且不炸。"""
    spy([])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
    assert forged["carryover"] is False


# ---------------------------------------------------------------- Task 3

def test_boundary_notice_injected_once(tmp_path, spy):
    s = spy(["sess-old", "a", "b"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            _drain_to(wsc, "forged")
            wsc.send_json({"text": "换窗后第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"text": "换窗后第二句"})
            _drain_to(wsc, "result")

    first, second = s.messages[-2], s.messages[-1]
    assert "刚完成换窗" in first
    assert "不确定就说不知道" in first
    assert "换窗后第一句" in first
    assert "刚完成换窗" not in second
    assert "换窗后第二句" in second
    # 装配顺序：边界声明在时间注入/正文之前（裁决 5 的 L0 修正案）
    assert first.index("[系统提示") < first.index("[当前时间")


def test_no_notice_when_degraded(tmp_path, spy):
    s = spy(["sess-old", "sess-new"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"forge": True})          # 无 transcript → 降级
            _drain_to(wsc, "forged")
            wsc.send_json({"text": "降级后第一句"})
            _drain_to(wsc, "result")

    assert "刚完成换窗" not in s.messages[-1]


# ---------------------------------------------------------------- Task 4

def test_tail_turns_config_respected(tmp_path, spy):
    """CARRYOVER_TAIL_TURNS=2 时新会话只带首回合 + 最近 2 回合。"""
    s = spy(["sess-old", "sess-new"])
    app = create_app(_config(tmp_path, CARRYOVER_TAIL_TURNS=2))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old", turns=8)
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            wsc.send_json({"text": "换窗后第一句"})
            _drain_to(wsc, "result")

    proj = carryover.transcript_path(
        tmp_path / "projects", str(tmp_path / "cwd"), forged["session_id"])
    dumped = proj.read_text(encoding="utf-8")
    assert "用户第 0 问" in dumped                    # 首回合
    assert "用户第 6 问" in dumped and "用户第 7 问" in dumped
    assert "用户第 3 问" not in dumped
    # 附注里的轮数跟着 config 走，不硬编码
    assert "最近约 2 轮" in s.messages[-1]


def test_max_chars_config_respected(tmp_path, spy):
    """字符预算收紧后中段回合被裁，首回合与最近回合仍在。"""
    spy(["sess-old"])
    app = create_app(_config(tmp_path, CARRYOVER_MAX_CHARS=60))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old", turns=8)
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    proj = carryover.transcript_path(
        tmp_path / "projects", str(tmp_path / "cwd"), forged["session_id"])
    dumped = proj.read_text(encoding="utf-8")
    assert "用户第 0 问" in dumped
    assert "用户第 7 问" in dumped
    assert "用户第 4 问" not in dumped


# ------------------------------------------- L0 重注入（2026-08-20 真机修正案）

_LAYER_PLUGIN = "tests.fixtures.hook_plugins.LayerInjectPlugin"


def test_carryover_reinjects_l0(tmp_path, spy):
    """接续会话第一条消息必须带 L0——transcript 里没有 --system-prompt 的内容，
    不重建就会走插件冷启动仪式（真机 bug）。"""
    s = spy(["sess-old", "sess-new"])
    app = create_app(_config(tmp_path, PLUGINS=[_LAYER_PLUGIN]))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            assert forged["carryover"] is True
            wsc.send_json({"text": "换窗后第一句"})
            recall = _drain_to(wsc, "memory_recall")   # L0 注入证据帧
            _drain_to(wsc, "result")

    assert "context injected" in recall["context"]
    first = s.messages[-1]
    from tests.fixtures.hook_plugins import LayerInjectPlugin as P
    assert P.L0 in first
    # 装配顺序：身份层 → 边界声明 → 时间/正文
    assert first.index(P.L0) < first.index("[系统提示") < first.index("[当前时间")
    assert "换窗后第一句" in first
    # --resume 与 --system-prompt 互斥：绝不能同时出现
    argv = s.calls[-1]["argv"]
    assert "--resume" in argv and "--system-prompt" not in argv


def test_carryover_l0_injected_only_once(tmp_path, spy):
    s = spy(["sess-old", "a", "b"])
    app = create_app(_config(tmp_path, PLUGINS=[_LAYER_PLUGIN]))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            _drain_to(wsc, "forged")
            wsc.send_json({"text": "换窗后第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"text": "换窗后第二句"})
            _drain_to(wsc, "result")

    from tests.fixtures.hook_plugins import LayerInjectPlugin as P
    assert P.L0 in s.messages[-2]
    assert P.L0 not in s.messages[-1]      # 第二条只剩 L2 召回，不再重建身份层


def test_degraded_path_keeps_system_prompt(tmp_path, spy):
    """降级（纯重置）路径不受修正案影响：仍走 --system-prompt 冷启动。"""
    s = spy(["sess-old", "sess-fresh"])
    app = create_app(_config(tmp_path, PLUGINS=[_LAYER_PLUGIN]))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"forge": True})      # 无 transcript → 降级
            _drain_to(wsc, "forged")
            wsc.send_json({"text": "降级后第一句"})
            _drain_to(wsc, "result")

    argv = s.calls[-1]["argv"]
    assert "--system-prompt" in argv and "--resume" not in argv
    from tests.fixtures.hook_plugins import LayerInjectPlugin as P
    assert P.L0 not in s.messages[-1]           # 身份层在 CLI 参数里，不在消息正文里


# ------------------------------------------- forge 在途防重入（Fix 3）

def _count_transcripts(tmp_path):
    d = (tmp_path / "projects" / carryover.encode_project_dir(str(tmp_path / "cwd")))
    return sorted(p.name for p in d.iterdir() if p.suffix == ".jsonl")


def test_double_forge_same_connection_ignored(tmp_path, spy):
    """同一连接连发两帧 forge：只精炼一次，只多出一份新 JSONL。"""
    spy(["sess-old"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            wsc.send_json({"forge": True})
            first = _drain_to(wsc, "forged")

    files = _count_transcripts(tmp_path)
    assert "sess-old.jsonl" in files
    assert len(files) == 2, f"应只多出一份新 JSONL，实际 {files}"
    assert first["session_id"] + ".jsonl" in files


def test_concurrent_forge_across_connections_gated(tmp_path, spy, monkeypatch, caplog):
    """两条 WS 连接对**同一源会话**并发 forge——真机孤儿事故的原始形态
    （11:52:52 两次 carryover 源同为 4a8d6586，前者当场变孤儿）。
    把精炼拖慢制造真实的在途窗口，第二条必须被在途闸挡下。"""
    spy(["sess-shared"])
    app = create_app(_config(tmp_path))

    real = carryover.refine_detailed

    def slow(*a, **k):
        time.sleep(0.6)          # 跑在 asyncio.to_thread 里，不挡事件循环
        return real(*a, **k)

    monkeypatch.setattr(carryover, "refine_detailed", slow)

    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as w1:
                w1.receive_json()
                w1.send_json({"text": "第一句"})
                _drain_to(w1, "result")
                _seed_transcript(tmp_path, "sess-shared")

                with client.websocket_connect("/ws") as w2:
                    w2.receive_json()
                    w2.send_json({"switch_session": "sess-shared"})
                    _drain_to(w2, "session_switched")

                    w1.send_json({"forge": True})
                    time.sleep(0.25)              # 让 w1 进入精炼在途
                    w2.send_json({"forge": True})  # 撞闸，被忽略且不回帧
                    forged = _drain_to(w1, "forged")

    assert forged["carryover"] is True
    assert "forge already in flight, ignored" in caplog.text
    files = _count_transcripts(tmp_path)
    assert files == sorted(["sess-shared.jsonl", forged["session_id"] + ".jsonl"]), \
        f"并发 forge 只该产出一份新 JSONL，实际 {files}"


def test_forge_gate_released_after_completion(tmp_path, spy):
    """闸必须在 finally 里放开：同一会话第二次 forge（非并发）仍要能跑。"""
    spy(["sess-a", "sess-b"])
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-a")
            wsc.send_json({"forge": True})
            first = _drain_to(wsc, "forged")
            assert first["carryover"] is True

            # 回到原会话再 forge 一次：闸已放开，应当照常工作
            wsc.send_json({"switch_session": "sess-a"})
            _drain_to(wsc, "session_switched")
            wsc.send_json({"forge": True})
            second = _drain_to(wsc, "forged")

    assert second["carryover"] is True
    assert second["session_id"] != first["session_id"]


def test_forge_gate_released_on_error(tmp_path, spy, monkeypatch):
    """精炼内部抛异常时闸也要放开，否则该会话此后永远 forge 不了。"""
    spy(["sess-old"])
    app = create_app(_config(tmp_path))

    boom = {"n": 0}
    real = carryover.refine_detailed

    def flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(carryover, "refine_detailed", flaky)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            first = _drain_to(wsc, "forged")
            assert first["carryover"] is False      # 异常 → 降级

            wsc.send_json({"switch_session": "sess-old"})
            _drain_to(wsc, "session_switched")
            wsc.send_json({"forge": True})
            second = _drain_to(wsc, "forged")

    assert second["carryover"] is True              # 闸已放开，第二次正常精炼


def test_carryover_can_be_disabled(tmp_path, spy):
    """CARRYOVER_ENABLED=False → forge 行为回到本功能上线前。"""
    spy(["sess-old"])
    app = create_app(_config(tmp_path, CARRYOVER_ENABLED=False))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")

    assert forged["carryover"] is False
    assert forged["session_id"] is None
