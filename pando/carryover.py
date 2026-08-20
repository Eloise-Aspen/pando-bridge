"""精炼续窗（refined session carryover）。

forge（换窗）时对当前 CLI transcript 做一次纯本地的精炼手术：滤掉工具调用、思考块、
hook/attachment 注入等非对话噪音，只留「首个回合（内含 L0+L1 记忆注入原文）+ 最近 N 个
干净回合」，重建成一份全新的 JSONL 会话文件，供 `claude --resume <新 id>` 接续。

设计要点（对应 spec feat-forge-carryover 裁决 2/3）：
- 只读源文件：旧 transcript 一个字节都不动，新文件是新建的独立 JSONL，天然免回滚；
- 配对完整性优先：一个回合里 tool_use 与 tool_result 的 id 集合不相等（被打断/截断的回合）
  就整个回合丢弃，绝不产生孤儿帧；
- fail closed：任何一步异常都返回 None，调用方据此降级为「纯重置」的现行 forge 行为。

零第三方依赖，纯标准库。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pando.carryover")

# 参与对话的帧类型。其余（queue-operation / attachment / last-prompt / custom-title /
# agent-name / mode / permission-mode / system / file-history-snapshot ...）一律不进新会话。
DIALOGUE_TYPES = ("user", "assistant")

# 运行时注入包裹块：hook 追加上下文、系统提醒、任务通知、本地命令回显。
# 这些是上一个会话的运行时产物，续窗时重放只会污染新窗，整块剥掉。
_INJECTION_BLOCK_RE = re.compile(
    r"<(system-reminder|task-notification|local-command-stdout|local-command-stderr"
    r"|command-name|command-message|command-args)\b.*?</\1>",
    re.S | re.I,
)
# 未闭合的注入开标签（被截断的注入块）——整条帧作废，不做半截保留。
_OPEN_INJECTION_RE = re.compile(
    r"</?(system-reminder|task-notification|local-command-stdout|local-command-stderr)\b",
    re.I,
)


@dataclass
class RefineStats:
    """一次精炼的统计口径，供日志与验收使用（不含任何对话正文）。"""
    source_session: str = ""
    new_session: str = ""
    total_frames: int = 0            # 源文件里成功解析的帧总数
    dialogue_frames: int = 0         # 其中属于 user/assistant 的帧数
    kept_turns: int = 0              # 最终保留的回合数
    kept_frames: int = 0             # 最终写入新 JSONL 的帧数
    dropped_frames: int = 0          # 未进新文件的帧数（噪音 + 未入选回合 + 预算裁掉）
    dropped_turns_incomplete: int = 0  # 因 tool_use/tool_result 配对不全整体丢弃的回合数
    dropped_turns_budget: int = 0    # 因超出字符预算裁掉的回合数
    chars: int = 0                   # 保留内容的字符数（近似 token 预算）

    def as_log_fields(self) -> str:
        return (
            f"src={self.source_session} new={self.new_session} "
            f"frames={self.total_frames}/{self.dialogue_frames} "
            f"kept_turns={self.kept_turns} kept_frames={self.kept_frames} "
            f"dropped_frames={self.dropped_frames} "
            f"drop_incomplete={self.dropped_turns_incomplete} "
            f"drop_budget={self.dropped_turns_budget} chars={self.chars}"
        )


@dataclass
class _Turn:
    """一个对话回合：一条真实 user 消息 + 其后到下一条 user 消息之间的全部帧。"""
    frames: list = field(default_factory=list)   # 清洗后、准备写入的帧
    raw_count: int = 0                           # 本回合在源文件里占的对话帧数
    tool_use_ids: set = field(default_factory=set)
    tool_result_ids: set = field(default_factory=set)
    has_user_text: bool = False
    has_assistant_text: bool = False
    chars: int = 0

    @property
    def complete(self) -> bool:
        """配对完整 + 有真实一问一答，才算一个可续的干净回合。"""
        return (
            self.tool_use_ids == self.tool_result_ids
            and self.has_user_text
            and self.has_assistant_text
        )


def encode_project_dir(cwd: str) -> str:
    """把工作目录路径编码成 Claude CLI 的 transcript 目录名。

    CLI 的规则是「非字母数字一律换成连字符」：
    `C:\\_Projects\\Aspen\\dev` -> `C---Projects-Aspen-dev`。
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcript_path(projects_root, cwd: str, session_id: str) -> Path:
    """定位某个会话在某个工作目录下的 transcript 文件路径（不保证存在）。"""
    return Path(projects_root) / encode_project_dir(cwd) / f"{session_id}.jsonl"


