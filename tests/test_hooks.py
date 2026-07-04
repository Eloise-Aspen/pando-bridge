"""钩子异常隔离：on_startup 失败的插件后续钩子全部跳过；单次钩子异常不冒泡、不阻塞其他插件。"""

from fastapi.testclient import TestClient

from pando import create_app


def _make_config(tmp_path, plugins):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",  # 故意不存在：只验证钩子调度，不真正起 claude 子进程
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": plugins,
        "ARCHIVE_INTERVAL": 600,
    }


def test_on_startup_failure_disables_all_later_hooks_for_that_plugin(tmp_path):
    """BrokenOnStartupPlugin.on_startup 抛异常后，同一插件的 on_user_message 不应再被调用
    （否则会在 WS 消息处理时抛出未捕获异常，导致连接异常中断而不是正常收到 status/hello）。"""
    app = create_app(_make_config(tmp_path, [
        "tests.fixtures.hook_plugins.BrokenOnStartupPlugin",
    ]))

    client = TestClient(app)
    with client:
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"

            ws.send_text('{"text": "hi"}')
            status1 = ws.receive_json()
            assert status1["type"] == "status"
            # 如果 on_user_message 被错误调用，BrokenOnStartupPlugin 会抛异常；
            # 核心的钩子隔离应吞掉它，流程正常推进到下一个 status（而不是连接直接断开）。
            status2 = ws.receive_json()
            assert status2["type"] == "status"


def test_broken_on_user_message_does_not_block_good_plugin(tmp_path):
    """一个插件的 on_user_message 抛异常，不影响同一轮其他插件的 on_user_message 正常执行。"""
    from tests.fixtures.hook_plugins import GoodPlugin

    GoodPlugin.calls.clear()
    app = create_app(_make_config(tmp_path, [
        "tests.fixtures.hook_plugins.BrokenOnUserMessagePlugin",
        "tests.fixtures.hook_plugins.GoodPlugin",
    ]))

    client = TestClient(app)
    with client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            ws.send_text('{"text": "hi"}')
            ws.receive_json()  # status: loading memory layers...
            ws.receive_json()  # status: thinking...

    assert "on_user_message" in GoodPlugin.calls
