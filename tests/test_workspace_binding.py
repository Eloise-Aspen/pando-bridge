"""feat-bridge-workstation Task 1/2：工作目录白名单下发 + 会话-目录绑定。

覆盖 spec 验收三条：
- 绑定落库：新会话带 cwd_key 发消息 → sessions.cwd_key 落库，子进程 cwd = 白名单路径，
  且后续轮次只读库里的值（前端改口也不改会话绑定）。
- 名单外回退：伪造 WS payload 的 cwd_key 被服务端拒绝回退默认 cwd，不 crash。
- 存量空串：不带 cwd_key 的会话（= 改动前的行为）cwd 仍是 CLAUDE_CWD。

另含 /health 白名单下发与「未配置 WORKSPACES 时降级单席」（Task 1 验收）。
假子进程 monkeypatch create_subprocess_exec，捕获 argv 与 kwargs（cwd 在 kwargs 里）。"""

import json
import sqlite3

from fastapi.testclient import TestClient

import pando.server as server_mod
from pando import create_app
from pando.server import normalize_cwd_key, normalize_workspaces

_WORKSPACES = {
    "chat": {"label": "客厅", "path": "C:/ws/chat"},
    "dev": {"label": "工位", "path": "C:/ws/dev"},
}


def _config(tmp_path, **extra):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }
    cfg.update(extra)
    return cfg


class _EchoProc:
    """吐 init→result 两行即结束的假子进程（与 test_permission_wiring 同款）。"""

    def __init__(self, lines):
        self._lines = lines

    class _Stdout:
        def __init__(self, lines):
            self._lines = lines
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i < len(self._lines):
                line = self._lines[self._i]
                self._i += 1
                return line
            raise StopAsyncIteration

    class _Stderr:
        async def read(self):
            return b""

    @property
    def stdout(self):
        return self._Stdout(self._lines)

    @property
    def stderr(self):
        return self._Stderr()

    returncode = 0

    async def wait(self):
        return 0


def _patch_exec(monkeypatch, spawns, session_id="s1"):
    """每次 spawn 把 (argv, cwd) 追加进 spawns，供断言子进程工作目录。"""
    def _lines():
        return [
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": session_id, "model": "stub"}).encode(),
            json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0,
                        "duration_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(),
        ]

    async def fake_exec(*argv, **kwargs):
        spawns.append({"argv": list(argv), "cwd": kwargs.get("cwd")})
        return _EchoProc(_lines())

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)


def _send(wsc, payload):
    """发一条消息并读到 result 帧。"""
    wsc.send_json(payload)
    for _ in range(60):
        if wsc.receive_json().get("type") == "result":
            return


def _db_cwd_key(tmp_path, session_id):
    conn = sqlite3.connect(str(tmp_path / "data" / "chat.db"))
    row = conn.execute("SELECT cwd_key FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_normalize_workspaces_drops_bad_entries():
    out = normalize_workspaces(
        {"dev": {"label": "工位", "path": "C:/ws/dev"},
         "bad": {"label": "无路径"},          # 缺 path → 丢弃
         "": {"path": "C:/ws/x"},             # 空 key → 丢弃
         "notdict": "C:/ws/y"},               # 值非 dict → 丢弃
        "C:/default",
    )
    assert out == {"dev": {"label": "工位", "path": "C:/ws/dev"}}


def test_normalize_workspaces_degrades_to_single_seat():
    out = normalize_workspaces(None, "C:/_Projects/Aspen/chat")
    assert list(out) == [""]
    assert out[""]["path"] == "C:/_Projects/Aspen/chat"


def test_normalize_cwd_key_whitelist():
    assert normalize_cwd_key("dev", _WORKSPACES) == "dev"
    assert normalize_cwd_key("", _WORKSPACES) == ""
    assert normalize_cwd_key(None, _WORKSPACES) == ""
    assert normalize_cwd_key("../../etc", _WORKSPACES) == ""
    assert normalize_cwd_key(123, _WORKSPACES) == ""      # 非字符串不抛异常


# ---------------------------------------------------------------------------
# /health 下发（Task 1）
# ---------------------------------------------------------------------------

def test_health_lists_workspaces(tmp_path):
    app = create_app(_config(tmp_path, WORKSPACES=_WORKSPACES))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["workspaces"] == [{"key": "chat", "label": "客厅"},
                                  {"key": "dev", "label": "工位"}]
    # 绝对路径绝不下发前端
    assert "C:/ws/dev" not in json.dumps(body)


def test_health_without_workspaces_config_degrades(tmp_path):
    """开源仓/首跑不配 WORKSPACES 时照常启动，只下发单席。"""
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert len(body["workspaces"]) == 1
    assert body["workspaces"][0]["key"] == ""


# ---------------------------------------------------------------------------
# 会话-目录绑定（Task 2）
# ---------------------------------------------------------------------------

def test_binding_persists_and_sticks(tmp_path, monkeypatch):
    """选「工位」新建对话 → cwd_key 落库、子进程 cwd = 工位；后续轮次不受前端改口影响。"""
    spawns = []
    _patch_exec(monkeypatch, spawns)
    app = create_app(_config(tmp_path, WORKSPACES=_WORKSPACES))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()  # hello
            _send(wsc, {"text": "hi", "cwd_key": "dev"})
            # 第二轮谎报 chat：会话已绑定，服务端只认库里的 dev
            _send(wsc, {"text": "again", "cwd_key": "chat"})
    assert _db_cwd_key(tmp_path, "s1") == "dev"
    assert [s["cwd"] for s in spawns] == ["C:/ws/dev", "C:/ws/dev"]


def test_unknown_cwd_key_falls_back(tmp_path, monkeypatch):
    """名单外 key（伪造 payload）不解析路径、不 crash，回退默认 cwd 并按空串落库。"""
    spawns = []
    _patch_exec(monkeypatch, spawns)
    app = create_app(_config(tmp_path, WORKSPACES=_WORKSPACES))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            _send(wsc, {"text": "hi", "cwd_key": "../../etc"})
    assert spawns[0]["cwd"] == str(tmp_path)      # = CLAUDE_CWD
    assert _db_cwd_key(tmp_path, "s1") == ""


def test_legacy_session_uses_default_cwd(tmp_path, monkeypatch):
    """存量行为：不带 cwd_key 的会话 cwd 仍是 CLAUDE_CWD，落库空串。"""
    spawns = []
    _patch_exec(monkeypatch, spawns)
    app = create_app(_config(tmp_path, WORKSPACES=_WORKSPACES))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            _send(wsc, {"text": "hi"})
    assert spawns[0]["cwd"] == str(tmp_path)
    assert _db_cwd_key(tmp_path, "s1") == ""
    # /sessions 也带出绑定，供前端标注列表项
    with TestClient(app) as client:
        assert client.get("/sessions").json()[0]["cwd_key"] == ""


def test_switch_to_bound_session_reads_db(tmp_path, monkeypatch):
    """切回已绑定会话（新连接、前端不再上报 key）时，cwd 仍是库里落定的工位。"""
    spawns = []
    _patch_exec(monkeypatch, spawns)
    app = create_app(_config(tmp_path, WORKSPACES=_WORKSPACES))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            _send(wsc, {"text": "hi", "cwd_key": "dev"})
        with client.websocket_connect("/ws") as wsc:
            wsc.receive_json()
            wsc.send_json({"switch_session": "s1"})
            wsc.receive_json()   # session_switched
            _send(wsc, {"text": "back"})
    assert spawns[-1]["cwd"] == "C:/ws/dev"
