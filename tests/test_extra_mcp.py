"""额外 MCP 与权限透传共用一次配置，只在可见轮次生效。"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app
from tests.test_permission_wiring import _EchoProc, _config


@pytest.mark.parametrize("permission,extra", [(False, False), (False, True),
                                             (True, False), (True, True)])
def test_visible_mcp_combinations(tmp_path, monkeypatch, permission, extra):
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _EchoProc({}, argv)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)
    extra_cfg = {"sample": {"command": "python", "args": ["sample.py"], "env": {}}} if extra else {}
    app = create_app(_config(tmp_path, PERMISSION_PASSTHROUGH=permission,
                             EXTRA_MCP_SERVERS=extra_cfg,
                             EXTRA_ALLOWED_TOOLS=["mcp__sample__save"] if extra else []))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "hello sentinel"})
            for _ in range(60):
                if ws.receive_json().get("type") == "result":
                    break
    argv = calls[0]
    assert argv[-2] == "--"
    assert argv[-1].endswith("hello sentinel")
    assert ("--mcp-config" in argv) == (permission or extra)
    if permission or extra:
        cfg = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
        assert ("pando_permission" in cfg) == permission
        assert ("sample" in cfg) == extra
    assert ("--permission-prompt-tool" in argv) == permission
    assert ("mcp__sample__save" in " ".join(argv)) == extra


def test_auto_archive_turn_has_no_extra_tools(tmp_path, monkeypatch):
    calls = []

    class FakeMemory:
        def build_session_context(self):
            return ""

        def build_recall_context(self, query):
            return ""

        def build_archive_prompt(self, messages, force=False):
            return "archive sentinel" if messages else None

        def finalize_archive(self, raw, session=None):
            return {"stored": 0}

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _EchoProc({}, argv)

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server_mod, "get_provider", lambda *args, **kwargs: FakeMemory())
    app = create_app(_config(tmp_path, ARCHIVE_INTERVAL=0.02,
                             EXTRA_MCP_SERVERS={"sample": {"command": "python"}},
                             EXTRA_ALLOWED_TOOLS=["mcp__sample__save"]))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "hello"})
            for _ in range(60):
                if ws.receive_json().get("type") == "result":
                    break
            deadline = time.monotonic() + 2
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
    assert len(calls) >= 2
    assert "--mcp-config" in calls[0]
    assert "--mcp-config" not in calls[1]
    assert "mcp__sample__save" not in " ".join(calls[1])
