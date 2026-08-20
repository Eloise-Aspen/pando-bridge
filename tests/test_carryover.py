"""精炼续窗引擎（pando.carryover）单测。

用脱敏的合成 transcript 覆盖 spec feat-forge-carryover 裁决 2/3：
噪音帧过滤、tool_use/tool_result 配对完整性、首回合+尾部 N 回合选取、
字符预算裁剪、sessionId/parentUuid 链重写、原子写与源文件只读。
"""

import json
import uuid

import pytest

from pando import carryover


# ---------------------------------------------------------------- 构造工具

def _frame(ftype, content, session="src-session", **extra):
    """造一条 user/assistant 帧。content 为 str 或 block 列表。"""
    frame = {
        "type": ftype,
        "uuid": str(uuid.uuid4()),
        "parentUuid": None,
        "sessionId": session,
        "timestamp": "2026-08-20T10:00:00.000Z",
        "cwd": "C:\\work",
        "version": "2.1.200",
        "message": {"role": ftype, "content": content},
    }
    frame.update(extra)
    return frame


def _turn(user_text, assistant_text, tool_id=None, orphan=False):
    """一个完整回合；tool_id 非空则插入一对 tool_use/tool_result（orphan=True 时只插 use）。"""
    frames = [_frame("user", user_text)]
    if tool_id:
        frames.append(_frame("assistant", [
            {"type": "thinking", "thinking": "内部思考不该续过去"},
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}},
        ]))
        if not orphan:
            frames.append(_frame("user", [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "文件内容"},
            ]))
    frames.append(_frame("assistant", [{"type": "text", "text": assistant_text}]))
    return frames


def _write(path, frames):
    path.write_text(
        "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames),
        encoding="utf-8",
    )
    return path


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def src(tmp_path):
    return tmp_path / "src-session.jsonl"


