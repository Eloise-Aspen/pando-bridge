"""HttpMemoryProvider — 把 MemoryProvider 的 4 个方法转成对外部记忆服务的 HTTP 调用。

配置了 memory_service_url 时使用。外部服务可用任意技术栈实现，只要满足契约（v2）：

    POST {url}/session_context  {}                        -> {"context": str}
    POST {url}/recall           {"query": str}            -> {"context": str}
    POST {url}/archive_prompt   {"messages": [...], "force": bool} -> {"prompt": str | null}
    POST {url}/archive          {"raw": str}               -> {"stored": int, ...}

任何网络/解析错误都**静默降级**（返回 "" / None / {"stored": 0}），绝不阻断聊天。
这些方法由核心在线程池里同步调用，故用同步 HTTP 客户端。
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("pando.memory.http")


class HttpMemoryProvider:
    """通过 HTTP 对接外部记忆服务的 provider。"""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict | None:
        """POST 并返回 JSON dict；任何异常都降级为 None（只 warning）。"""
        try:
            resp = requests.post(
                f"{self.base_url}{path}", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.warning("memory service %s failed (degraded): %s", path, e)
            return None

    def build_session_context(self) -> str:
        data = self._post("/session_context", {})
        return (data or {}).get("context", "") or ""

    def build_recall_context(self, query: str) -> str:
        data = self._post("/recall", {"query": query})
        return (data or {}).get("context", "") or ""

    def build_archive_prompt(self, messages: list[dict], force: bool = False) -> str | None:
        data = self._post("/archive_prompt", {"messages": messages, "force": force})
        if not data:
            return None
        return data.get("prompt") or None

    def finalize_archive(self, raw: str) -> dict:
        data = self._post("/archive", {"raw": raw})
        return data or {"stored": 0}
