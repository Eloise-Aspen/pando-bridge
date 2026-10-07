"""chat 换窗后人格提示不丢（fix-carryover-persona-prompt）。

背景：CLI 把会话首次请求的系统提示存成 transcript 里的 prompt_snapshot，之后 --resume
一律沿用它、忽略新传的 --system-prompt。旧的 chat 换窗拼 JSONL 再 resume，拼出来的
文件没有快照，整窗跑在 CLI 默认提示上。

这里的假 CLI 尽量照真 CLI 的行为来：
- `--session-id X`：X 的 transcript 已存在 → 报「already in use」退出 1（真 CLI 实测如此）；
  否则新建 transcript，写一条 prompt_snapshot（内容 = 本次 --system-prompt-file 的文件内容
  或 --system-prompt，都没传就是默认提示）
- `--resume X`：transcript 不存在 → 「No conversation found」退出 1；存在则只追加对话帧，
  快照不变
- 不带 id：开新会话，规则同 --session-id

覆盖完成标准：首轮命令行、首轮失败保留登记、重启后首轮仍对、非 chat 不变、报警。
"""

import asyncio
import json
import logging
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import carryover, create_app
from tests.fixtures.hook_plugins import LayerInjectPlugin as L

_LAYER_PLUGIN = "tests.fixtures.hook_plugins.LayerInjectPlugin"
# 客厅提示：报警按它的前 40 个字符核对，故写得长一点
HINT = "\n\n【客厅模式】这是陪伴聊天，不是写代码。语气自然、像朋友一样说话，别列清单、别像客服。"
DEFAULT_PROMPT = "You are an agent working with the user toward their goals."


# ---------------------------------------------------------------- 假 CLI

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
    def __init__(self, data=b""):
        self._data = data

    async def read(self):
        return self._data


class _FakeProc:
    def __init__(self, lines, returncode=0, stderr=b""):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _enc(obj):
    return json.dumps(obj, ensure_ascii=False).encode()


def _arg(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


class FakeCLI:
    """记录每次 spawn，并像真 CLI 一样读写 transcript（含 prompt_snapshot）。"""

    def __init__(self, projects_dir, write_snapshot=True, snapshot_override=None):
        self.projects_dir = projects_dir
        self.calls = []
        self.write_snapshot = write_snapshot
        self.snapshot_override = snapshot_override
        self.fail_next = 0           # >0 时本次调用模拟 CLI 失败（不建文件、退出 1）

    def _path(self, cwd, sid):
        return carryover.transcript_path(self.projects_dir, cwd, sid)

    def _append(self, path, sid, message, reply):
        frames = [
            {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
             "message": {"role": "user", "content": message}},
            {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "内心戏不该带过去"},
                 {"type": "text", "text": reply}]}},
        ]
        with path.open("a", encoding="utf-8") as fh:
            for f in frames:
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")

    async def __call__(self, *args, **kwargs):
        argv = list(args)
        cwd = kwargs.get("cwd")
        # 首轮提示走文件（补裁 8）：spawn 当下读出内容并记下路径，事后断言文件已删
        prompt_path = _arg(argv, "--system-prompt-file")
        file_prompt = None
        if prompt_path:
            with open(prompt_path, encoding="utf-8") as fh:
                file_prompt = fh.read()
        self.calls.append({"argv": argv, "cwd": cwd,
                           "prompt": file_prompt, "prompt_path": prompt_path})
        message = argv[-1]
        if self.fail_next:
            self.fail_next -= 1
            return _FakeProc([], returncode=1, stderr=b"API Error: overloaded")
        resume = _arg(argv, "--resume")
        if resume:
            sid = resume
            path = self._path(cwd, sid)
            if not path.exists():
                return _FakeProc([], returncode=1,
                                 stderr=b"No conversation found with session ID")
        else:
            sid = _arg(argv, "--session-id") or str(uuid.uuid4())
            path = self._path(cwd, sid)
            if path.exists():
                return _FakeProc([], returncode=1, stderr=(
                    f"Error: Session ID {sid} is already in use.").encode())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            if self.write_snapshot:
                prompt = (self.snapshot_override or file_prompt
                          or _arg(argv, "--system-prompt") or DEFAULT_PROMPT)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "attachment", "attachment": {
                        "type": "prompt_snapshot", "systemPrompt": [prompt, "环境段"]}},
                        ensure_ascii=False) + "\n")
        self._append(path, sid, message, f"回复#{len(self.calls)}")
        return _FakeProc([
            _enc({"type": "system", "subtype": "init", "session_id": sid, "model": "m"}),
            _enc({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "好的"}],
                              "usage": {"input_tokens": 1, "output_tokens": 1}}}),
            _enc({"type": "result", "total_cost_usd": 0.0, "duration_ms": 1,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ])

    def prompt(self, i=-1):
        return self.calls[i]["prompt"]

    def prompt_files(self):
        return [c["prompt_path"] for c in self.calls if c["prompt_path"]]

    def argv(self, i=-1):
        return self.calls[i]["argv"]


