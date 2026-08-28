"""自动换窗触发层（feat-carryover-auto-trigger Task 2，裁决 1/2/3/7/9）。

用假 claude 子进程驱动真实 WS 回合，断言：
1. 硬顶：单轮 total_input 过硬顶 → 本轮 result 之后立即换窗，日志见 (hard)
2. 软阈值：过软线只置 pending 不换窗；空闲到点后由节拍器投帧换窗，日志见 (soft)
3. 范围：工位会话（cwd_key 非空）同样超线也不触发
4. 开关：auto_carryover_enabled=false 时超线不触发（走 /settings，无需重启）
5. silent 轮（自动存档）不参与判定
6. forge 在途时到点的自动触发跳过本次，日志见 skipped
"""

import asyncio
import json
import logging
import time
import uuid

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
    """按调用序返回预置 session_id；cache_read 可调，用来伪造上下文占用。"""

    def __init__(self, session_ids, cache_read=0):
        self.calls = []
        self._session_ids = list(session_ids)
        self.cache_read = cache_read

    async def __call__(self, *args, **kwargs):
        argv = list(args)
        self.calls.append({"argv": argv, "cwd": kwargs.get("cwd")})
        # --resume 的一轮沿用被 resume 的会话 id（真 CLI 就是这个行为）；
        # 只有开新会话才从预置序列里取下一个
        if "--resume" in argv:
            sid = argv[argv.index("--resume") + 1]
        else:
            sid = self._session_ids.pop(0) if self._session_ids else "sess-fallback"
        return _FakeProc([
            _enc({"type": "system", "subtype": "init", "session_id": sid, "model": "m"}),
            _enc({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "好的"}],
                              "usage": {"input_tokens": 1, "output_tokens": 1}}}),
            _enc({"type": "result", "total_cost_usd": 0.0, "duration_ms": 1,
                  "usage": {"input_tokens": 1, "output_tokens": 1,
                            "cache_read_input_tokens": self.cache_read}}),
        ])


def _config(tmp_path, **overrides):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path / "cwd"),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 3600,
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
        # 节拍器调到 50ms，测试不等实时钟
        "AUTO_CARRYOVER_TICK_SECONDS": 0.05,
    }
    cfg.update(overrides)
    return cfg


def _seed_transcript(tmp_path, session_id, cwd=None, turns=6):
    cwd = cwd or str(tmp_path / "cwd")
    path = carryover.transcript_path(tmp_path / "projects", cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(turns):
        base = {"sessionId": session_id, "timestamp": "2026-08-28T10:00:00.000Z",
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
    for _ in range(limit):
        frame = wsc.receive_json()
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"没有收到 {wanted} 帧")


def _collect_to(wsc, wanted, limit=60):
    """收帧直到 wanted，返回沿途所有帧。
    断言「不该出现 forged」只能这么做——WS 上没有第二帧可等时 receive 会一直阻塞，
    所以用「再走一轮，看这一路上有没有混进 forged」来证否。"""
    frames = []
    for _ in range(limit):
        frame = wsc.receive_json()
        frames.append(frame)
        if frame.get("type") == wanted:
            return frames
    raise AssertionError(f"没有收到 {wanted} 帧")


def _assert_no_forge_next_turn(wsc, text="再说一句"):
    """再跑一轮，断言这一路（含上一轮遗留帧）没有 forged。"""
    wsc.send_json({"text": text})
    types = [f.get("type") for f in _collect_to(wsc, "result")]
    assert "forged" not in types, f"不该触发换窗，实收帧序：{types}"


def _install(monkeypatch, session_ids, cache_read=0):
    s = _Spy(session_ids, cache_read=cache_read)
    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", s)
    return s


# ---------------------------------------------------------------- 硬顶

def test_hard_threshold_forges_immediately(tmp_path, monkeypatch, caplog):
    """单轮就超硬顶 → result 之后立即换窗，日志见 (hard)。"""
    _install(monkeypatch, ["sess-old"], cache_read=5000)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=100,
                             AUTO_CARRYOVER_HARD_TOKENS=1000))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                forged = _drain_to(wsc, "forged")

    assert forged["auto"] is True
    assert forged["carryover"] is True
    assert forged["source_session"] == "sess-old"
    assert "carryover auto-trigger (hard)" in caplog.text


def test_below_thresholds_no_trigger(tmp_path, monkeypatch, caplog):
    _install(monkeypatch, ["sess-old", "sess-old"], cache_read=10)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=100_000,
                             AUTO_CARRYOVER_HARD_TOKENS=160_000))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                _assert_no_forge_next_turn(wsc)

    assert "carryover auto-trigger" not in caplog.text


