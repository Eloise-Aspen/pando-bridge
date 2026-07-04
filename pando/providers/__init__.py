"""记忆 provider 选择 —— 按 URL 是否配置决定挂哪个后端。

配置了 memory_service_url → HttpMemoryProvider；否则 → NullMemoryProvider（默认，无记忆）。
"""

from __future__ import annotations

import logging

from ..memory_provider import MemoryProvider

log = logging.getLogger("pando.memory")


def get_provider(memory_service_url: str = "", timeout: float = 10.0) -> MemoryProvider:
    """根据 memory_service_url 是否非空返回记忆 provider 实例。"""
    if memory_service_url:
        from .http import HttpMemoryProvider
        log.info("memory: HttpMemoryProvider -> %s", memory_service_url)
        return HttpMemoryProvider(memory_service_url, timeout=timeout)

    from .null import NullMemoryProvider
    log.info("memory: NullMemoryProvider (no memory service configured)")
    return NullMemoryProvider()