# ---------------------------------------------------------------- 装置

def _config(tmp_path, **overrides):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path / "cwd"),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [_LAYER_PLUGIN],
        "ARCHIVE_INTERVAL": 3600,
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
        "CHAT_MODE_HINT": HINT,
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def _install(**kw):
        fake = FakeCLI(tmp_path / "projects", **kw)
        monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake)
        return fake
    return _install


def _drain_to(wsc, wanted, limit=60):
    for _ in range(limit):
        frame = wsc.receive_json()
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"没有收到 {wanted} 帧")


def _registry(tmp_path):
    with sqlite3.connect(tmp_path / "data" / "chat.db") as conn:
        return dict(conn.execute("SELECT session_id, tail_text FROM carryover_first_turn"))


def _transcripts(tmp_path):
    return sorted(p.name for p in (tmp_path / "projects").rglob("*.jsonl"))


def _chat_then_forge(wsc, turns=3):
    """在客厅开一个会话聊几轮，再手动换窗；返回 (旧 id, forged 帧)。"""
    wsc.receive_json()                                      # hello
    old = None
    for i in range(turns):
        wsc.send_json({"text": f"旧窗第 {i} 句"})
        old = _drain_to(wsc, "result")["session_id"]
    wsc.send_json({"forge": True})
    return old, _drain_to(wsc, "forged")


# ---------------------------------------------------------------- 首轮命令行

def test_chat_forge_first_turn_uses_session_id_and_persona_prompt(tmp_path, cli):
    fake = cli()
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            old, forged = _chat_then_forge(wsc)
            new_id = forged["session_id"]
            assert forged["carryover"] is True and new_id != old
            uuid.UUID(new_id)
            # 换窗：不生成新 JSONL，登记表有记录
            assert _transcripts(tmp_path) == [f"{old}.jsonl"]
            assert new_id in _registry(tmp_path)

            wsc.send_json({"text": "换窗后第一句"})
            first = _drain_to(wsc, "result")
            wsc.send_json({"text": "换窗后第二句"})
            second = _drain_to(wsc, "result")

    assert first["session_id"] == new_id and second["session_id"] == new_id
    argv1, argv2 = fake.argv(-2), fake.argv(-1)
    # 首轮：--session-id <新 id> + --system-prompt-file（补裁 8），不 resume
    assert _arg(argv1, "--session-id") == new_id
    assert "--resume" not in argv1
    assert "--system-prompt" not in argv1                 # 长提示不上命令行
    prompt = fake.prompt(-2)
    # 依次：L0 → 客厅提示 → 尾部标题与原文 → 边界声明
    i_l0 = prompt.index(L.L0)
    i_hint = prompt.index(HINT)
    i_head = prompt.index("# 上一窗最后的对话（换窗前约 12 轮，原文）")
    i_tail = prompt.index("旧窗第 2 句")
    assert "用户：" in prompt[i_head:i_tail]
    i_note = prompt.index("[系统提示：刚完成换窗")
    assert i_l0 < i_hint < i_head < i_tail < i_note
    assert "助手：回复#3" in prompt          # 公开仓默认无 assistantName → 中性称呼
    assert "内心戏" not in prompt            # 思考内容不带
    # 提示文件建在系统临时目录、UTF-8，本轮结束即删除
    pf = Path(_arg(argv1, "--system-prompt-file"))
    assert pf.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    assert not pf.exists()
    # 首轮消息本身不再背 L0 和边界声明（已进系统提示），仍有 L2 召回
    assert L.L0 not in argv1[-1] and "刚完成换窗" not in argv1[-1]
    assert "换窗后第一句" in argv1[-1]
    # 第二轮：--resume <新 id>，不再带系统提示
    assert _arg(argv2, "--resume") == new_id
    assert "--system-prompt" not in argv2 and "--system-prompt-file" not in argv2
    assert "--session-id" not in argv2
    assert L.L0 not in argv2[-1] and "刚完成换窗" not in argv2[-1]
    # 首轮成功后登记销号；CLI 自己建了新会话的 transcript
    assert new_id not in _registry(tmp_path)
    assert f"{new_id}.jsonl" in _transcripts(tmp_path)


