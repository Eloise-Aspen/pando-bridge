"""记忆 provider 选择 —— 按 URL 是否配置决定挂哪个后端。

配置了 memory_service_url → HttpMemoryProvider；否则 → NullMemoryProvider（默认，无记忆）。
"""

from __future__ import annotations

import logging

from ..memory_provider import MemoryProvider

log = logging.getLogger("pando.memory")


def get_provider(memory_service_url: str = "", timeout: float = 10.0,
                 headers: dict | None = None) -> MemoryProvider:
    """根据 memory_service_url 是否非空返回记忆 provider 实例。

    headers：附加在每次请求上的固定请求头（记忆服务要求鉴权时用，如 X-Memory-Token）。
    值来自调用方配置，核心不解释其含义，也不打进日志。
    """
    if memory_service_url:
        from .http import HttpMemoryProvider
        log.info("memory: HttpMemoryProvider -> %s (auth headers: %s)",
                 memory_service_url, "yes" if headers else "no")
        return HttpMemoryProvider(memory_service_url, timeout=timeout, headers=headers)

    from .null import NullMemoryProvider
    log.info("memory: NullMemoryProvider (no memory service configured)")
    return NullMemoryProvider()