def _strip_injections(text: str) -> str:
    """剥掉运行时注入包裹块；剩下空白即视为该帧无对话内容。"""
    cleaned = _INJECTION_BLOCK_RE.sub("", text)
    if _OPEN_INJECTION_RE.search(cleaned):
        return ""
    return cleaned.strip()


def _clean_content(content):
    """返回 (清洗后的 content, 纯文本, tool_use ids, tool_result ids)。

    content 可能是字符串或 block 列表。只保留 text block；thinking / tool_use /
    tool_result / image 等一律剔除，但 tool id 会被记下来做配对完整性判定。
    """
    tool_uses, tool_results = set(), set()

    if isinstance(content, str):
        text = _strip_injections(content)
        return (text, text, tool_uses, tool_results) if text else (None, "", tool_uses, tool_results)

    if not isinstance(content, list):
        return None, "", tool_uses, tool_results

    kept_blocks, texts = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            if block.get("id"):
                tool_uses.add(block["id"])
        elif btype == "tool_result":
            if block.get("tool_use_id"):
                tool_results.add(block["tool_use_id"])
        elif btype == "text":
            text = _strip_injections(block.get("text") or "")
            if text:
                kept_blocks.append({"type": "text", "text": text})
                texts.append(text)
        # thinking / redacted_thinking / image 等：静默丢弃，不进新会话

    if not kept_blocks:
        return None, "", tool_uses, tool_results
    return kept_blocks, "\n".join(texts), tool_uses, tool_results


def _is_dialogue(event: dict) -> bool:
    if event.get("type") not in DIALOGUE_TYPES:
        return False
    if event.get("isMeta") or event.get("isSidechain"):
        return False
    return isinstance(event.get("message"), dict)


def _load(src_path: Path):
    """逐行解析源 JSONL，跳过空行与坏行（只读，不改源文件）。"""
    events = []
    with src_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _build_turns(events, stats: RefineStats):
    """把帧序列切成回合。真实 user 文本帧开启新回合，其余帧归属当前回合。"""
    turns: list[_Turn] = []
    current: _Turn | None = None

    for event in events:
        if not _is_dialogue(event):
            continue
        stats.dialogue_frames += 1

        message = event["message"]
        cleaned, text, tool_uses, tool_results = _clean_content(message.get("content"))
        etype = event.get("type")

        # 带真实文字的 user 帧 = 新回合起点（纯 tool_result 的 user 帧不算，它属于上一回合）
        if etype == "user" and cleaned is not None:
            current = _Turn()
            turns.append(current)

        if current is None:
            # 会话以 assistant 开头（异常/截断），无所属回合，直接忽略
            continue

        current.raw_count += 1
        current.tool_use_ids |= tool_uses
        current.tool_result_ids |= tool_results

        if cleaned is None:
            continue

        new_event = dict(event)
        new_message = dict(message)
        new_message["content"] = cleaned
        new_event["message"] = new_message
        current.frames.append(new_event)
        current.chars += len(text)
        if etype == "user":
            current.has_user_text = True
        else:
            current.has_assistant_text = True

    return turns