def test_first_turn_failure_keeps_registry_and_retries_first_turn(tmp_path, cli):
    fake = cli()
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            _, forged = _chat_then_forge(wsc)
            new_id = forged["session_id"]

            fake.fail_next = 1
            wsc.send_json({"text": "换窗后第一句"})
            err = _drain_to(wsc, "error")
            assert "code 1" in err["message"]
            assert new_id in _registry(tmp_path)         # 失败不删

            wsc.send_json({"text": "重试一句"})
            ok = _drain_to(wsc, "result")

    assert ok["session_id"] == new_id
    retry = fake.argv(-1)
    assert _arg(retry, "--session-id") == new_id and "--system-prompt-file" in retry
    # 失败那次与成功那次的提示文件都已删除
    assert len(fake.prompt_files()) == 2
    assert not any(Path(f).exists() for f in fake.prompt_files())
    assert "--resume" not in retry
    assert new_id not in _registry(tmp_path)


def test_prompt_file_removed_when_spawn_itself_fails(tmp_path, cli, monkeypatch):
    """CLI 根本没起来（如可执行文件缺失）也要删提示文件，且登记保留。"""
    fake = cli()
    app = create_app(_config(tmp_path))
    seen = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            _, forged = _chat_then_forge(wsc)
            new_id = forged["session_id"]

            async def boom(*args, **kwargs):
                seen.append(_arg(list(args), "--system-prompt-file"))
                raise FileNotFoundError("claude missing")

            monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", boom)
            wsc.send_json({"text": "换窗后第一句"})
            # spawn 抛错时服务端只记日志、不再回帧，这里轮询等它走完，不去收帧（会阻塞）
            for _ in range(100):
                if seen and not Path(seen[0]).exists():
                    break
                time.sleep(0.05)
    assert seen and seen[0] and not Path(seen[0]).exists()
    assert new_id in _registry(tmp_path)


def test_first_turn_after_restart_between_forge_and_first_message(tmp_path, cli):
    fake = cli()
    cfg = _config(tmp_path)
    with TestClient(create_app(cfg)) as client:
        with client.websocket_connect("/ws") as wsc:
            _, forged = _chat_then_forge(wsc)
    new_id = forged["session_id"]
    assert new_id in _registry(tmp_path)

    # 模拟服务重启：全新 app 实例（内存状态清零），登记只剩库里那份
    with TestClient(create_app(cfg)) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"switch_session": new_id})
            _drain_to(wsc, "session_switched")
            wsc.send_json({"text": "重启后第一句"})
            result = _drain_to(wsc, "result")

    assert result["session_id"] == new_id
    argv = fake.argv(-1)
    assert _arg(argv, "--session-id") == new_id and "--resume" not in argv
    prompt = fake.prompt(-1)
    assert prompt.index(L.L0) < prompt.index(HINT) < prompt.index("# 上一窗") \
        < prompt.index("[系统提示：刚完成换窗")
    assert new_id not in _registry(tmp_path)


