"""Task 1：ToolPolicy 存取 + CLI 参数翻译的单测。

覆盖：缺省读取、写入持久化、校验非法值回退、CLI 参数翻译三态各一、
group_for_tool 查找、reset 重置。"""

import asyncio
import json
import tempfile
from pathlib import Path

from pando.server import ToolPolicy, TOOL_POLICY_DEFAULTS, TOOL_GROUPS


def _tmp_dir():
    return Path(tempfile.mkdtemp())


def test_defaults_when_no_file():
    """文件不存在时返回缺省策略。"""
    d = _tmp_dir()
    tp = ToolPolicy(d)
    assert tp.get() == TOOL_POLICY_DEFAULTS


def test_write_and_read():
    """写入后能读回正确值。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        result = await tp.set({"file": "allow", "network": "ask"})
        assert result == {"file": "allow", "shell": "ask", "network": "ask"}
        # 重新实例化也能读到（持久化验证）
        tp2 = ToolPolicy(d)
        assert tp2.get() == {"file": "allow", "shell": "ask", "network": "ask"}

    asyncio.run(scenario())


def test_invalid_state_ignored():
    """非法状态值被忽略，保持原值。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        await tp.set({"file": "allow"})
        result = await tp.set({"file": "bogus", "shell": "deny"})
        # file 应保持 allow（bogus 被忽略），shell 更新为 deny
        assert result["file"] == "allow"
        assert result["shell"] == "deny"

    asyncio.run(scenario())


def test_invalid_group_ignored():
    """未知组名被忽略。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        result = await tp.set({"unknown_group": "allow"})
        assert result == TOOL_POLICY_DEFAULTS

    asyncio.run(scenario())


def test_corrupt_file_returns_defaults():
    """文件损坏时返回缺省。"""
    d = _tmp_dir()
    (d / "tool_policy.json").write_text("not json!", encoding="utf-8")
    tp = ToolPolicy(d)
    assert tp.get() == TOOL_POLICY_DEFAULTS


def test_cli_args_all_three_states():
    """三态各一的 CLI 参数翻译：allow → allowedTools, deny → disallowedTools, ask → 不出现。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        await tp.set({"file": "allow", "shell": "ask", "network": "deny"})
        args = tp.to_cli_args()
        # file allow → Read,Write,Edit 进 allowedTools
        assert "--allowedTools" in args
        allowed_idx = args.index("--allowedTools")
        allowed_val = args[allowed_idx + 1]
        for tool in TOOL_GROUPS["file"]:
            assert tool in allowed_val
        # network deny → WebFetch,WebSearch 进 disallowedTools
        assert "--disallowedTools" in args
        disallowed_idx = args.index("--disallowedTools")
        disallowed_val = args[disallowed_idx + 1]
        for tool in TOOL_GROUPS["network"]:
            assert tool in disallowed_val
        # shell ask → Bash 不出现在任何列表
        assert "Bash" not in allowed_val
        assert "Bash" not in disallowed_val

    asyncio.run(scenario())


def test_cli_args_all_ask():
    """全部 ask 时不产生任何参数。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        await tp.set({"file": "ask", "shell": "ask", "network": "ask"})
        assert tp.to_cli_args() == []

    asyncio.run(scenario())


def test_reset():
    """reset 重置为缺省。"""
    async def scenario():
        d = _tmp_dir()
        tp = ToolPolicy(d)
        await tp.set({"file": "allow", "shell": "deny", "network": "allow"})
        result = await tp.reset()
        assert result == TOOL_POLICY_DEFAULTS
        assert tp.get() == TOOL_POLICY_DEFAULTS

    asyncio.run(scenario())


def test_group_for_tool():
    """根据工具名查组。"""
    d = _tmp_dir()
    tp = ToolPolicy(d)
    assert tp.group_for_tool("Read") == "file"
    assert tp.group_for_tool("Write") == "file"
    assert tp.group_for_tool("Bash") == "shell"
    assert tp.group_for_tool("WebFetch") == "network"
    assert tp.group_for_tool("Unknown") is None
