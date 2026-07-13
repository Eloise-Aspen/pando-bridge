"""feat-memory-onboarding 关键裁决①：设了 MEMORY_SERVICE_URL 即自动挂载 MemoryPlugin。

三态：
  ① 设 URL、未在 PLUGINS 声明   -> 自动启用（/memory-admin 代理路由存在）
  ② 设 URL、已显式声明 MemoryPlugin -> 不重复（恰好一条代理路由，无双挂）
  ③ 未设 URL                     -> 不启用（无代理路由，纯终端向后兼容）

观测点：MemoryPlugin.register_routes 会挂 /memory-admin/{path} 代理路由，
故用「app.routes 里 memory-admin 路由的条数」作为「插件是否/挂了几次」的可判定证据。
"""

from fastapi.testclient import TestClient

from pando import create_app

_MEMORY_PLUGIN = "pando.plugins.memory.MemoryPlugin"


def _base_config(tmp_path):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
    }


def _memory_admin_route_count(app) -> int:
    # register_routes 只在 startup 事件里跑，必须进入 TestClient 上下文触发 startup 后再数。
    return sum(1 for r in app.routes if "memory-admin" in getattr(r, "path", ""))


def test_url_set_without_plugins_auto_enables(tmp_path):
    cfg = _base_config(tmp_path)
    cfg["MEMORY_SERVICE_URL"] = "http://127.0.0.1:9/"  # 端口不通也无妨：startup 不发网络请求
    app = create_app(cfg)
    with TestClient(app):
        assert _memory_admin_route_count(app) == 1


def test_url_set_with_explicit_plugin_not_duplicated(tmp_path):
    cfg = _base_config(tmp_path)
    cfg["MEMORY_SERVICE_URL"] = "http://127.0.0.1:9/"
    cfg["PLUGINS"] = [_MEMORY_PLUGIN]  # 用户已显式声明
    app = create_app(cfg)
    with TestClient(app):
        # 恰好一条：证明没有因自动追加而变成两个 MemoryPlugin 实例。
        assert _memory_admin_route_count(app) == 1


def test_no_url_does_not_enable_memory(tmp_path):
    cfg = _base_config(tmp_path)  # 不设 MEMORY_SERVICE_URL
    app = create_app(cfg)
    with TestClient(app):
        assert _memory_admin_route_count(app) == 0