def test_registry_with_existing_transcript_resumes_instead_of_dead_loop(tmp_path, cli):
    """首轮被中途停止/报错但 CLI 已建好会话文件：再传 --session-id 会被拒，
    应改为 resume 并销号，不能卡死。"""
    fake = cli()
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            _, forged = _chat_then_forge(wsc)
            new_id = forged["session_id"]
            # CLI 已建好文件（快照随之落定）但登记还在
            path = carryover.transcript_path(tmp_path / "projects", str(tmp_path / "cwd"), new_id)
            path.write_text(json.dumps({"type": "attachment", "attachment": {
                "type": "prompt_snapshot", "systemPrompt": [L.L0 + HINT]}},
                ensure_ascii=False) + "\n", encoding="utf-8")
            wsc.send_json({"text": "换窗后第一句"})
            result = _drain_to(wsc, "result")

    assert result["session_id"] == new_id
    argv = fake.argv(-1)
    assert _arg(argv, "--resume") == new_id and "--session-id" not in argv
    assert new_id not in _registry(tmp_path)


# ---------------------------------------------------------------- 非 chat 不变

def test_non_chat_forge_unchanged(tmp_path, cli):
    fake = cli()
    work = tmp_path / "work"
    work.mkdir()
    app = create_app(_config(tmp_path, WORKSPACES={"proj": {"label": "项目", "path": str(work)}}))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"text": "工位第一句", "cwd_key": "proj"})
            old = _drain_to(wsc, "result")["session_id"]
            wsc.send_json({"text": "工位第二句"})
            _drain_to(wsc, "result")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            new_id = forged["session_id"]
            assert forged["carryover"] is True
            # 照旧：精炼出新 JSONL，与源同目录；不进首轮登记
            assert sorted([f"{old}.jsonl", f"{new_id}.jsonl"]) == _transcripts(tmp_path)
            assert _registry(tmp_path) == {}
            wsc.send_json({"text": "工位换窗后第一句"})
            _drain_to(wsc, "result")
            wsc.send_json({"text": "工位换窗后第二句"})
            _drain_to(wsc, "result")

    first, second = fake.argv(-2), fake.argv(-1)
    # 照旧 resume，不带 --session-id / --system-prompt
    assert _arg(first, "--resume") == new_id
    assert "--session-id" not in first and "--system-prompt" not in first
    assert "--system-prompt-file" not in first
    assert fake.calls[-2]["cwd"] == str(work)
    # 照旧文字前缀：L0 → 边界声明 → 时间/正文，只在第一条
    msg = first[-1]
    assert msg.index(L.L0) < msg.index("[系统提示：刚完成换窗") < msg.index("[当前时间")
    assert "工位换窗后第一句" in msg
    assert L.L0 not in second[-1] and "刚完成换窗" not in second[-1]


# ---------------------------------------------------------------- 夺舍报警

def _two_turns(wsc):
    wsc.receive_json()
    frames = []
    for text in ("第一句", "第二句"):
        wsc.send_json({"text": text})
        while True:
            f = wsc.receive_json()
            frames.append(f)
            if f.get("type") == "result":
                break
    return frames


def _after_results(wsc, n=1):
    """result 之后服务端可能再补一帧报警。发一个不会触发 CLI 的切换请求做栅栏，
    栅栏回执之前到的帧就是本轮剩余的全部帧。"""
    wsc.send_json({"switch_session": ""})
    out = []
    while True:
        f = wsc.receive_json()
        if f.get("type") == "session_switched":
            return out
        out.append(f)


def test_alarm_when_snapshot_lacks_persona(tmp_path, cli, caplog):
    cli(snapshot_override=DEFAULT_PROMPT)
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                frames = _two_turns(wsc) + _after_results(wsc)

    errors = [f for f in frames if f.get("type") == "error"]
    assert len(errors) == 1, frames
    assert errors[0]["message"] == "人格提示没有生效，建议干净重开"
    assert "turn_id" not in errors[0]                     # 不挂在轮次上，不打断
    # 报警帧在第一轮 result 之后
    first_result = next(i for i, f in enumerate(frames) if f.get("type") == "result")
    assert frames.index(errors[0]) > first_result
    logged = [r for r in caplog.records if "persona prompt missing" in r.getMessage()]
    assert len(logged) == 1 and logged[0].levelno == logging.ERROR


def test_no_alarm_when_snapshot_has_persona(tmp_path, cli, caplog):
    cli()
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                frames = _two_turns(wsc) + _after_results(wsc)
    assert not [f for f in frames if f.get("type") == "error"]
    assert "persona prompt" not in caplog.text


