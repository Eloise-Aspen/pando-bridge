"""旁路记录离线验收：假 CLI 保留实际 argv，比对分段、失败隔离和只读 API。"""
import asyncio
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

import pando.server as server
from pando import create_app
from pando.trace import TraceStore, current_turn_id, segment, safe_argv, truncate_strings
from pando.providers.http import HttpMemoryProvider
from tests.test_carryover_persona_prompt import FakeCLI, _config, _drain_to


def test_store_and_read_only_api(tmp_path):
    store = TraceStore(tmp_path / "trace.db")
    store.start("one", session_id=None, inbound={"text": "hello"})
    store.update("one", returned_session_id="session", usage={"cost": 0})
    store.parts("one", 1, [segment("user_message", "user_text", "inbound", "hello")], "", "hello")
    store.event("one", 1, 0, {"type": "system", "subtype": "init", "tools": ["Read"]})
    store.start("archive", kind="archive", parent_turn_id="one", session_id="session")
    store.saved("one", "user", 12)
    store.followup("one", "archive_triggered", archive_turn_id="archive")
    store.flush()
    # 独立应用上的端点只读相同库；不启动记忆或 CLI。
    from fastapi import FastAPI
    api = FastAPI()
    store.mount(api)
    with TestClient(api) as client:
        before = store.health()["count"]
        assert len(client.get("/trace/turns", params={"session_id": "session"}).json()) == 2
        detail = client.get("/trace/turns/one").json()
        assert detail["segments_verified"] == 1
        assert detail["events"][0]["event"]["tools"] == ["Read"]
        assert detail["persist"][0]["message_id"] == 12
        assert len(detail["boundaries"]) == 2
        assert len(client.get("/trace/sessions/session/turns").json()) == 2
        assert client.get("/trace/health").json()["count"] == before
        assert client.get("/trace/turns/missing").status_code == 404
        assert client.post("/trace/turns/one").status_code == 405
        assert store.health()["count"] == before
    store.close()


def test_disabled_and_failure(tmp_path):
    off = TraceStore(tmp_path / "off.db", False)
    off.start("off")
    off.parts("off", 1, None, None, None)
    off.event("off", 1, 0, {})
    off.flush()
    assert not off.path.exists()
    bad = TraceStore(tmp_path / "bad.db")
    bad.submit(lambda conn: (_ for _ in ()).throw(RuntimeError("sentinel")))
    bad.flush()
    assert bad.health()["write_failures"] == 1
    bad.close()


def test_verification_mismatch_and_immutable_parts(tmp_path):
    store = TraceStore(tmp_path / "trace.db")
    store.start("one")
    hold = threading.Event()
    store.submit(lambda conn: hold.wait(3))
    part = segment("user_message", "user_text", "inbound", "original")
    store.parts("one", 1, [part], "", "original")
    part["text"] = "modified"
    hold.set()
    store.parts("one", 2, [segment("user_message", "user_text", "inbound", "wrong")], "", "expected")
    store.flush()
    detail = store.detail("one")
    assert detail["segments"][0]["text"] == "original"
    assert detail["segments_verified"] == 0
    assert detail["followups"][0]["kind"] == "warning"
    store.close()


def test_redaction_and_utf8_truncation():
    cfg = {"mcpServers": {"sample": {"env": {"API_KEY": "sentinel-value", "Nested": {"Authorization": "sentinel-value"}, "ordinary": "safe"}}}}
    argv = ["cli", "--mcp-config", json.dumps(cfg), "--system-prompt-file", "temporary", "--", "message"]
    clean = safe_argv(argv, "prompt")
    assert "sentinel-value" not in json.dumps(clean)
    assert "sha256=" in clean[4]
    assert "temporary" not in clean[4]
    assert "sentinel-value" in argv[2]
    assert truncate_strings("a" * 65536) == "a" * 65536
    event = {"type": "system", "nested": [{"body": "字" * 22000, "number": 1}]}
    truncated = truncate_strings(event)
    assert "66000 字节" in truncated["nested"][0]["body"]
    assert "sha256=" in truncated["nested"][0]["body"]
    assert truncated["nested"][0]["number"] == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_ws_segments_events_persist_and_mcp(tmp_path, monkeypatch, enabled):
    fake = FakeCLI(tmp_path / "projects")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake)
    app = create_app(_config(tmp_path, TRACE_ENABLED=enabled, CHAT_MODE_HINT="\nmode hint\n",
                             VOICE_INLINE_HINT="voice\n", VOICE_EXIT_HINT="exit\n",
                             EXTRA_MCP_SERVERS={"sample": {"command": "python", "env": {"API_KEY": "sentinel-private"}}}))
    frames = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            for payload in ({"text": "hello", "voice_mode": True, "archive": False}, {"text": "second", "archive": False}):
                ws.send_json(payload)
                while True:
                    frame = ws.receive_json()
                    if frame.get("type") == "turn_start":
                        frames.append(frame)
                    if frame.get("type") == "result":
                        break
            app.state.trace.flush()
            if enabled:
                for frame, call in zip(frames, fake.calls):
                    detail = client.get("/trace/turns/" + frame["turn_id"]).json()
                    assert detail["segments_verified"] == 1
                    assert "".join(p["text"] for p in detail["segments"] if p["channel"] == "user_message") == call["argv"][-1]
                    assert len(detail["persist"]) == 2
                    assert detail["events"][0]["event"]["subtype"] == "init"
                    assert detail["inbound"]["text"] in {"hello", "second"}
                    assert "sentinel-private" not in json.dumps(detail["cli_invocations"])
                    env = json.loads(call["argv"][call["argv"].index("--mcp-config") + 1])["mcpServers"]["sample"]["env"]
                    assert env["PANDO_TURN_ID"] == frame["turn_id"]
                kinds = {p["kind"] for frame in frames for p in client.get("/trace/turns/" + frame["turn_id"]).json()["segments"]}
                assert {"plugin_context", "chat_mode_hint", "voice_hint", "voice_exit_hint", "time_prefix", "user_text", "recall", "cli_builtin", "resumed_snapshot"} <= kinds
            else:
                assert client.get("/trace/health").json()["count"] == 0
                assert not app.state.trace.path.exists()
                assert all("PANDO_TURN_ID" not in " ".join(call["argv"]) for call in fake.calls)


