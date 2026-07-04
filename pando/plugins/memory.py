"""记忆插件 —— on_user_message 钩子做会话上下文/召回注入 + /memory-admin/* 透传代理。

对应 design/unify-core/core-design.md「已定决策 1」（管理面透传代理）与
「钩子签名与调用时机」表 on_user_message 行：注入走钩子链路；存档编排
（build_archive_prompt/finalize_archive）仍留在核心直调 provider，本插件不涉及。
"""

import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("pando")

_ADMIN_PROXY_TIMEOUT = 15.0
_HOP_BY_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}


class MemoryPlugin:
    """构造时复用核心已持有的 MemoryProvider 实例，不新开第二份 HTTP 客户端配置。"""

    def __init__(self, provider):
        self._provider = provider

    def on_user_message(self, session_id: str, text: str, is_new_session: bool) -> str:
        if is_new_session:
            return self._provider.build_session_context() or ""
        return self._provider.build_recall_context(text) or ""

    def register_routes(self, app: FastAPI) -> None:
        base_url = getattr(self._provider, "base_url", None)
        if not base_url:
            return  # NullMemoryProvider 没有外部服务可代理

        @app.api_route("/memory-admin/{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
        async def memory_admin_proxy(path: str, request: Request):
            url = f"{base_url}/{path}"
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
            try:
                async with httpx.AsyncClient(timeout=_ADMIN_PROXY_TIMEOUT) as client:
                    resp = await client.request(
                        request.method, url,
                        params=request.query_params, content=body, headers=headers,
                    )
            except Exception as e:
                log.warning("memory-admin proxy %s %s failed (degraded): %s", request.method, path, e)
                return JSONResponse(
                    {"error": "memory service unreachable", "detail": str(e)}, status_code=502
                )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
            )
