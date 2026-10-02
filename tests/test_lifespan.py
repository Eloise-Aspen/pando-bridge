"""验证真实 TestClient 生命周期：数据库与钩子就绪，关闭主动取消并等待扫描任务。"""

import sqlite3

from fastapi.testclient import TestClient

import pando.server as server_mod
from tests.fixtures.hook_plugins import GoodPlugin


def test_lifespan_initializes_db_plugins_and_cancels_idle_scan(tmp_path, monkeypatch):
    chat_db = tmp_path / "data" / "chat.db"
    events = []
    idle_scans = []

    def on_startup(self, app, config_dict):
        # 在插件初始化当场读临时库，证明建表先于钩子，而非仅在上下文末尾成功。
        with sqlite3.connect(chat_db) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"sessions", "messages", "usage", "settings"} <= tables
        events.append("on_startup")

    def register_session_source(self, registry):
        events.append("register_session_source")

    def register_routes(self, app):
        events.append("register_routes")

    monkeypatch.setattr(GoodPlugin, "on_startup", on_startup)
    monkeypatch.setattr(GoodPlugin, "register_session_source", register_session_source,
                        raising=False)
    monkeypatch.setattr(GoodPlugin, "register_routes", register_routes, raising=False)
    monkeypatch.setattr(server_mod, "_detect_lan_ip", lambda: None)

    class TrackedTask:
        # 保留真实空闲循环；代理仅记录核心调用 cancel 与 await，排除 TestClient
        # 自行清理后台任务造成的假阳性：框架取消真实 Task 不会经过这个代理。
        def __init__(self, task):
            self.task = task
            self.cancel_called = False
            self.await_finished = False

        def cancel(self):
            self.cancel_called = True
            return self.task.cancel()

        def __await__(self):
            async def wait():
                try:
                    return await self.task
                finally:
                    self.await_finished = True
            return wait().__await__()

    create_task = server_mod.asyncio.create_task

    def track_idle_scan(coro, *args, **kwargs):
        task = create_task(coro, *args, **kwargs)
        if coro.__name__ == "_auto_carryover_loop":
            tracked = TrackedTask(task)
            idle_scans.append(tracked)
            events.append("idle_scan")
            return tracked
        return task

    monkeypatch.setattr(server_mod.asyncio, "create_task", track_idle_scan)
    app = server_mod.create_app({
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
        "DATA_DIR": str(chat_db.parent),
        "PLUGINS": ["tests.fixtures.hook_plugins.GoodPlugin"],
        "PORT": 8767,
    })
    assert not chat_db.exists()
    assert events == []

    with TestClient(app) as client:
        assert chat_db.exists()
        assert events == ["on_startup", "register_session_source", "register_routes",
                          "idle_scan"]
        assert len(idle_scans) == 1
        scan = idle_scans[0]
        assert not scan.task.done()
        assert not scan.cancel_called
        assert client.get("/health").status_code == 200

    assert scan.cancel_called
    assert scan.await_finished
    assert scan.task.cancelled()
