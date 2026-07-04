"""NullMemoryProvider — 默认记忆后端：什么都不做。

未配置 memory_service_url 时使用。核心退化为纯 Claude Code 终端：
不注入任何长期/召回上下文，也不做自动存档。满足 MemoryProvider 协议（v2）。
"""

from __future__ import annotations


class NullMemoryProvider:
    """全部 no-op 的记忆 provider。"""

    def build_session_context(self) -> str:
        return ""

    def build_recall_context(self, query: str) -> str:
        return ""

    def build_archive_prompt(self, messages: list[dict], force: bool = False) -> str | None:
        return None

    def finalize_archive(self, raw: str) -> dict:
        return {"stored": 0}