@pytest.fixture
def dst(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return d


# ---------------------------------------------------------------- 路径编码

def test_encode_project_dir():
    assert carryover.encode_project_dir("C:\\_Projects\\Aspen\\dev") == "C---Projects-Aspen-dev"
    assert carryover.encode_project_dir("C:\\Users\\testuser") == "C--Users-testuser"


def test_transcript_path(tmp_path):
    p = carryover.transcript_path(tmp_path, "C:\\Users\\testuser", "abc-123")
    assert p == tmp_path / "C--Users-testuser" / "abc-123.jsonl"


# ---------------------------------------------------------------- 基本精炼

def test_refine_keeps_head_and_tail(src, dst):
    frames = []
    for i in range(10):
        frames += _turn(f"用户第 {i} 问", f"助手第 {i} 答")
    _write(src, frames)

    sid, stats = carryover.refine_detailed(src, dst, tail_turns=3, max_chars=100_000)

    assert sid is not None
    out = _read(dst / f"{sid}.jsonl")
    texts = "\n".join(json.dumps(f, ensure_ascii=False) for f in out)
    # 首回合（身份层所在）+ 最近 3 回合
    assert stats.kept_turns == 4
    assert "用户第 0 问" in texts
    for i in (7, 8, 9):
        assert f"用户第 {i} 问" in texts
    for i in (1, 2, 3, 4, 5, 6):
        assert f"用户第 {i} 问" not in texts


def test_refine_returns_session_id_only(src, dst):
    _write(src, _turn("你好", "你也好"))
    sid = carryover.refine(src, dst, tail_turns=12, max_chars=100_000)
    assert sid and (dst / f"{sid}.jsonl").is_file()


# ---------------------------------------------------------------- 噪音过滤

def test_noise_frames_dropped(src, dst):
    frames = _turn("正常问题", "正常回答", tool_id="toolu_1")
    frames += [
        {"type": "queue-operation", "operation": "enqueue", "uuid": str(uuid.uuid4())},
        {"type": "attachment", "attachment": {"type": "file"}, "uuid": str(uuid.uuid4())},
        {"type": "last-prompt", "lastPrompt": "x", "uuid": str(uuid.uuid4())},
        {"type": "system", "content": "系统帧", "uuid": str(uuid.uuid4())},
        _frame("user", "元数据帧", isMeta=True),
        _frame("assistant", [{"type": "text", "text": "支线帧"}], isSidechain=True),
    ]
    frames += _turn("第二问", "第二答")
    _write(src, frames)

    sid, stats = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    out = _read(dst / f"{sid}.jsonl")
    dumped = json.dumps(out, ensure_ascii=False)

    assert {f["type"] for f in out} == {"user", "assistant"}
    for bad in ("queue-operation", "内部思考不该续过去", "toolu_1", "元数据帧", "支线帧", "系统帧"):
        assert bad not in dumped
    # 无孤儿 tool_use / tool_result
    for frame in out:
        content = frame["message"]["content"]
        if isinstance(content, list):
            assert all(b["type"] == "text" for b in content)
    assert stats.dropped_frames > 0


def test_injection_blocks_stripped(src, dst):
    frames = _turn("第一问", "第一答")
    frames += [
        _frame("user", "<system-reminder>这是注入的提醒</system-reminder>"),
        _frame("assistant", [{"type": "text", "text": "回应注入"}]),
    ]
    frames += _turn("真实问题", "真实回答")
    _write(src, frames)

    sid, _ = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    dumped = json.dumps(_read(dst / f"{sid}.jsonl"), ensure_ascii=False)
    assert "这是注入的提醒" not in dumped
    assert "system-reminder" not in dumped
    assert "真实回答" in dumped


def test_mixed_text_and_injection_keeps_text(src, dst):
    frames = _turn("开场", "开场回应")
    frames += [
        _frame("user", "我想问个问题\n<system-reminder>忽略我</system-reminder>"),
        _frame("assistant", [{"type": "text", "text": "好的"}]),
    ]
    _write(src, frames)
    sid, _ = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    dumped = json.dumps(_read(dst / f"{sid}.jsonl"), ensure_ascii=False)
    assert "我想问个问题" in dumped
    assert "忽略我" not in dumped


# ---------------------------------------------------------------- 配对完整性

def test_incomplete_pairing_drops_whole_turn(src, dst):
    frames = _turn("首问", "首答")
    frames += _turn("被打断的一问", "被打断的一答", tool_id="toolu_orphan", orphan=True)
    frames += _turn("末问", "末答")
    _write(src, frames)

    sid, stats = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    dumped = json.dumps(_read(dst / f"{sid}.jsonl"), ensure_ascii=False)

    assert stats.dropped_turns_incomplete == 1
    assert "被打断的一问" not in dumped
    assert "被打断的一答" not in dumped
    assert "首问" in dumped and "末问" in dumped


def test_turn_without_assistant_reply_dropped(src, dst):
    frames = _turn("首问", "首答")
    frames += [_frame("user", "没人回答的问题")]
    _write(src, frames)
    sid, stats = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    dumped = json.dumps(_read(dst / f"{sid}.jsonl"), ensure_ascii=False)
    assert "没人回答的问题" not in dumped
    assert stats.dropped_turns_incomplete == 1


# ---------------------------------------------------------------- 预算裁剪

def test_char_budget_trims_from_middle_keeping_head(src, dst):
    frames = []
    for i in range(6):
        frames += _turn(f"问{i}" + "填" * 500, f"答{i}" + "充" * 500)
    _write(src, frames)

    sid, stats = carryover.refine_detailed(src, dst, tail_turns=6, max_chars=4000)
    dumped = json.dumps(_read(dst / f"{sid}.jsonl"), ensure_ascii=False)

    assert stats.dropped_turns_budget > 0
    assert stats.chars <= 4000
    assert "问0" in dumped        # 首回合（身份层）永不裁
    assert "问5" in dumped        # 最近回合保留


# ---------------------------------------------------------------- 写入纪律

def test_session_and_parent_chain_rewritten(src, dst):
    frames = []
    for i in range(3):
        frames += _turn(f"问{i}", f"答{i}")
    _write(src, frames)

    sid, _ = carryover.refine_detailed(src, dst, tail_turns=12, max_chars=100_000)
    out = _read(dst / f"{sid}.jsonl")

    uuid.UUID(sid)  # 合法 uuid4
    assert all(f["sessionId"] == sid for f in out)
    assert out[0]["parentUuid"] is None
    uuids = [f["uuid"] for f in out]
    assert len(set(uuids)) == len(uuids)
    for prev, cur in zip(out, out[1:]):
        assert cur["parentUuid"] == prev["uuid"]


def test_source_file_untouched(src, dst):
    frames = []
    for i in range(4):
        frames += _turn(f"问{i}", f"答{i}")
    _write(src, frames)
    before = src.read_bytes()

    carryover.refine(src, dst, tail_turns=2, max_chars=100_000)

    assert src.read_bytes() == before


def test_no_temp_file_left_behind(src, dst):
    _write(src, _turn("问", "答"))
    sid = carryover.refine(src, dst, tail_turns=12, max_chars=100_000)
    assert [p.name for p in dst.iterdir()] == [f"{sid}.jsonl"]


# ---------------------------------------------------------------- fail closed

def test_missing_source_returns_none(tmp_path, dst):
    assert carryover.refine(tmp_path / "nope.jsonl", dst) is None
    assert list(dst.iterdir()) == []


def test_corrupt_source_returns_none(src, dst):
    src.write_text("{ 这不是 json\n乱码乱码\n", encoding="utf-8")
    assert carryover.refine(src, dst) is None


def test_empty_source_returns_none(src, dst):
    src.write_text("", encoding="utf-8")
    assert carryover.refine(src, dst) is None


def test_tool_only_source_returns_none(src, dst):
    """整份 transcript 只有工具噪音，没有可续的干净回合 → 降级。"""
    frames = [
        _frame("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
        _frame("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]),
    ]
    _write(src, frames)
    assert carryover.refine(src, dst) is None


def test_unwritable_dst_returns_none(src, dst, monkeypatch):
    _write(src, _turn("问", "答"))
    monkeypatch.setattr(carryover, "_atomic_write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert carryover.refine(src, dst) is None
