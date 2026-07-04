"""MemoryProvider — 可插拔记忆后端接口。

Pando 核心（server.py）零记忆逻辑，只通过这个 Protocol 与记忆系统交互。核心**不含**任何
记忆引擎实现；记忆是一个可选的、独立的外部服务。

内置两个 provider：
- `pando.providers.null.NullMemoryProvider`：默认，全部 no-op。核心当作纯 Claude Code 终端使用。
- `pando.providers.http.HttpMemoryProvider`：配置 `MEMORY_SERVICE_URL` 后，把下面 4 个方法转成对外部
  记忆服务的 HTTP 调用（契约见下）。任何实现了这 4 个方法的对象都满足 `MemoryProvider`
  （结构化类型，无需显式继承）。

设计要点 —— **存档归记忆引擎，核心只借会话**：
    live 的 Claude 会话只存在于核心。自动存档时，核心向 provider 要一段 archive prompt
    （`build_archive_prompt`），把它丢进**当前这条 live 会话**里跑（复用上下文、省一次 LLM 调用），
    再把模型写出的记忆正文交回 provider 落库（`finalize_archive`）。存档的策略、内容与存储全部
    由记忆引擎决定，核心连提示词都不持有。

外部记忆服务 HTTP 契约（HttpMemoryProvider 使用，v2）：
    POST {url}/session_context  {}                       -> {"context": str}
    POST {url}/recall           {"query": str}           -> {"context": str}
    POST {url}/archive_prompt   {"messages": [{role, content}, ...], "force": bool} -> {"prompt": str | null}
    POST {url}/archive          {"raw": str}              -> {"stored": int, ...}
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryProvider(Protocol):
    """记忆后端需实现的 4 个方法。所有方法都应是「无记忆时安全降级」的（返回空 / None / no-op）。"""

    def build_session_context(self) -> str:
        """构建会话级长期上下文（L0 + L1），在新会话首条消息时作为 system prompt 注入。

        返回拼接好的纯文本；没有可注入内容时返回空字符串 ""。
        每次新会话只调用一次（恢复已有会话时不调用）。
        """
        ...

    def build_recall_context(self, query: str) -> str:
        """针对单条用户消息做情节记忆检索（L2），返回拼到用户消息前的召回文本。

        无相关记忆 / 命中闲聊门控时返回空字符串 ""。每条用户消息都会调用。
        """
        ...

    def build_archive_prompt(self, messages: list[dict], force: bool = False) -> str | None:
        """自动存档触发时调用：由记忆引擎决定要不要存、以及让模型写什么。

        `messages` 是本会话最近的消息列表（[{"role": ..., "content": ...}, ...]）。
        `force=True`（换窗等主动触发场景）时跳过"消息条数够不够"的门槛，但仍保留
        "这段对话值不值得存"的质量判断（如 arousal 阈值），由记忆引擎内部决定。
        返回一段注入 prompt —— 核心会把它丢进当前 live Claude 会话执行，让模型基于会话
        上下文写出记忆正文；返回 None 表示这批对话不值得存档，核心跳过本轮。
        """
        ...

    def finalize_archive(self, raw: str) -> dict:
        """接收模型针对 archive_prompt 写出的原始输出（未解析），由记忆引擎完成解析与落库。

        `raw` 是模型原始文本——JSON 提取、worthy 判断、字段解析等全部由记忆引擎内部完成，
        核心不解析这段文本的语义。
        返回形如 {"stored": int, ...} 的结果字典。
        """
        ...