def test_missing_snapshot_warns_once(tmp_path, cli, caplog):
    cli(write_snapshot=False)
    app = create_app(_config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                frames = _two_turns(wsc) + _after_results(wsc)
    assert not [f for f in frames if f.get("type") == "error"]
    warned = [r for r in caplog.records if "persona prompt unverifiable" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert "persona prompt missing" not in caplog.text


def test_empty_chat_hint_skips_check(tmp_path, cli, caplog):
    cli(snapshot_override=DEFAULT_PROMPT)
    app = create_app(_config(tmp_path, CHAT_MODE_HINT=""))
    with caplog.at_level(logging.WARNING, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                frames = _two_turns(wsc) + _after_results(wsc)
    assert not [f for f in frames if f.get("type") == "error"]
    assert "persona prompt" not in caplog.text


def test_alarm_skips_non_chat_session(tmp_path, cli, caplog):
    cli(snapshot_override=DEFAULT_PROMPT)
    work = tmp_path / "work"
    work.mkdir()
    app = create_app(_config(tmp_path, WORKSPACES={"proj": {"label": "项目", "path": str(work)}}))
    with caplog.at_level(logging.WARNING, logger="pando"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as wsc:
                wsc.receive_json()
                wsc.send_json({"text": "工位", "cwd_key": "proj"})
                _drain_to(wsc, "result")
                frames = _after_results(wsc)
    assert not [f for f in frames if f.get("type") == "error"]
    assert "persona prompt" not in caplog.text


# ---------------------------------------------------------------- 尾部渲染 / 截短

def _write(path, frames):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames),
                    encoding="utf-8")


def test_render_tail_collapses_tools_and_drops_thinking(tmp_path):
    src = tmp_path / "s.jsonl"
    _write(src, [
        {"type": "user", "message": {"role": "user", "content": "帮我查天气"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "秘密思考"},
            {"type": "text", "text": "我查一下"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"q": "成都天气"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "晴 25 度"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "明天晴"}]}},
    ])
    text, stats = carryover.render_tail(src, assistant_label="阿助")
    assert text == "用户：帮我查天气\n阿助：我查一下\n（调用了 WebSearch）\n阿助：明天晴"
    assert "秘密思考" not in text and "成都天气" not in text and "晴 25 度" not in text
    assert stats.kept_turns == 1
    assert not (tmp_path / f"{stats.new_session}.jsonl").exists()


def test_render_tail_missing_source_returns_none(tmp_path):
    text, _ = carryover.render_tail(tmp_path / "nope.jsonl")
    assert text is None


def test_trim_tail_keeps_first_turn_and_drops_middle():
    turns = [f"用户：第 {i} 问\n\n第二段\n助手：第 {i} 答" for i in range(5)]
    text = "\n\n".join(turns)
    assert carryover.trim_tail(text, len(text)) == text
    budget = len("\n\n".join([turns[0], turns[3], turns[4]]))
    trimmed = carryover.trim_tail(text, budget)
    # 按整轮删（段落里的空行不算轮边界），保留首轮与最近的轮
    assert trimmed == "\n\n".join([turns[0], turns[3], turns[4]])
    assert len(carryover.trim_tail(text, 10)) <= 10


def test_first_turn_prompt_trimmed_to_budget(tmp_path, cli):
    fake = cli()
    app = create_app(_config(tmp_path, CARRYOVER_MAX_CHARS=900))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            for i in range(6):
                wsc.send_json({"text": f"旧窗第 {i} 句" + "很长" * 40})
                _drain_to(wsc, "result")
            wsc.send_json({"forge": True})
            forged = _drain_to(wsc, "forged")
            assert forged["carryover"] is True
            wsc.send_json({"text": "换窗后第一句"})
            _drain_to(wsc, "result")
    prompt = fake.prompt(-1)
    assert len(prompt) <= 900
    # 人格与声明完整保留，被截的只有尾部
    assert L.L0 in prompt and HINT in prompt and "[系统提示：刚完成换窗" in prompt
    assert "用户：" in prompt