def test_memory_header_thread_context(monkeypatch):
    seen = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"context": "safe"}
    def post(url, **kwargs):
        seen.append(kwargs.get("headers"))
        return Response()
    monkeypatch.setattr("pando.providers.http.requests.post", post)
    provider = HttpMemoryProvider("http://memory.invalid")
    async def run():
        token = current_turn_id.set("turn-sentinel")
        try:
            assert await asyncio.to_thread(provider.build_recall_context, "query") == "safe"
            await asyncio.to_thread(provider.build_session_context)
        finally:
            current_turn_id.reset(token)
        await asyncio.to_thread(provider.build_session_context)
    asyncio.run(run())
    assert seen[:2] == [{"X-Pando-Turn": "turn-sentinel"}] * 2
    assert seen[2] is None


def test_archive_forge_and_carryover_segments(tmp_path, monkeypatch):
    fake = FakeCLI(tmp_path / "projects")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake)
    class Memory:
        def build_archive_prompt(self, messages, force=False):
            return "archive prompt" if messages else None
        def finalize_archive(self, raw, session=None):
            return {"stored": 0}
    monkeypatch.setattr(server, "get_provider", lambda *a, **k: Memory())
    app = create_app(_config(tmp_path, CHAT_MODE_HINT="mode hint"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "first"})
            first = _drain_to(ws, "turn_start")["turn_id"]
            _drain_to(ws, "result")
            ws.send_json({"forge": True})
            _drain_to(ws, "forged")
            ws.send_json({"text": "after forge", "archive": False})
            second = _drain_to(ws, "turn_start")["turn_id"]
            _drain_to(ws, "result")
            app.state.trace.flush()
            rows = client.get("/trace/turns").json()
            archives = [r for r in rows if r["kind"] == "archive"]
            assert len(archives) == 1 and archives[0]["parent_turn_id"] == first
            detail = client.get("/trace/turns/" + second).json()
            assert detail["segments_verified"] == 1
            assert {"carryover_tail", "carryover_notice"} <= {p["kind"] for p in detail["segments"]}
            assert any(f["kind"] == "manual_forge" for f in client.get("/trace/turns/" + first).json()["followups"])
            archive = client.get("/trace/turns/" + archives[0]["turn_id"]).json()
            assert "archive_prompt" in {p["kind"] for p in archive["segments"]}


@pytest.mark.parametrize("retry_error", [False, True])
def test_expired_retry_preserves_id(tmp_path, monkeypatch, retry_error):
    fake = FakeCLI(tmp_path / "projects")
    async def run(*args, **kwargs):
        if retry_error and len(fake.calls) == 1:
            fake.fail_next = 1
        return await fake(*args, **kwargs)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", run)
    app = create_app(_config(tmp_path, CHAT_MODE_HINT="mode hint"))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"switch_session": "missing-session", "archive": False})
            _drain_to(ws, "session_switched")
            # 历史注入来自既有 chat.db，夹具仅使用中性文字。
            with sqlite3.connect(tmp_path / "data" / "chat.db") as conn:
                conn.execute("INSERT INTO messages (session_id,role,content,metadata,created_at) VALUES (?,?,?,?,?)", ("missing-session", "user", "history sentinel", "{}", "2020-01-01"))
            ws.send_json({"text": "retry", "archive": False})
            turn_id = _drain_to(ws, "turn_start")["turn_id"]
            frame = _drain_to(ws, "turn_start")
            assert frame["turn_id"] == turn_id
            _drain_to(ws, "error" if retry_error else "result")
            app.state.trace.flush()
            detail = client.get("/trace/turns/" + turn_id).json()
            assert [r["attempt"] for r in detail["cli_invocations"]] == [1, 2]
            assert detail["status"] == ("error" if retry_error else "expired_retry")
            assert detail["segments_verified"] == 1
            assert "expired_history" in {p["kind"] for p in detail["segments"]}


