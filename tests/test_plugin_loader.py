"""插件声明式加载：坏路径/构造失败/import 失败都应被跳过，不阻塞其他插件或应用启动。"""

from fastapi.testclient import TestClient

from pando import create_app


def _make_config(tmp_path, plugins):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": plugins,
        "ARCHIVE_INTERVAL": 600,
    }


def test_unconstructible_and_import_failure_plugins_are_skipped(tmp_path):
    from tests.fixtures.hook_plugins import GoodPlugin

    GoodPlugin.calls.clear()
    app = create_app(_make_config(tmp_path, [
        "tests.fixtures.hook_plugins.GoodPlugin",
        "tests.fixtures.hook_plugins.UnconstructiblePlugin",
        "tests.fixtures.nonexistent_module.NoSuchPlugin",
        "tests.fixtures.hook_plugins.NoSuchClassName",
    ]))

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    assert "on_startup" in GoodPlugin.calls


def test_broken_on_startup_plugin_does_not_block_other_plugins(tmp_path):
    from tests.fixtures.hook_plugins import GoodPlugin

    GoodPlugin.calls.clear()
    app = create_app(_make_config(tmp_path, [
        "tests.fixtures.hook_plugins.BrokenOnStartupPlugin",
        "tests.fixtures.hook_plugins.GoodPlugin",
    ]))

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200

    assert "on_startup" in GoodPlugin.calls