# ---------------------------------------------------------------- 软阈值 + 空闲

def test_soft_threshold_waits_for_idle_then_forges(tmp_path, monkeypatch, caplog):
    """过软线不当场换窗；空闲到点后节拍器投帧，换窗照样是无缝 carryover。"""
    _install(monkeypatch, ["sess-old"], cache_read=5000)
    app = create_app(_config(
        tmp_path,
        AUTO_CARRYOVER_SOFT_TOKENS=1000,
        AUTO_CARRYOVER_HARD_TOKENS=1_000_000,   # 硬顶够高，确保走软档
        AUTO_CARRYOVER_IDLE_MINUTES=0.002,      # 0.12 秒即算空闲
    ))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                forged = _drain_to(wsc, "forged")     # 由空闲节拍器触发

    assert forged["auto"] is True
    assert forged["carryover"] is True
    assert "carryover auto-trigger (soft)" in caplog.text
    assert "carryover auto-trigger (hard)" not in caplog.text


def test_soft_pending_not_fired_while_busy(tmp_path, monkeypatch, caplog):
    """软线过了但用户还在说话（空闲计时被顶新）→ 只置 pending，不换窗。"""
    _install(monkeypatch, ["sess-old", "sess-old", "sess-old"], cache_read=5000)
    app = create_app(_config(
        tmp_path,
        AUTO_CARRYOVER_SOFT_TOKENS=1000,
        AUTO_CARRYOVER_HARD_TOKENS=1_000_000,
        AUTO_CARRYOVER_IDLE_MINUTES=60,          # 一小时才算空闲，本测试内绝不到点
    ))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                _assert_no_forge_next_turn(wsc, "第二句")

    assert "pending until idle" in caplog.text      # 标记置了
    assert "idle elapsed" not in caplog.text        # 但没到点


# ---------------------------------------------------------------- 范围与开关

def test_workstation_session_never_triggers(tmp_path, monkeypatch, caplog):
    """工位会话（cwd_key 非空）超线也不触发（裁决 3）。"""
    work = tmp_path / "work"
    work.mkdir()
    _install(monkeypatch, ["sess-work", "sess-work"], cache_read=5000)
    app = create_app(_config(
        tmp_path,
        WORKSPACES={"proj": {"label": "项目", "path": str(work)}},
        AUTO_CARRYOVER_SOFT_TOKENS=100,
        AUTO_CARRYOVER_HARD_TOKENS=1000,
    ))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-work", cwd=str(work))
                wsc.send_json({"text": "第一句", "cwd_key": "proj"})
                _drain_to(wsc, "result")
                _assert_no_forge_next_turn(wsc)

    assert "carryover auto-trigger" not in caplog.text


def test_disabled_via_settings_endpoint(tmp_path, monkeypatch, caplog):
    """设置页关掉开关 → 超线不触发，且无需重启（同一 app 实例内即时生效）。"""
    _install(monkeypatch, ["sess-old", "sess-old"], cache_read=5000)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=100,
                             AUTO_CARRYOVER_HARD_TOKENS=1000))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            client.post("/settings", json={"auto_carryover_enabled": False})
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                _assert_no_forge_next_turn(wsc)

    assert "carryover auto-trigger" not in caplog.text


def test_threshold_change_takes_effect_next_turn(tmp_path, monkeypatch):
    """设置页改软阈值 → 下一轮判定就用新值（完成标准 5）。"""
    _install(monkeypatch, ["sess-old", "sess-old"], cache_read=5000)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=1_000_000,
                             AUTO_CARRYOVER_HARD_TOKENS=1_000_000))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            _seed_transcript(tmp_path, "sess-old")
            wsc.send_json({"text": "第一句"})
            types = [f.get("type") for f in _collect_to(wsc, "result")]
            assert "forged" not in types            # 阈值高，第一轮不该触发
            # 把硬顶调到本轮 total_input 之下，下一轮即触发
            client.post("/settings", json={"auto_carryover_hard_tokens": 1000})
            wsc.send_json({"text": "第二句"})
            _drain_to(wsc, "result")
            forged = _drain_to(wsc, "forged")
    assert forged["auto"] is True


# ---------------------------------------------------------------- silent 轮