def test_attachment_individual_plugins_and_failure_chat(tmp_path, monkeypatch):
    fake = FakeCLI(tmp_path / "projects")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake)
    app = create_app(_config(tmp_path, CHAT_MODE_HINT="hint", PLUGINS=[
        "tests.fixtures.hook_plugins.LayerInjectPlugin",
        "tests.fixtures.hook_plugins.LayerInjectPlugin"]))
    with TestClient(app) as client:
        upload = client.post("/attachments", files={"file": ("sample.txt", b"neutral attachment", "text/plain")})
        assert upload.status_code == 200
        path = upload.json()["path"]
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"text": "attachment", "attachments": [path], "archive": False})
            turn_id = _drain_to(ws, "turn_start")["turn_id"]
            _drain_to(ws, "result")
            app.state.trace.flush()
            detail = client.get("/trace/turns/" + turn_id).json()
            assert detail["segments_verified"] == 1
            assert len([p for p in detail["segments"] if p["kind"] == "plugin_context"]) == 2
            assert len([p for p in detail["segments"] if p["kind"] == "recall"]) == 2
            assert "attachment_note" in {p["kind"] for p in detail["segments"]}
            # 强制每次投递落库失败，聊天结果和原有消息保存仍必须成功。
            original = app.state.trace._write
            before_failures = app.state.trace.failures
            failed = []
            def one_failure(*args):
                if not failed:
                    failed.append(True)
                    app.state.trace._failed()
                else:
                    original(*args)
            monkeypatch.setattr(app.state.trace, "_write", one_failure)
            ws.send_json({"text": "survive recorder failure", "archive": False})
            assert _drain_to(ws, "result")["text"]
            app.state.trace.flush()
            assert client.get("/trace/health").json()["write_failures"] == before_failures + 1
            monkeypatch.setattr(app.state.trace, "_write", original)


def test_api_never_calls_memory(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("trace API called memory")
    monkeypatch.setattr("pando.providers.http.requests.post", forbidden)
    app = create_app(_config(tmp_path, MEMORY_SERVICE_URL="http://memory.invalid", PLUGINS=[]))
    app.state.trace.start("one", session_id="sample")
    app.state.trace.flush()
    # 不运行 lifespan，让这里只验证四个路由本身。
    client = TestClient(app)
    def counts():
        with sqlite3.connect(app.state.trace.path) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    before = counts()
    for path in ("/trace/health", "/trace/turns", "/trace/turns/one", "/trace/sessions/sample/turns"):
        assert client.get(path).status_code == 200
    assert app.state.trace.health()["count"] == 1
    assert counts() == before
    app.state.trace.close()


def test_carryover_source_lookup_failure_preserves_prompt(tmp_path):
    """只有观测新增的来源查询出错时，实际提示仍须逐字保持一致。"""
    app = create_app(_config(tmp_path, CHAT_MODE_HINT="neutral hint"))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ws")
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))
    runner = cells["run_claude"].cell_contents
    runner_cells = dict(zip(runner.__code__.co_freevars, runner.__closure__))
    builder = runner_cells["_build_first_turn_system_prompt"].cell_contents
    builder_cells = dict(zip(builder.__code__.co_freevars, builder.__closure__))
    source_cell = builder_cells["session_chain"]
    original = source_cell.cell_contents
    token = current_turn_id.set("neutral-turn")
    try:
        pending = ("neutral tail", "neutral notice")
        expected = asyncio.run(builder("neutral-session", "neutral message", pending))
        def unavailable(_sid):
            raise sqlite3.OperationalError("neutral source lookup failure")
        source_cell.cell_contents = unavailable
        parts = []
        app.state.trace.start("neutral-turn")
        actual = asyncio.run(builder("neutral-session", "neutral message", pending, parts))
        assert actual == expected
        assert "".join(part["text"] for part in parts) == actual
        assert next(part for part in parts if part["kind"] == "carryover_tail")["source"] == "carryover:unknown"
        app.state.trace.flush()
        assert any(row["kind"] == "warning" for row in app.state.trace.detail("neutral-turn")["followups"])
    finally:
        source_cell.cell_contents = original
        current_turn_id.reset(token)
        app.state.trace.close()


def test_followup_timestamp_precedes_queue_delay(tmp_path, monkeypatch):
    import pando.trace as trace_module
    store = TraceStore(tmp_path / "trace.db")
    hold, entered = threading.Event(), threading.Event()
    clock = ["2020-01-01T00:00:00+00:00"]
    monkeypatch.setattr(trace_module, "now", lambda: clock[0])
    try:
        store.start("neutral-turn")
        def block(conn):
            entered.set()
            hold.wait(5)
        store.submit(block)
        assert entered.wait(5)
        store.followup("neutral-turn", "manual_forge")
        clock[0] = "2020-01-01T00:00:05+00:00"
        hold.set()
        store.flush()
        assert store.detail("neutral-turn")["followups"][0]["at"] == "2020-01-01T00:00:00+00:00"
    finally:
        hold.set()
        store.close()