def _select(turns, tail_turns: int, max_chars: int, stats: RefineStats):
    """裁决 2 的配方：首个干净回合 + 最近 N 个干净回合，超预算从中段往回削。"""
    clean = []
    for turn in turns:
        if turn.complete:
            clean.append(turn)
        else:
            stats.dropped_turns_incomplete += 1

    if not clean:
        return []

    # 用下标而非对象做选取——_Turn 是 dataclass（值相等），对象比较会错配重复回合
    tail_start = max(0, len(clean) - tail_turns) if tail_turns > 0 else len(clean)
    indices = sorted({0, *range(tail_start, len(clean))})
    selected = [clean[i] for i in indices]

    # 预算裁剪：首回合是身份层（L0+L1 注入原文）不可动，从最老的尾部回合开始削
    total = sum(t.chars for t in selected)
    while total > max_chars and len(selected) > 1:
        removed = selected.pop(1)
        total -= removed.chars
        stats.dropped_turns_budget += 1

    stats.chars = total
    return selected


def _rewrite(frames, new_session_id: str):
    """重写 sessionId / uuid / parentUuid 链，让新文件成为一条自洽的会话。"""
    rewritten = []
    previous_uuid = None
    for frame in frames:
        clean = dict(frame)
        frame_uuid = str(uuid.uuid4())
        clean["sessionId"] = new_session_id
        clean["uuid"] = frame_uuid
        clean["parentUuid"] = previous_uuid
        # 上一个会话的运行时痕迹，续窗后不再成立
        for key in ("leafUuid", "toolUseResult", "hookAdditionalContext",
                    "hookInfos", "hookErrors", "hookCount"):
            clean.pop(key, None)
        previous_uuid = frame_uuid
        rewritten.append(clean)
    return rewritten


def _atomic_write(dst_dir: Path, new_session_id: str, frames) -> Path:
    """先写临时文件、逐行校验可解析，再 rename 原子落位。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / f"{new_session_id}.jsonl"
    tmp_path = dst_dir / f".{new_session_id}.jsonl.tmp"

    lines = [json.dumps(frame, ensure_ascii=False) for frame in frames]
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")

    # 结构校验：落位前把刚写的文件重读一遍，逐行必须能 json.loads 且 sessionId 一致
    try:
        with tmp_path.open(encoding="utf-8") as fh:
            count = 0
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parsed = json.loads(line)
                if parsed.get("sessionId") != new_session_id:
                    raise ValueError("sessionId mismatch in refined transcript")
                count += 1
        if count != len(frames):
            raise ValueError(f"line count mismatch: {count} != {len(frames)}")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, dst_path)
    return dst_path


def refine_detailed(src_path, dst_dir, tail_turns: int = 12,
                    max_chars: int = 120_000, dry_run: bool = False):
    """精炼实现体，返回 (new_session_id | None, RefineStats)。

    任何异常都被吞掉并返回 (None, stats)——fail closed 是本功能的核心纪律，
    宁可降级成纯重置，也不能让换窗本身失败。
    """
    src_path = Path(src_path)
    stats = RefineStats(source_session=src_path.stem)
    try:
        if not src_path.is_file():
            log.info("carryover: source transcript missing (%s)", src_path.name)
            return None, stats

        events = _load(src_path)
        stats.total_frames = len(events)

        turns = _build_turns(events, stats)
        selected = _select(turns, tail_turns, max_chars, stats)
        if not selected:
            log.info("carryover: no clean turn selected (%s)", stats.as_log_fields())
            return None, stats

        frames = [frame for turn in selected for frame in turn.frames]
        stats.kept_turns = len(selected)
        stats.kept_frames = len(frames)
        stats.dropped_frames = stats.dialogue_frames - stats.kept_frames

        new_session_id = str(uuid.uuid4())
        stats.new_session = new_session_id
        if dry_run:
            return new_session_id, stats

        _atomic_write(Path(dst_dir), new_session_id, _rewrite(frames, new_session_id))
        log.info("carryover ok: %s", stats.as_log_fields())
        return new_session_id, stats
    except Exception as exc:                      # noqa: BLE001 —— fail closed 是刻意的
        log.warning("carryover failed, falling back to plain reset: %s", exc)
        return None, stats


def refine(src_path, dst_dir, tail_turns: int = 12, max_chars: int = 120_000):
    """精炼续窗入口：成功返回新 session_id，任何失败返回 None（调用方据此降级）。"""
    new_session_id, _ = refine_detailed(src_path, dst_dir, tail_turns, max_chars)
    return new_session_id
