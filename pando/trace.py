"""对话旁路记录：单写线程保证顺序，查询只开 SQLite 只读连接。"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

current_turn_id = contextvars.ContextVar("pando_turn_id", default=None)
log = logging.getLogger("pando.trace")
BOUNDARIES = ["前端本地发送失败、未到达服务端的帧，未观测", "CLI 自带系统提示 / 工具定义 / CLAUDE.md，未观测"]
KINDS = {"plugin_context", "chat_mode_hint", "carryover_tail", "carryover_notice",
         "voice_hint", "voice_exit_hint", "time_prefix", "user_text", "recall",
         "attachment_note", "archive_prompt", "expired_history", "cli_builtin", "resumed_snapshot"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
 turn_id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT,
 returned_session_id TEXT, parent_turn_id TEXT, received_at TEXT,
 started_at TEXT, ended_at TEXT, status TEXT, model TEXT, effort TEXT,
 mode TEXT, cwd_key TEXT, voice_mode INTEGER, segments_verified INTEGER,
 usage TEXT, inbound TEXT
);
CREATE TABLE IF NOT EXISTS segments (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, attempt INTEGER,
 "order" INTEGER, channel TEXT, kind TEXT, source TEXT, text TEXT, chars INTEGER
);
CREATE TABLE IF NOT EXISTS cli_invocations (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, attempt INTEGER,
 argv TEXT, cwd TEXT, exit_code INTEGER, stderr TEXT
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, attempt INTEGER, seq INTEGER, event TEXT
);
CREATE TABLE IF NOT EXISTS persist (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, role TEXT, message_id INTEGER,
 success INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS followups (
 id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, at TEXT, kind TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, received_at);
CREATE INDEX IF NOT EXISTS turns_parent ON turns(parent_turn_id);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def truncate_strings(value):
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) > 65536:
            return f"[截断：原长 {len(raw)} 字节，sha256={hashlib.sha256(raw).hexdigest()}]"
        return value
    if isinstance(value, dict):
        return {k: truncate_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate_strings(v) for v in value]
    return value


def redact(value):
    if isinstance(value, dict):
        return {k: "[已隐去]" if any(s in k.lower() for s in
                ("token", "key", "secret", "password", "authorization")) else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def safe_argv(argv, prompt=None):
    result = list(argv)
    for index, arg in enumerate(result[:-1]):
        if arg == "--mcp-config":
            try:
                result[index + 1] = json.dumps(redact(json.loads(result[index + 1])), ensure_ascii=False)
            except (ValueError, TypeError):
                result[index + 1] = "[已隐去：无法解析 MCP 配置]"
        elif arg == "--system-prompt-file":
            result[index + 1] = "sha256=" + hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()
    return result


def segment(channel, kind, source, text):
    if kind not in KINDS:
        raise ValueError("unknown trace segment kind")
    return dict(channel=channel, kind=kind, source=source, text=text, chars=len(text))


class TraceStore:
    # 所有写入与 JSON 大字段处理只在这一个线程执行；调用端只投递、不等待。
    def __init__(self, path, enabled=True):
        self.path = Path(path)
        self.enabled = enabled
        self.failures = 0
        self.last_write = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace") if enabled else None

    def submit(self, operation, *args):
        if not self.enabled:
            return
        try:
            self._executor.submit(self._write, operation, args)
        except Exception:
            self._failed()

    def _failed(self):
        with self._lock:
            self.failures += 1
        log.warning("trace write failed (chat unaffected)")

    def _write(self, operation, args):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                conn.executescript(SCHEMA)
                operation(conn, *args)
            self.last_write = now()
        except Exception:
            self._failed()

    def flush(self):
        """仅测试/关闭使用，用户可见路径不得调用。"""
        if self._executor:
            self._executor.submit(lambda: None).result(timeout=30)

    def close(self):
        if self._executor:
            self._executor.shutdown(wait=True)

    def start(self, turn_id, **fields):
        if not self.enabled:
            return
        fields = copy.deepcopy({"turn_id": turn_id, "kind": "chat", "received_at": now(), **fields})
        def write(conn, data):
            data = dict(data)
            if "inbound" in data:
                data["inbound"] = json.dumps(data["inbound"], ensure_ascii=False)
            conn.execute(f"INSERT OR IGNORE INTO turns ({','.join(data)}) VALUES ({','.join('?' for _ in data)})", list(data.values()))
        self.submit(write, fields)

    def update(self, turn_id, **fields):
        if not self.enabled:
            return
        fields = copy.deepcopy(fields)
        def write(conn, data):
            data = dict(data)
            if "usage" in data:
                data["usage"] = json.dumps(data["usage"], ensure_ascii=False)
            conn.execute(f"UPDATE turns SET {','.join(k+'=?' for k in data)} WHERE turn_id=?", [*data.values(), turn_id])
        self.submit(write, fields)

    def parts(self, turn_id, attempt, parts, system_prompt, message):
        if not self.enabled:
            return
        parts = [dict(p) for p in parts]
        def write(conn):
            verified = all("".join(p["text"] for p in parts if p["channel"] == ch).encode("utf-8") == actual.encode("utf-8")
                           for ch, actual in (("system_prompt", system_prompt or ""), ("user_message", message)))
            conn.execute("UPDATE turns SET segments_verified=CASE WHEN segments_verified=0 THEN 0 ELSE ? END WHERE turn_id=?", (int(verified), turn_id))
            if not verified:
                log.warning("trace segments mismatch: turn=%s attempt=%s", turn_id, attempt)
                self._followup(conn, turn_id, "warning", {"reason": "segments mismatch", "attempt": attempt})
            for i, p in enumerate(parts):
                conn.execute('INSERT INTO segments (turn_id,attempt,"order",channel,kind,source,text,chars) VALUES (?,?,?,?,?,?,?,?)',
                             (turn_id, attempt, i, p["channel"], p["kind"], p["source"], p["text"], p["chars"]))
        self.submit(write)

    def invocation(self, turn_id, attempt, argv, cwd, prompt):
        if not self.enabled:
            return
        argv = list(argv)
        self.submit(lambda conn: conn.execute("INSERT INTO cli_invocations (turn_id,attempt,argv,cwd) VALUES (?,?,?,?)",
                    (turn_id, attempt, json.dumps(safe_argv(argv, prompt), ensure_ascii=False), cwd)))

    def invocation_end(self, turn_id, attempt, code, stderr):
        self.submit(lambda conn: conn.execute("UPDATE cli_invocations SET exit_code=?,stderr=? WHERE turn_id=? AND attempt=?",
                    (code, stderr.encode("utf-8")[-4096:].decode("utf-8", errors="replace"), turn_id, attempt)))

    def event(self, turn_id, attempt, seq, event):
        if not self.enabled:
            return
        event = copy.deepcopy(event)
        self.submit(lambda conn: conn.execute("INSERT INTO events (turn_id,attempt,seq,event) VALUES (?,?,?,?)",
                    (turn_id, attempt, seq, json.dumps(truncate_strings(event), ensure_ascii=False))))

    def saved(self, turn_id, role, message_id=None, error=None):
        self.submit(lambda conn: conn.execute("INSERT INTO persist (turn_id,role,message_id,success,error) VALUES (?,?,?,?,?)",
                    (turn_id, role, message_id, int(error is None), error)))

    def _followup(self, conn, turn_id, kind, details):
        conn.execute("INSERT INTO followups (turn_id,at,kind,details) VALUES (?,?,?,?)",
                     (turn_id, now(), kind, json.dumps(details, ensure_ascii=False)))

    def followup(self, turn_id, kind, **details):
        if self.enabled and turn_id:
            self.submit(self._followup, turn_id, kind, copy.deepcopy(details))

    def _read(self, sql, args=()):
        if not self.enabled or not self.path.exists():
            return []
        try:
            with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(r) for r in conn.execute(sql, args)]
        except (sqlite3.Error, OSError):
            return []

    def health(self):
        rows = self._read("SELECT count(*) AS count FROM turns")
        return dict(enabled=self.enabled, count=rows[0]["count"] if rows else 0,
                    last_write_at=self.last_write, write_failures=self.failures)

    def turns(self, session_id=None, kind=None, before=None, limit=50):
        clauses, args = [], []
        for column, value, op in (("session_id", session_id, "="), ("kind", kind, "="), ("received_at", before, "<")):
            if value is not None:
                if column == "session_id":
                    clauses.append("(session_id=? OR returned_session_id=?)")
                    args.extend((value, value))
                else:
                    clauses.append(f"{column}{op}?")
                    args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self._read("SELECT turn_id,kind,session_id,returned_session_id,parent_turn_id,received_at,started_at,ended_at,status,model,effort,mode,cwd_key,voice_mode,segments_verified FROM turns" + where + " ORDER BY received_at DESC,turn_id DESC LIMIT ?", (*args, max(1, min(limit, 200))))

    def detail(self, turn_id):
        rows = self._read("SELECT * FROM turns WHERE turn_id=?", (turn_id,))
        if not rows:
            return None
        result = rows[0]
        for name in ("usage", "inbound"):
            result[name] = json.loads(result[name]) if result[name] else None
        for table, column in (("segments", None), ("cli_invocations", "argv"), ("events", "event"), ("persist", None), ("followups", "details")):
            result[table] = self._read(f"SELECT * FROM {table} WHERE turn_id=? ORDER BY id", (turn_id,))
            if column:
                for row in result[table]:
                    row[column] = json.loads(row[column])
        result["boundaries"] = BOUNDARIES
        return result

    def session_turns(self, session_id):
        return self._read("""WITH RECURSIVE chain(turn_id) AS (
            SELECT turn_id FROM turns WHERE session_id=? OR returned_session_id=?
            UNION SELECT t.turn_id FROM turns t JOIN chain c ON t.parent_turn_id=c.turn_id)
            SELECT turn_id,kind,session_id,returned_session_id,parent_turn_id,received_at,status
            FROM turns WHERE turn_id IN (SELECT turn_id FROM chain) ORDER BY received_at,turn_id""", (session_id, session_id))

    def mount(self, app):
        # 同步端点由 FastAPI 在线程池执行，且只读 trace.db，绝不触达记忆服务。
        @app.get("/trace/health")
        def health():
            return self.health()

        @app.get("/trace/turns")
        def turns(session_id: str | None = None, kind: str | None = None, before: str | None = None, limit: int = 50):
            return self.turns(session_id, kind, before, limit)

        @app.get("/trace/turns/{turn_id}")
        def detail(turn_id: str):
            result = self.detail(turn_id)
            if result is None:
                raise HTTPException(404, "turn not found")
            return result

        @app.get("/trace/sessions/{session_id}/turns")
        def sessions(session_id: str):
            return self.session_turns(session_id)