def test_silent_archive_turn_not_counted(tmp_path, monkeypatch, caplog):
    """静默存档轮的 usage 不参与判定（裁决「静默存档轮不计数」）。
    手动 forge 会跑一次 force 存档（silent 轮），其 total_input 同样超线，
    但不得因此产生第二次自动触发。"""
    _install(monkeypatch, ["sess-old", "sess-arch"], cache_read=5000)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=1_000_000,
                             AUTO_CARRYOVER_HARD_TOKENS=1_000_000))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                # 阈值收紧后手动 forge：存档轮是 silent 的，不该被算进触发判定
                client.post("/settings", json={"auto_carryover_hard_tokens": 1000})
                wsc.send_json({"forge": True})
                forged = _drain_to(wsc, "forged")

    assert forged["auto"] is False          # 这次换窗来自用户手动，不是自动触发
    assert "carryover auto-trigger (hard)" not in caplog.text


# ---------------------------------------------------------------- 在途闸

def test_auto_trigger_skipped_while_forge_in_flight(tmp_path, monkeypatch, caplog):
    """forge 在途时到点的自动触发跳过本次，等下一轮 result 重新判（裁决 7）。
    真机形态：一条连接正在精炼，另一条连接（重连/多开）同会话跑完一轮且超线。"""
    _install(monkeypatch, ["sess-shared", "sess-shared"], cache_read=5000)
    app = create_app(_config(tmp_path, AUTO_CARRYOVER_SOFT_TOKENS=1_000_000,
                             AUTO_CARRYOVER_HARD_TOKENS=1_000_000))

    real = carryover.refine_detailed

    def slow(*a, **k):
        time.sleep(0.8)          # 跑在 asyncio.to_thread 里，不挡事件循环
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
                    # 阈值收紧：w2 这一轮必超硬顶
                    client.post("/settings", json={"auto_carryover_hard_tokens": 1000})

                    w1.send_json({"forge": True})       # 进入精炼在途（0.8s）
                    time.sleep(0.2)
                    w2.send_json({"text": "在途期间的一轮"})
                    types = [f.get("type") for f in _collect_to(w2, "result")]
                    _drain_to(w1, "forged")

    assert "forged" not in types                 # 撞闸的那条没换窗
    assert "carryover auto-trigger skipped: forge in flight" in caplog.text


def test_manual_forge_clears_pending(tmp_path, monkeypatch, caplog):
    """pending 期间用户手动压缩 → pending 清除，不会稍后再自动来一次（裁决 7）。"""
    _install(monkeypatch, ["sess-old", "sess-x"], cache_read=5000)
    app = create_app(_config(
        tmp_path,
        AUTO_CARRYOVER_SOFT_TOKENS=1000,
        AUTO_CARRYOVER_HARD_TOKENS=1_000_000,
        AUTO_CARRYOVER_IDLE_MINUTES=0.002,
    ))
    with caplog.at_level(logging.INFO, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                _seed_transcript(tmp_path, "sess-old")
                wsc.send_json({"text": "第一句"})
                _drain_to(wsc, "result")
                wsc.send_json({"forge": True})       # 抢在空闲到点前手动换窗
                first = _drain_to(wsc, "forged")
                assert first["auto"] is False
                time.sleep(0.4)                      # 让节拍器空转几拍

    assert "pending until idle" in caplog.text
    # 手动换窗把 pending 划掉了，节拍器不该再对源会话投帧
    assert "idle elapsed" not in caplog.text


# ---------------------------------------------------------------- 干净重开（Task 4）

def test_clean_reopen_archives_but_skips_carryover(tmp_path, monkeypatch):
    """「开新对话」= 干净重开（裁决 4）：照样存档，但不 carryover——
    即使源 transcript 完好可精炼，也必须回纯重置的帧。"""
    _install(monkeypatch, ["sess-old", "sess-fresh"], cache_read=10)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "第一句"})
            _drain_to(wsc, "result")
            _seed_transcript(tmp_path, "sess-old")      # 精炼本可成功
            wsc.send_json({"forge": True, "clean": True})
            forged = _drain_to(wsc, "forged")

    assert forged["clean"] is True
    assert forged["auto"] is False
    assert forged["carryover"] is False
    assert forged["session_id"] is None
    assert forged["message"] == "已存档，新对话已开始"
    # 没生成接续 JSONL：目录里只剩源文件
    d = tmp_path / "projects" / carryover.encode_project_dir(str(tmp_path / "cwd"))
    assert sorted(p.name for p in d.iterdir()) == ["sess-old.jsonl"]


def test_manual_forge_frame_flags(tmp_path, monkeypatch):
    """手动「压缩上下文」：auto/clean 都是 false，carryover 成功即无缝。"""
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

    assert forged["auto"] is False and forged["clean"] is False
    assert forged["carryover"] is True
