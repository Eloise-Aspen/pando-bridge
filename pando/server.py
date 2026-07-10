"""Pando 核心 —— 会话转发/记忆钩子编排（会话/WS/chat.db/CLI 编排/钩子调度/插件加载）。

不含具体记忆引擎实现、不 import 任何生产私有配置。所有可变项经 create_app(config)
参数注入；config 只需提供下面用到的属性（module 或任意有同名属性/键的对象均可）。
"""

import asyncio
import importlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .providers import get_provider

log = logging.getLogger("pando")

_PACKAGE_STATIC_DIR = Path(__file__).parent.parent / "static"

# 附件上传约束（feat-attachment-upload）：单文件上限 + 扩展名白名单。
# 存储位置必须落在 CLAUDE_CWD 内——Task 1 实测 CC headless 子进程用 Read 读 cwd 外
# 路径会触发权限门控（无法交互批准），只有 cwd 内的文件才免批可读。
_ATTACH_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_ATTACH_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif", "pdf", "txt", "md"}
_ATTACH_RETENTION_DAYS = 7  # 启动时清理超过此天数的旧附件

# CC 子进程 stdout 按行读，asyncio StreamReader 默认单行上限 64KB。CC 读附件（尤其 PDF）
# 时，tool_result 事件会把整份文件的 base64 作为**一整行**回吐——10MB 附件 base64≈13.3MB，
# 远超 64KB 会抛 LimitOverrunError（"chunk exceed the limit"）导致整轮崩、无输出。故把行
# 上限抬高到能容纳最大附件 base64 + JSON 包裹的量级。
_STREAM_LINE_LIMIT = 32 * 1024 * 1024  # 32MB


class PermissionBroker:
    """权限透传的挂起队列 + token 路由（feat-permission-passthrough）。

    与 WS/HTTP 传输解耦，只管「一次授权请求 → 阻塞等决策 → 返回 allow/deny」的核心状态机，
    便于单测。所有异常路径（token 失效 / 推送失败 / 超时 / 断连）一律默认拒绝——安全优先。

    - `register/unregister`：每个 WS 连接一个不透明 token，注入 MCP 服务 env，回调时反查连接。
    - `request`：HTTP 回调进来时调用，登记 Future 并阻塞等前端决策；`send_request` 由调用方注入
      （负责把 modal 推给对应 WS），broker 不碰传输。
    - `resolve`：前端 permission_response 到达时解掉对应 Future。
    - `deny_all`：连接断开时把该连接所有挂起请求默拒清空。
    多请求各自独立 request_id + Future，天然排队互不串扰（完成标准 3）。
    """

    def __init__(self, timeout: float = 120.0):
        self._timeout = timeout
        self._ws_by_token: dict = {}
        self._token_by_ws: dict = {}
        self._pending: dict = {}          # request_id -> asyncio.Future(decision dict)
        self._pending_by_ws: dict = {}    # ws -> set(request_id)，供断连清队

    def register(self, token, ws):
        self._ws_by_token[token] = ws
        self._token_by_ws[ws] = token

    def unregister(self, ws):
        tok = self._token_by_ws.pop(ws, None)
        if tok is not None:
            self._ws_by_token.pop(tok, None)

    def token_for(self, ws):
        return self._token_by_ws.get(ws)

    def ws_for_token(self, token):
        return self._ws_by_token.get(token) if token else None

    async def request(self, token, send_request) -> dict:
        """处理一次授权请求。`send_request` 是 async callable(request_id)，负责推 modal 给客户端。
        返回 {"decision": "allow"|"deny", "message": str}。"""
        ws = self._ws_by_token.get(token) if token else None
        if ws is None:
            return {"decision": "deny", "message": "no active session"}
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        self._pending_by_ws.setdefault(ws, set()).add(rid)
        try:
            await send_request(rid)
        except Exception:
            self._cleanup(rid, ws)
            return {"decision": "deny", "message": "failed to reach client"}
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return {"decision": "deny", "message": "timed out"}
        finally:
            self._cleanup(rid, ws)

    def resolve(self, request_id, allow: bool, message: str = ""):
        """前端决策回帧时解掉对应 Future；未知 request_id（已超时/清理）静默忽略。"""
        fut = self._pending.get(request_id) if request_id else None
        if fut is not None and not fut.done():
            fut.set_result({
                "decision": "allow" if allow else "deny",
                "message": message or ("" if allow else "denied by user"),
            })

    def deny_all(self, ws):
        """连接断开：该连接所有挂起请求一律默拒并清表（完成标准 2）。"""
        for rid in list(self._pending_by_ws.get(ws, set())):
            fut = self._pending.get(rid)
            if fut is not None and not fut.done():
                fut.set_result({"decision": "deny", "message": "connection closed"})
            self._pending.pop(rid, None)
        self._pending_by_ws.pop(ws, None)

    def _cleanup(self, request_id, ws):
        self._pending.pop(request_id, None)
        ids = self._pending_by_ws.get(ws)
        if ids is not None:
            ids.discard(request_id)


def _cfg(config, key: str, default=None):
    """兼容 module / dict / 任意具名属性对象的配置读取。"""
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _detect_lan_ip() -> str | None:
    """UDP connect 探测本机 LAN IP：不真正发包，只借内核路由表选出口地址。
    断网/无路由等任何失败返回 None，由调用方静默降级为只提示 localhost。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if ip and not ip.startswith("127.") else None


# ---------------------------------------------------------------------------
# 官方 5 小时/周限额用量(非公开 OAuth 接口,全链路静默降级)
# ---------------------------------------------------------------------------
# 端点为 Anthropic 非公开接口,可能随时变动;下面三个纯函数任何异常一律降级为
# None/{available:false},绝不抛错、绝不影响聊天。OAuth token 只在服务端内存内
# 流转:只读凭证文件、不写、不进日志、不进响应。
# 端点用 api.anthropic.com(实测:接受订阅 OAuth token、无 Cloudflare 挑战);
# claude.ai/api/oauth/usage 同数据但挂在 Cloudflare 后,无头请求恒 403,不可用于服务端。
_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _read_oauth_token() -> str | None:
    """从 CC 本地凭证读取 claude.ai OAuth access token(只读,不落日志)。
    发现顺序:CLAUDE_CONFIG_DIR 环境变量指向的目录 > ~/.claude/,读其下
    .credentials.json 的 claudeAiOauth.accessToken。文件不存在(API 模式/未登录/
    macOS Keychain)、解析失败、字段缺失、token 已过期一律返回 None,由上层降级。"""
    try:
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        cred_path = (Path(base) if base else Path.home() / ".claude") / ".credentials.json"
        if not cred_path.exists():
            return None
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            return None
        # access token 有寿命(CC 使用时会刷新并回写文件)。已过期就别拿死 token 去
        # 打端点换回一堆 401/限流噪声,直接当不可用降级。expiresAt 为 CC 存的毫秒时间戳;
        # 只在明确过期时短路,字段缺失/格式异常则放行交由端点判定。
        exp = oauth.get("expiresAt")
        if isinstance(exp, (int, float)) and time.time() * 1000 >= exp:
            return None
        return token
    except Exception:
        return None


async def _fetch_oauth_usage(token: str) -> dict | None:
    """带 Bearer 调官方用量端点,5s 超时。成功回原始 JSON dict;
    httpx 缺失/网络/超时/非 200/非 JSON 一律回 None。token 只进请求头,不落日志。"""
    try:
        import httpx
    except ImportError:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "pando-bridge",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_OAUTH_USAGE_URL, headers=headers)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _clean_quota(raw: dict) -> dict:
    """把官方响应清洗成最小对外形状:只取两条利用率(百分比)+各自 resets_at。
    响应结构变化时缺失字段给 None,不把官方原始结构(可能含敏感/多余字段)透传出去。"""
    def _one(block: str) -> dict:
        b = raw.get(block) or {}
        return {"utilization": b.get("utilization"), "resets_at": b.get("resets_at")}
    return {
        "available": True,
        "five_hour": _one("five_hour"),
        "seven_day": _one("seven_day"),
    }


def create_app(config) -> FastAPI:
    """组装并返回一个可运行的 FastAPI app。

    config 需要（或可选，见默认值）提供：
        CLAUDE_EXE, CLAUDE_CWD, DATA_DIR                 —— 必需
        MEMORY_SERVICE_URL (默认 "")                      —— 留空则用 NullMemoryProvider
        MEMORY_SERVICE_TIMEOUT (默认 10.0)
        PLUGINS (默认 [])                                  —— 声明式插件类路径列表
        ARCHIVE_INTERVAL (默认 600)
        STATIC_DIR (默认包内 static/，demo 前端)
        CORS_ORIGINS (默认 ["*"])
        APP_TITLE (默认 "Pando"), APP_VERSION (默认与 pando.__version__ 一致)
        SERVICE_NAME (默认 "pando")                        —— /health 的 "service" 字段
    """
    from . import __version__

    claude_exe = _cfg(config, "CLAUDE_EXE")
    claude_cwd = _cfg(config, "CLAUDE_CWD")
    data_dir: Path = Path(_cfg(config, "DATA_DIR"))
    memory_service_url = _cfg(config, "MEMORY_SERVICE_URL", "") or ""
    memory_service_timeout = _cfg(config, "MEMORY_SERVICE_TIMEOUT", 10.0)
    plugin_paths = _cfg(config, "PLUGINS", []) or []
    archive_interval = _cfg(config, "ARCHIVE_INTERVAL", 10 * 60)
    static_dir = Path(_cfg(config, "STATIC_DIR", _PACKAGE_STATIC_DIR))
    cors_origins = _cfg(config, "CORS_ORIGINS", ["*"])
    app_title = _cfg(config, "APP_TITLE", "Pando")
    app_version = _cfg(config, "APP_VERSION", __version__)
    service_name = _cfg(config, "SERVICE_NAME", "pando")
    # 语音/聊天模式的提示词文本留给调用方注入（persona 相关内容不属于公开核心）；
    # 留空则该模式不追加任何提示词，行为等同"无此功能"。
    voice_inline_hint = _cfg(config, "VOICE_INLINE_HINT", "") or ""
    voice_exit_hint = _cfg(config, "VOICE_EXIT_HINT", "") or ""
    chat_mode_hint = _cfg(config, "CHAT_MODE_HINT", "") or ""

    # 权限确认透传（feat-permission-passthrough）。默认关闭——存量用户零感知（完成标准 4）。
    # 开启后 run_claude 追加 --permission-prompt-tool + --mcp-config，把 CC 的门控工具授权
    # 请求经内嵌 MCP 小服务回调 → WS modal → 用户决策 → 原路返回（allow/deny）。
    permission_passthrough = bool(_cfg(config, "PERMISSION_PASSTHROUGH", False))
    # MCP 小服务的解释器与脚本路径。**跨平台注意**：CC 若是 Windows claude.exe（WSL 部署常见），
    # 它拉起的 MCP 进程也是 Windows 进程，故这两项须指向 Windows 侧可执行的解释器/脚本路径，
    # 由 config 显式覆盖（禁硬编码，见 CONSTRAINTS）。默认取当前解释器命令名 + 本模块同目录脚本，
    # 仅适用于 bridge 与 claude 同平台的场景（如本地 Windows 自测）。
    permission_mcp_python = _cfg(config, "PERMISSION_MCP_PYTHON", "python")
    permission_mcp_script = _cfg(
        config, "PERMISSION_MCP_SCRIPT",
        str(Path(__file__).parent / "permission_mcp.py"),
    )
    permission_timeout = float(_cfg(config, "PERMISSION_TIMEOUT", 120.0))
    # 桥用自签证书跑 HTTPS 时，回调走 https 回环，MCP 端需跳过证书校验（见 permission_mcp
    # 的 INSECURE_TLS）。仅回环 + token 关联场景，默认关。
    permission_insecure_tls = bool(_cfg(config, "PERMISSION_INSECURE_TLS", False))
    # MCP 服务回调 bridge 的地址。默认 127.0.0.1:<PORT>；WSL+Windows-claude 场景由 Windows 侧
    # 经 localhost 转发访问 WSL 服务（待真机复验连通性）。可用 PERMISSION_CALLBACK_URL 显式覆盖。
    _perm_port = _cfg(config, "PORT") or os.environ.get("BRIDGE_PORT") or 8765
    permission_callback_url = _cfg(
        config, "PERMISSION_CALLBACK_URL",
        f"http://127.0.0.1:{_perm_port}/internal/permission",
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    chat_db = data_dir / "chat.db"

    # 附件落盘目录。默认放 CLAUDE_CWD/.pando-uploads（Task 1 结论：必须在 cwd 内 CC 才
    # 能免批 Read）；允许 config 用 UPLOAD_DIR 显式覆盖。claude_cwd 缺失时降级到包内
    # 临时目录，仅为让 create_app 不炸——正常部署 claude_cwd 必配。
    upload_dir = Path(
        _cfg(config, "UPLOAD_DIR")
        or (Path(claude_cwd) / ".pando-uploads" if claude_cwd else data_dir / ".pando-uploads")
    )

    memory = get_provider(memory_service_url, timeout=memory_service_timeout)

    app = FastAPI(title=app_title, version=app_version)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    server_started_at = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # 插件机制：五钩子 + 声明式加载（design/unify-core/core-design.md）
    # -----------------------------------------------------------------------

    class SessionSourceRegistry:
        """会话来源注册占位——接口签名见 core-design.md，具体形状留给 remote-control spec 设计。"""
        pass

    session_source_registry = SessionSourceRegistry()
    failed_plugins: set[int] = set()  # 已在某钩子失败的插件（id(plugin)），后续钩子全部跳过

    def _load_plugins() -> list:
        """按声明式类路径列表 importlib 加载、实例化插件。
        导入失败或构造失败的插件跳过，不影响其他插件（core-design"插件发现/注册机制"）。
        构造时优先尝试传入核心已持有的 memory provider 实例复用（memory 插件需要），
        构造函数不接受该参数的插件（TTS/push/toy 等）退回无参构造。"""
        instances = []
        for class_path in plugin_paths:
            try:
                module_path, class_name = class_path.rsplit(".", 1)
                module = importlib.import_module(module_path)
                plugin_cls = getattr(module, class_name)
                try:
                    instances.append(plugin_cls(memory))
                except TypeError:
                    instances.append(plugin_cls())
            except Exception as e:
                log.error("plugin load failed (%s): %s", class_path, e)
        return instances

    plugin_instances: list = _load_plugins()

    def _config_as_dict() -> dict:
        """把 config 摊平成 dict，供 on_startup 钩子使用。"""
        if isinstance(config, dict):
            return dict(config)
        return {k: v for k, v in vars(config).items() if k.isupper()}

    def _call_hook(plugin, hook_name: str, *args, default=None):
        """公共钩子调用包裹：异常记日志、不重新抛出、不阻塞主流程（core-design"错误隔离策略"）。
        某插件的 on_startup 一旦失败，视为初始化失败，该插件后续任何钩子都跳过不再调用。"""
        if id(plugin) in failed_plugins:
            return default
        hook = getattr(plugin, hook_name, None)
        if hook is None:
            return default
        try:
            return hook(*args)
        except Exception as e:
            log.error("plugin hook %s.%s failed: %s", type(plugin).__name__, hook_name, e)
            if hook_name == "on_startup":
                failed_plugins.add(id(plugin))
            return default

    async def _run_on_user_message_hooks(loop, session_id: str | None, text: str, is_new_session: bool) -> str:
        """依次调用所有插件的 on_user_message 钩子（阻塞式钩子丢线程池执行），拼接非空注入文本。"""
        combined = ""
        for plugin in plugin_instances:
            injected = await loop.run_in_executor(
                None,
                lambda p=plugin: _call_hook(p, "on_user_message", session_id or "", text, is_new_session, default=""),
            )
            if injected:
                combined += injected
        return combined

    # -----------------------------------------------------------------------
    # Chat history DB
    # -----------------------------------------------------------------------

    def _chat_conn() -> sqlite3.Connection:
        data_dir.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(chat_db))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_chat_db():
        conn = _chat_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                model TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_archived_id INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT DEFAULT '',
                model TEXT DEFAULT '',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read INTEGER DEFAULT 0,
                cache_create INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_created ON usage(created_at);
        """)
        # 旧库兼容：last_archived_id 是历史加列;usage 表用 CREATE TABLE IF NOT EXISTS,
        # 旧库首次启动自动补建,无需额外迁移语句。
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_archived_id INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.commit()
        conn.close()

    def save_session(session_id: str, model: str = ""):
        conn = _chat_conn()
        now = now_iso()
        conn.execute("""
            INSERT INTO sessions (id, title, model, created_at, updated_at)
            VALUES (?, '', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = ?, model = COALESCE(NULLIF(?, ''), model)
        """, (session_id, model, now, now, now, model))
        conn.commit()
        conn.close()

    def get_last_archived_id(session_id: str) -> int:
        conn = _chat_conn()
        row = conn.execute("SELECT last_archived_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 0

    def set_last_archived_id(session_id: str, msg_id: int):
        conn = _chat_conn()
        conn.execute("UPDATE sessions SET last_archived_id = ? WHERE id = ?", (msg_id, session_id))
        conn.commit()
        conn.close()

    def get_recent_messages(session_id: str, limit: int = 30, after_id: int = 0) -> list[dict]:
        """Return recent messages. Each dict has 'role', 'content', 'id'."""
        conn = _chat_conn()
        cur = conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
            (session_id, after_id, limit),
        )
        msgs = [{"id": row[0], "role": row[1], "content": row[2]} for row in cur.fetchall()]
        conn.close()
        msgs.reverse()
        return msgs

    def save_message(session_id: str, role: str, content: str, metadata: dict | None = None):
        conn = _chat_conn()
        now = now_iso()
        conn.execute(
            "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        conn.commit()
        conn.close()

    def list_sessions(limit: int = 30) -> list[dict]:
        conn = _chat_conn()
        cur = conn.execute("""
            SELECT s.id, s.title, s.model, s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM messages WHERE session_id = s.id) as msg_count,
                   (SELECT content FROM messages WHERE session_id = s.id AND role = 'user'
                    ORDER BY id ASC LIMIT 1) as first_msg
            FROM sessions s
            ORDER BY s.updated_at DESC LIMIT ?
        """, (limit,))
        sessions = []
        for row in cur.fetchall():
            title = row[1]
            if not title and row[6]:
                title = row[6][:50] + ("…" if len(row[6] or "") > 50 else "")
            sessions.append({
                "id": row[0],
                "title": title,
                "model": row[2],
                "created_at": row[3],
                "updated_at": row[4],
                "msg_count": row[5],
            })
        conn.close()
        return sessions

    def get_session_messages(session_id: str) -> list[dict]:
        conn = _chat_conn()
        cur = conn.execute(
            "SELECT role, content, metadata, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        msgs = []
        for row in cur.fetchall():
            meta = {}
            try:
                meta = json.loads(row[2]) if row[2] else {}
            except json.JSONDecodeError:
                pass
            msgs.append({
                "role": row[0],
                "content": row[1],
                "metadata": meta,
                "created_at": row[3],
            })
        conn.close()
        return msgs

    def record_usage(session_id: str | None, model: str, usage: dict):
        """把一次 result 的 token 用量落 usage 表,供 /usage/stats 聚合。
        model 为空(用户选「默认」)时归到 'default' 名下,避免分组丢失。
        用量统计属旁路观测,任何写库异常都只记日志、不阻断聊天主流程。"""
        try:
            conn = _chat_conn()
            conn.execute(
                "INSERT INTO usage (session_id, model, input_tokens, output_tokens, "
                "cache_read, cache_create, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id or "",
                    model or "default",
                    int(usage.get("input_tokens", 0) or 0),
                    int(usage.get("output_tokens", 0) or 0),
                    int(usage.get("cache_read", 0) or 0),
                    int(usage.get("cache_create", 0) or 0),
                    now_iso(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error("record_usage failed: %s", e)

    def usage_stats() -> dict:
        """按模型分组的 token 用量,分「今日」与「累计」两组。
        今日以 UTC 日界判定(date('now') 用 UTC,与 created_at 存储的 UTC ISO 一致,
        避免时区错位导致今日统计对不上)。每项含 model / token 数 / 请求次数。"""
        conn = _chat_conn()

        def _grouped(where: str) -> list[dict]:
            cur = conn.execute(
                "SELECT model, SUM(input_tokens), SUM(output_tokens), "
                "SUM(cache_read), SUM(cache_create), COUNT(*) "
                f"FROM usage {where} GROUP BY model "
                "ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC"
            )
            return [{
                "model": r[0] or "default",
                "input_tokens": r[1] or 0,
                "output_tokens": r[2] or 0,
                "cache_read": r[3] or 0,
                "cache_create": r[4] or 0,
                "requests": r[5] or 0,
            } for r in cur.fetchall()]

        result = {
            "today": _grouped("WHERE date(created_at) = date('now')"),
            "total": _grouped(""),
        }
        conn.close()
        return result

    # -----------------------------------------------------------------------
    # App lifecycle
    # -----------------------------------------------------------------------

    @app.on_event("startup")
    async def startup():
        _init_chat_db()
        log.info("chat DB ready: %s", chat_db)
        _cleanup_old_attachments()

        config_dict = _config_as_dict()
        for plugin in plugin_instances:
            _call_hook(plugin, "on_startup", app, config_dict)
            _call_hook(plugin, "register_session_source", session_source_registry)
        for plugin in plugin_instances:
            _call_hook(plugin, "register_routes", app)

        # 启动横幅：本机 + 局域网访问地址，附手机接入指引。
        # 端口来源：config PORT > 环境变量 BRIDGE_PORT > 默认 8765（与 README/.env.example 约定一致）——
        # 核心拿不到 uvicorn 实际绑定的端口，此处仅作提示用途。
        # 用 print 而非 log.info：uvicorn 默认不给应用 logger 配 handler，快速起步场景日志不可见，
        # 而库内不应擅自修改全局 logging 配置。横幅纯属提示，任何输出异常（如终端编码不支持
        # emoji）都不应阻断启动，整段兜底吞掉。
        try:
            banner_port = _cfg(config, "PORT") or os.environ.get("BRIDGE_PORT") or 8765
            print(f"\n  Pando ready:  http://127.0.0.1:{banner_port}", flush=True)
            lan_ip = _detect_lan_ip()
            if lan_ip:
                print(f"  LAN access:   http://{lan_ip}:{banner_port}", flush=True)
            print("  📱 want it on your phone? see README → Reach it from your phone\n", flush=True)
        except Exception:
            pass

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # -----------------------------------------------------------------------
    # HTTP endpoints
    # -----------------------------------------------------------------------

    @app.get("/health")
    async def health():
        # CLAUDE_EXE 默认是命令名 "claude"（靠 PATH 解析），也允许配相对/绝对路径。
        # which 兜命令名（在 PATH 里找得到即算装好），exists 兜显式路径，两者其一即 found，
        # 否则旧写法对命令名恒 missing（Path("claude").exists() 永远 False）。
        claude_ok = shutil.which(claude_exe) is not None or Path(claude_exe).exists()
        return {
            "status": "ok",
            "service": service_name,
            "version": app_version,
            "hostname": socket.gethostname(),
            "server_time": now_iso(),
            "started_at": server_started_at.isoformat(),
            "claude_cli": "found" if claude_ok else "missing",
        }

    @app.get("/sessions")
    async def api_list_sessions(limit: int = 30):
        return list_sessions(limit=limit)

    @app.delete("/sessions/{session_id}")
    async def api_delete_session(session_id: str):
        conn = _chat_conn()
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.get("/sessions/{session_id}/messages")
    async def api_session_messages(session_id: str):
        return get_session_messages(session_id)

    @app.get("/usage/stats")
    async def api_usage_stats():
        """只读:按模型分组的 token 用量(今日/累计),供设置页用量区展示。"""
        return usage_stats()

    # /usage/quota 的服务端缓存:官方端点是非公开接口,60s TTL 防被打成高频轮询。
    # 闭包内可变字典存最近一次成功清洗结果 + 单调时钟时间戳(避免系统时间回拨影响)。
    _quota_cache = {"data": None, "ts": 0.0}
    _QUOTA_TTL = 60.0

    @app.get("/usage/quota")
    async def api_usage_quota():
        """官方 5h 窗 / 周限额利用率(静默降级):无凭证 / 端点 401-403 / 超时 /
        响应形状变化一律回 {available:false},绝不抛 500、绝不影响聊天。
        OAuth token 只在服务端内存内流转,不进本响应、不进日志。"""
        now = time.monotonic()
        # 命中未过期缓存直接回,不碰官方端点
        if _quota_cache["data"] is not None and (now - _quota_cache["ts"]) < _QUOTA_TTL:
            return _quota_cache["data"]
        token = _read_oauth_token()
        if not token:
            return {"available": False}
        raw = await _fetch_oauth_usage(token)
        if not raw:
            return {"available": False}
        try:
            cleaned = _clean_quota(raw)
        except Exception:
            return {"available": False}
        # 只缓存成功结果:失败不写缓存,下次(设置页再次打开)可即时重试
        _quota_cache["data"] = cleaned
        _quota_cache["ts"] = now
        return cleaned

    # -----------------------------------------------------------------------
    # 附件上传（feat-attachment-upload）
    # -----------------------------------------------------------------------

    def _cleanup_old_attachments():
        """启动时清理超过保留天数的旧附件。目录不存在、单文件删除失败都静默跳过，
        绝不阻断服务启动（清理属旁路维护，失败只记日志）。"""
        cutoff = time.time() - _ATTACH_RETENTION_DAYS * 86400
        removed = 0
        try:
            if not upload_dir.exists():
                return
            for p in upload_dir.rglob("*"):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed:
                log.info("attachment cleanup: removed %d file(s) older than %d days",
                         removed, _ATTACH_RETENTION_DAYS)
        except Exception as e:
            log.warning("attachment cleanup skipped: %s", e)

    @app.post("/attachments")
    async def upload_attachment(file: UploadFile = File(...)):
        """接收单个上传文件，落 upload_dir/YYYYMM/ 下 UUID 重命名后的文件，返回其绝对路径。
        校验：扩展名白名单 + 大小上限。UUID 重命名 + 只保留白名单内的扩展名，杜绝用原始
        文件名做路径注入。返回的 path 供聊天 WS 注入，Task 3 会再校验它确实落在 upload_dir 内。"""
        # 扩展名白名单（大小写不敏感），不信任原始文件名的其余部分
        ext = Path(file.filename or "").suffix.lstrip(".").lower()
        if ext not in _ATTACH_ALLOWED_EXT:
            raise HTTPException(status_code=400, detail=f"unsupported file type: .{ext or '?'}")
        # 读到上限+1 字节即可判定是否超限，避免把超大文件整个读进内存
        data = await file.read(_ATTACH_MAX_BYTES + 1)
        if len(data) > _ATTACH_MAX_BYTES:
            raise HTTPException(status_code=413, detail="file too large (max 10MB)")
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        # 按月分目录 + UUID 文件名（只带白名单扩展名），杜绝路径穿越/覆盖
        subdir = upload_dir / datetime.now().strftime("%Y%m")
        subdir.mkdir(parents=True, exist_ok=True)
        dest = subdir / f"{uuid.uuid4().hex}.{ext}"
        dest.write_bytes(data)
        log.info("attachment saved: %s (%d bytes)", dest, len(data))
        return {"path": str(dest)}

    def _validate_attachment_paths(paths) -> list[str]:
        """只放行落在 upload_dir 内、真实存在的附件路径，挡任意路径注入。
        客户端只应回传 POST /attachments 返回的 path；这里再做前缀校验兜底——
        防止伪造 /etc/passwd 之类路径诱导 CC 用 Read 去读任意文件。"""
        if not isinstance(paths, list):
            return []
        base = upload_dir.resolve()
        valid: list[str] = []
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            try:
                rp = Path(p).resolve()
            except (OSError, ValueError):
                continue
            # 前缀校验：解析后的路径必须等于 upload_dir 或落在其下，且是真实文件
            if (rp == base or base in rp.parents) and rp.is_file():
                valid.append(str(rp))
        return valid

    def _build_attachment_note(valid_paths: list[str]) -> str:
        """把校验过的附件路径拼成注入消息的固定格式段落，供 CC 用 Read 工具查看。
        路径尽量转成「相对 claude_cwd」形式：CC 子进程 cwd 即 claude_cwd，相对路径由它
        自行解析。这样跨平台一致，且规避一个跨边界陷阱——WSL 里跑 Windows 版 claude.exe
        时，注入的挂载路径 /mnt/c/... 会被 Read 误转成 \\mnt\\c\\...（未映射成 C:\\...），
        被判定在 workspace 外 → Read 触发权限门控读不了。落在 cwd 外的路径（自定义
        UPLOAD_DIR）保持绝对，CC 本就读不到、由权限层拒绝，符合预期。"""
        if not valid_paths:
            return ""
        base = Path(claude_cwd).resolve() if claude_cwd else None
        display = []
        for p in valid_paths:
            shown = p
            if base is not None:
                try:
                    shown = Path(p).resolve().relative_to(base).as_posix()
                except ValueError:
                    shown = p  # 不在 cwd 下，保持绝对
            display.append(shown)
        lines = "\n".join(display)
        return f"\n\n[用户上传了附件，请用 Read 工具查看以下文件]\n{lines}"

    # -----------------------------------------------------------------------
    # Claude Code subprocess wrapper
    # -----------------------------------------------------------------------

    # 每个 WS 连接「当前用户可见轮次」的在途子进程登记表:stop 消息据此找到要杀的 proc,
    # 断连回收也用它。只登记 silent=False 的轮次(自动存档轮不给用户「停止」入口)。
    inflight_procs: dict[WebSocket, asyncio.subprocess.Process] = {}
    # 已请求停止的连接:被杀轮次的 result 事件不会到达,run_claude 据此改发 stopped 结束帧而非 error。
    stopped_conns: set[WebSocket] = set()

    # 权限透传挂起队列 + token 路由。开关关闭时也实例化（空表无副作用），run_claude 靠
    # token_for(ws) 是否有值决定要不要追加 CLI 参数——连接注册只在开关打开时发生。
    permission_broker = PermissionBroker(timeout=permission_timeout)

    @app.post("/internal/permission")
    async def internal_permission(req: Request):
        """MCP 权限小服务的回调端点（仅内部，绑 127.0.0.1）。
        收到 {token, tool_name, input, tool_use_id} → broker 反查连接 → 推 permission_request
        modal → 阻塞等前端 permission_response（或 120s 超时 / 断连）→ 返回 {"decision":...}。
        任何异常路径一律默拒——与 MCP 端的默认拒绝哲学一致。"""
        try:
            body = await req.json()
        except Exception:
            return {"decision": "deny", "message": "bad request"}

        async def _push(request_id: str):
            ws = permission_broker.ws_for_token(body.get("token"))
            await ws.send_text(json.dumps({
                "type": "permission_request",
                "request_id": request_id,
                "tool": body.get("tool_name", ""),
                "input": body.get("input", {}),
                "tool_use_id": body.get("tool_use_id", ""),
            }, ensure_ascii=False))

        return await permission_broker.request(body.get("token"), _push)

    async def _stop_inflight(ws: WebSocket):
        """终止某连接的在途子进程:terminate 优先,POSIX 兜底 2s 未退则 kill。
        找不到在途 proc(流已结束/快速连点)直接忽略,不产生副作用。"""
        proc = inflight_procs.get(ws)
        if proc is None:
            return
        stopped_conns.add(ws)
        try:
            proc.terminate()
        except ProcessLookupError:
            return  # 已退出
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()

    async def run_claude(
        message: str,
        session_id: str | None,
        ws: WebSocket,
        system_prompt: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        silent: bool = False,
        _retry: bool = False,
    ):
        cmd = [
            claude_exe,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            # settings.json 的 showThinkingSummaries 只在交互式终端生效（isInteractive 门控），
            # 子进程管道模式必须用这个 CLI flag 才能拿到非空的 thinking 文本
            "--thinking-display", "summarized",
        ]
        if session_id:
            cmd += ["--resume", session_id]
        elif system_prompt:
            cmd += ["--system-prompt", system_prompt]

        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]

        # 权限透传：仅用户可见轮次开启（silent 存档轮无人盯着，开了只会 120s 后默拒白等）。
        # 用内联 JSON 字符串传 --mcp-config（CC 支持 file 或 string），免临时文件与清理。
        # 版本注记：--permission-prompt-tool 在 CC 2.1.202 已从 --help 隐藏但仍生效（Task 1 实测），
        # CC 升级时需复验本机制。
        if permission_passthrough and not silent:
            tok = permission_broker.token_for(ws)
            if tok:
                mcp_cfg = json.dumps({"mcpServers": {"pando_permission": {
                    "command": permission_mcp_python,
                    "args": [permission_mcp_script],
                    "env": {
                        "PANDO_PERMISSION_CALLBACK_URL": permission_callback_url,
                        "PANDO_PERMISSION_TOKEN": tok,
                        # 本端 HTTP 等待上限略大于 bridge 120s，让 bridge 的默拒先触发
                        "PANDO_PERMISSION_HTTP_TIMEOUT": str(permission_timeout + 30),
                        # 自签 HTTPS 回环回调时跳过证书校验（默认不注入=校验）
                        **({"PANDO_PERMISSION_INSECURE_TLS": "1"} if permission_insecure_tls else {}),
                    },
                }}}, ensure_ascii=False)
                # 顺序要紧：--mcp-config 是可变参数（<configs...>），会贪婪吞掉后面所有非选项
                # token——若紧跟其后的是 message（位置参数），message 会被误当成第二个 config 路径
                # → CC 报 "MCP config file not found"。故把非可变的 --permission-prompt-tool 夹在
                # --mcp-config 的值与 message 之间，终止其贪婪消费，保护 message。
                cmd += [
                    "--mcp-config", mcp_cfg,
                    "--permission-prompt-tool", "mcp__pando_permission__approve",
                ]

        cmd.append(message)

        log.info("spawn: %s (session=%s, model=%s)", message[:80], session_id or "new", model or "default")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=claude_cwd,
            # 抬高单行上限：CC 读附件时 tool_result 的 base64 会是超长单行（见常量说明）
            limit=_STREAM_LINE_LIMIT,
        )

        # 只登记用户可见轮次:静默存档轮不给「停止」入口,避免和可见轮的 proc 互相覆盖
        if not silent:
            inflight_procs[ws] = proc

        new_session_id = session_id
        full_text = ""
        thinking_text = ""
        result_meta = {}
        # 上下文占用用「最后一个 assistant 事件的 message.usage」快照(不是 result 的账单聚合)——
        # result.usage 把本轮所有工具往返的 token 累加,当"上下文占用"看会虚高且不随压缩回落;
        # 最后一次 API 调用的 usage 才是"此刻上下文里装了多少"。覆盖式更新,最后一条胜出。
        last_assistant_usage = {}
        # 实际使用的模型名以 init 事件为准(用户选「默认」时 model 参数为空,
        # CC 回报的 model 才是真实模型),用量落库时按它分组。
        init_model = model or ""

        try:
            try:
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "system" and event.get("subtype") == "init":
                        new_session_id = event.get("session_id", new_session_id)
                        init_model = event.get("model") or init_model
                        if not silent:
                            save_session(new_session_id, model or "")
                            await ws.send_text(json.dumps({
                                "type": "session",
                                "session_id": new_session_id,
                                "model": event.get("model", ""),
                            }, ensure_ascii=False))

                    elif etype == "assistant":
                        msg = event.get("message", {})
                        # 每个 assistant 事件都带 message.usage;取最后一条作上下文占用快照
                        if msg.get("usage"):
                            last_assistant_usage = msg["usage"]
                        for block in msg.get("content", []):
                            btype = block.get("type")
                            if btype == "thinking":
                                thinking_text += block.get("thinking", "")
                                if not silent:
                                    await ws.send_text(json.dumps({
                                        "type": "thinking",
                                        "text": block.get("thinking", ""),
                                    }, ensure_ascii=False))
                            elif btype == "text":
                                text = block["text"]
                                full_text += text
                                if not silent:
                                    await ws.send_text(json.dumps({
                                        "type": "text",
                                        "text": text,
                                    }, ensure_ascii=False))
                            elif btype == "tool_use" and not silent:
                                await ws.send_text(json.dumps({
                                    "type": "tool_use",
                                    "tool": block.get("name", ""),
                                    "input_preview": str(block.get("input", ""))[:200],
                                }, ensure_ascii=False))

                    elif etype == "result":
                        # usage(result 聚合)= 本轮账单口径,用于落库 /usage/stats 与消息脚注成本行;
                        # 保持不动——它衡量"这轮花了多少 token",是计费语义,不是上下文占用。
                        usage = event.get("usage", {})
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        input_tok = usage.get("input_tokens", 0)
                        output_tok = usage.get("output_tokens", 0)
                        total_input = cache_read + cache_create + input_tok
                        cache_hit_pct = round(cache_read / total_input * 100) if total_input else 0

                        # context(上下文占用)= 最后一个 assistant 事件的 usage 快照,与账单口径分开。
                        # 占用 = 输入 + 缓存读 + 缓存写 + 输出(下一轮会带着这些进上下文);
                        # 分母取 result.modelUsage 里该模型的 contextWindow(拿不到则回退 200k)。
                        # 压缩后此值回落是预期:CC 内部压掉早期内容,上下文真的变小了。
                        context_meta = None
                        if last_assistant_usage:
                            lu = last_assistant_usage
                            l_input = lu.get("input_tokens", 0)
                            l_output = lu.get("output_tokens", 0)
                            l_cread = lu.get("cache_read_input_tokens", 0)
                            l_ccreate = lu.get("cache_creation_input_tokens", 0)
                            l_total_in = l_input + l_cread + l_ccreate
                            ctx_window = 0
                            for mu in (event.get("modelUsage") or {}).values():
                                cw = mu.get("contextWindow") or 0
                                if cw > ctx_window:
                                    ctx_window = cw
                            context_meta = {
                                "used": l_input + l_cread + l_ccreate + l_output,
                                "window": ctx_window or 200000,
                                "input": l_input,
                                "output": l_output,
                                "cache_read": l_cread,
                                "cache_create": l_ccreate,
                                "cache_hit_pct": round(l_cread / l_total_in * 100) if l_total_in else 0,
                            }

                        result_meta = {
                            "cost_usd": event.get("total_cost_usd"),
                            "duration_ms": event.get("duration_ms"),
                            "thinking": thinking_text if thinking_text else None,
                            "usage": {
                                "input_tokens": input_tok,
                                "output_tokens": output_tok,
                                "cache_read": cache_read,
                                "cache_create": cache_create,
                                "cache_hit_pct": cache_hit_pct,
                            },
                        }
                        if context_meta:
                            result_meta["context"] = context_meta

                        if not silent:
                            # 用量落库:只记用户可见的对话轮次(silent=True 的自动存档轮不计),
                            # 保证「发一条消息 → 统计数字增长」的可验证链路清晰可归因。
                            record_usage(new_session_id, init_model, result_meta["usage"])
                            await ws.send_text(json.dumps({
                                "type": "result",
                                "text": full_text,
                                "session_id": new_session_id,
                                **result_meta,
                            }, ensure_ascii=False))

            except WebSocketDisconnect:
                proc.kill()
                raise

            await proc.wait()

            # 用户主动停止:proc 被 terminate/kill,stdout 提前 EOF,result 事件从未到达
            # (result_meta 仍为空)。不当作进程崩溃上报,改发一帧带 stopped 的 result 结束帧,
            # 保留已生成的 full_text;用量无从补录——result 没到就没有可靠数据(见 spec「不做什么」)。
            if (not silent) and (ws in stopped_conns):
                if not result_meta:
                    try:
                        await ws.send_text(json.dumps({
                            "type": "result",
                            "text": full_text,
                            "session_id": new_session_id,
                            "stopped": True,
                        }, ensure_ascii=False))
                    except WebSocketDisconnect:
                        pass
                # result_meta 非空 = 停止请求到达时该轮已正常收尾,正常 result 帧已发,无需再动作
                return new_session_id, full_text, result_meta

            if proc.returncode and proc.returncode != 0:
                stderr_bytes = await proc.stderr.read()
                err = stderr_bytes.decode("utf-8", errors="replace").strip()

                if session_id and not _retry and "No conversation found" in err:
                    log.warning("session %s expired, retrying as new session", session_id)
                    if not silent:
                        try:
                            await ws.send_text('{"type":"session_expired"}')
                        except WebSocketDisconnect:
                            pass
                    loop = asyncio.get_event_loop()
                    history_msgs = await loop.run_in_executor(None, get_recent_messages, session_id, 30)
                    history_block = "\n".join(
                        f"{'用户' if m['role'] == 'user' else 'assistant'}: {m['content'][:500]}"
                        for m in history_msgs
                    )
                    injected_prompt = (
                        (system_prompt or "")
                        + ("\n\n# 历史对话（会话已重置，以下为上下文恢复）\n" + history_block if history_block else "")
                    ) or None
                    log.info("session_expired: injecting %d msgs history:\n%s", len(history_msgs), history_block[:1000])
                    return await run_claude(
                        message, None, ws,
                        system_prompt=injected_prompt,
                        model=model,
                        effort=effort,
                        silent=silent,
                        _retry=True,
                    )

                log.error("claude exited %d: %s", proc.returncode, err[:300])
                try:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"claude exited with code {proc.returncode}",
                        "detail": err[:500],
                    }, ensure_ascii=False))
                except WebSocketDisconnect:
                    pass

            return new_session_id, full_text, result_meta
        finally:
            # 用户可见轮次结束(正常/停止/异常)一律注销登记,避免残留导致下一轮误判停止或误杀
            if not silent:
                inflight_procs.pop(ws, None)
                stopped_conns.discard(ws)

    # -----------------------------------------------------------------------
    # WebSocket — Claude Code 会话
    # -----------------------------------------------------------------------

    session_archive_locks: dict[str, asyncio.Lock] = {}  # session_id -> lock，串行化存档防止竞态
    session_last_voice: dict[str, bool] = {}  # session_id -> 上一条消息是否为语音模式

    def _get_archive_lock(session_id: str) -> asyncio.Lock:
        lock = session_archive_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            session_archive_locks[session_id] = lock
        return lock

    async def _try_archive(session_id: str, ws: WebSocket | None = None,
                           model: str | None = None, effort: str | None = None,
                           force: bool = False):
        """存档新消息。Claude 写记忆正文（对话内注入）→ 记忆服务解析落库。
        游标持久化在 sessions.last_archived_id（重启不丢），
        per-session 锁串行化避免并发。
        条数门槛/JSON 解析/worthy 判断/task-state 提取全部交给记忆服务（契约 v2），
        核心只管编排：要不要发起这次存档尝试、把 Claude 原始输出整段转发过去。"""
        lock = _get_archive_lock(session_id)
        async with lock:
            try:
                after_id = get_last_archived_id(session_id)
                msgs = get_recent_messages(session_id, limit=30, after_id=after_id)

                for plugin in plugin_instances:
                    _call_hook(plugin, "on_archive", session_id, msgs, force)

                archive_prompt = await asyncio.to_thread(memory.build_archive_prompt, msgs, force)
                if not archive_prompt:
                    if force:
                        log.info("forge: no new messages to archive (msgs=%d)", len(msgs))
                    return

                # Claude 在当前对话内写记忆（方案 A）
                _, claude_raw, _ = await run_claude(
                    archive_prompt, session_id, ws,
                    model=model, effort=effort, silent=True,
                )

                if msgs:
                    set_last_archived_id(session_id, msgs[-1]["id"])

                if not claude_raw:
                    log.warning("auto_archive: Claude returned empty")
                    return

                archive_result = await asyncio.to_thread(memory.finalize_archive, claude_raw)

                if archive_result and archive_result.get("stored", 0) > 0:
                    log.info("auto_archive: saved [%s] for session %s",
                             archive_result.get("klass"), session_id)
                    if ws is not None:
                        try:
                            await ws.send_text(json.dumps({
                                "type": "status",
                                "message": "auto-archive: 1 new memory saved",
                            }, ensure_ascii=False))
                        except Exception:
                            pass
            except Exception as e:
                log.error("auto_archive error: %s", e)

    async def _auto_archive_loop(ws: WebSocket, get_session_id, get_model, get_effort):
        """Background task: auto-archive every ARCHIVE_INTERVAL seconds while session is alive."""
        await asyncio.sleep(archive_interval)
        while True:
            sid = get_session_id()
            if sid:
                await _try_archive(sid, ws, model=get_model(), effort=get_effort())
            await asyncio.sleep(archive_interval)

    @app.websocket("/ws")
    async def ws_claude(ws: WebSocket):
        await ws.accept()
        session_id = None
        system_prompt = None
        session_resumed = False  # True 表示恢复已有会话，跳过 L0+L1 重建

        # 权限透传：为本连接注册一个 token（注入 MCP 服务 env，回调时反查连接）。
        # 仅开关打开时注册；关闭时 token_by_ws 缺失 → run_claude 不追加 CLI 参数，行为如现状。
        if permission_passthrough:
            permission_broker.register(uuid.uuid4().hex, ws)
        await ws.send_text(json.dumps({
            "type": "hello",
            "message": "bridge connected",
            "server_time": now_iso(),
        }, ensure_ascii=False))

        model = None
        effort = None

        archive_task = asyncio.create_task(
            _auto_archive_loop(ws, lambda: session_id, lambda: model, lambda: effort)
        )

        # 独立读协程:主循环在 run_claude 流式期间阻塞在 await,自己收不到 stop。
        # 由它专职收帧——stop 帧即时终止在途子进程,其余帧原样入队交给主循环顺序消费,
        # 保持 switch_session/forge/普通消息的既有处理顺序不变。断连时投哨兵唤醒主循环退出。
        msg_queue: asyncio.Queue = asyncio.Queue()

        async def _reader():
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        peek = json.loads(raw)
                    except (json.JSONDecodeError, AttributeError):
                        await msg_queue.put(raw)
                        continue
                    if isinstance(peek, dict) and peek.get("type") == "stop":
                        await _stop_inflight(ws)  # 停止当前用户可见轮次;无在途轮则忽略
                        continue
                    # 授权决策回帧:主循环此刻正阻塞在 run_claude 的 await(CC 等着这个决策),
                    # 收不到帧,必须在 reader 里就地解掉对应 Future(与 stop 同理)。
                    if isinstance(peek, dict) and peek.get("type") == "permission_response":
                        permission_broker.resolve(
                            peek.get("request_id"),
                            peek.get("allow") is True,
                            peek.get("message") or "",
                        )
                        continue
                    await msg_queue.put(raw)
            except WebSocketDisconnect:
                await msg_queue.put(None)  # 哨兵:通知主循环连接已断

        reader_task = asyncio.create_task(_reader())

        try:
            while True:
                raw = await msg_queue.get()
                if raw is None:  # 断连哨兵
                    break

                voice_mode = False
                mode = "chat"
                attachments: list = []
                try:
                    payload = json.loads(raw)
                    text = payload.get("text", "").strip()
                    if "model" in payload:
                        model = payload["model"]
                    if "effort" in payload:
                        effort = payload["effort"]
                    voice_mode = payload.get("voice_mode", False)
                    mode = payload.get("mode", "chat")
                    attachments = payload.get("attachments") or []
                    # 切换会话
                    if "switch_session" in payload:
                        new_sid = payload["switch_session"]
                        if new_sid:
                            session_id = new_sid
                            session_resumed = True   # 恢复已有会话，跳过 L0+L1 重建
                            system_prompt = None
                            await ws.send_text(json.dumps({
                                "type": "session_switched",
                                "session_id": new_sid,
                            }, ensure_ascii=False))
                        else:
                            # 新建对话
                            session_id = None
                            system_prompt = None
                            session_resumed = False
                            await ws.send_text(json.dumps({
                                "type": "session_switched",
                                "session_id": None,
                            }, ensure_ascii=False))
                        continue
                    # 主动换窗：先存档当前会话（跳过条数门槛），再重置状态
                    if payload.get("forge"):
                        if session_id:
                            await _try_archive(session_id, ws, model=model, effort=effort, force=True)
                        session_id = None
                        system_prompt = None
                        session_resumed = False
                        await ws.send_text(json.dumps({
                            "type": "forged",
                            "message": "已换窗，记忆已存档",
                        }, ensure_ascii=False))
                        continue
                except (json.JSONDecodeError, AttributeError):
                    text = raw.strip()

                # 只放行落在 upload_dir 内的真实附件；纯附件（无文字）也可发，
                # 但文字与附件都空则跳过
                valid_attachments = _validate_attachment_paths(attachments)
                if not text and not valid_attachments:
                    continue

                loop = asyncio.get_event_loop()

                log.info("mode=%s session=%s", mode, session_id or "new")

                # 首条消息：构建 L0+L1 会话上下文（system prompt），走 on_user_message 钩子链路
                if session_id is None and not session_resumed and system_prompt is None:
                    await ws.send_text(json.dumps({
                        "type": "status",
                        "message": "loading memory layers...",
                    }, ensure_ascii=False))
                    system_prompt = await _run_on_user_message_hooks(loop, session_id, text, True)
                    if system_prompt:
                        await ws.send_text(json.dumps({
                            "type": "memory_recall",
                            "context": f"【context injected】({len(system_prompt)} chars)",
                        }, ensure_ascii=False))

                    if mode == "chat" and chat_mode_hint:
                        system_prompt = (system_prompt or "") + chat_mode_hint

                # 时间注入
                local_now = datetime.now()
                weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                time_prefix = "[当前时间: {}]\n".format(
                    local_now.strftime("%Y-%m-%d ") + weekdays[local_now.weekday()] + local_now.strftime(" %H:%M")
                )

                if voice_mode and voice_inline_hint:
                    claude_text = voice_inline_hint + time_prefix + text
                elif session_id and session_last_voice.get(session_id) and voice_exit_hint:
                    # 上一轮是语音模式：提醒模型停止延续情感标签风格
                    claude_text = voice_exit_hint + time_prefix + text
                else:
                    claude_text = time_prefix + text

                # L2：每条消息做情节记忆检索，走 on_user_message 钩子链路
                recall_ctx = await _run_on_user_message_hooks(loop, session_id, text, False)

                if recall_ctx:
                    await ws.send_text(json.dumps({
                        "type": "memory_recall",
                        "context": recall_ctx,
                    }, ensure_ascii=False))
                    enhanced = recall_ctx + "\n" + claude_text
                else:
                    enhanced = claude_text

                # 附件注入：把校验过的路径以固定格式附进消息，CC 用自带 Read 工具查看
                attach_note = _build_attachment_note(valid_attachments)
                if attach_note:
                    enhanced = enhanced + attach_note

                await ws.send_text(json.dumps({
                    "type": "status",
                    "message": "thinking...",
                }, ensure_ascii=False))

                new_session_id, assistant_text, assistant_meta = await run_claude(
                    enhanced,
                    session_id,
                    ws,
                    system_prompt=system_prompt if session_id is None else None,
                    model=model,
                    effort=effort,
                )

                effective_sid = new_session_id or session_id

                # 保存消息（user 先，assistant 后，保证顺序正确）
                if effective_sid:
                    user_meta = {"voice_mode": True} if voice_mode else None
                    if valid_attachments:
                        user_meta = user_meta or {}
                        # 只存文件名（不含 upload_dir 路径），供历史回看识别带了哪些附件
                        user_meta["attachments"] = [Path(p).name for p in valid_attachments]
                    # 纯附件无文字：存占位符，避免历史里出现空气泡
                    user_text_for_db = text if text else "[附件]"
                    save_message(effective_sid, "user", user_text_for_db, user_meta)
                    if assistant_text:
                        assistant_meta = assistant_meta or {}
                        if recall_ctx:
                            assistant_meta["recall"] = recall_ctx
                        if voice_mode:
                            assistant_meta["voice_mode"] = True
                        save_message(effective_sid, "assistant", assistant_text, assistant_meta)
                    session_last_voice[effective_sid] = voice_mode

                session_id = new_session_id

        except WebSocketDisconnect:
            log.info("client disconnected (session=%s)", session_id)
        except Exception as e:
            log.error("ws connection error (session=%s): %s", session_id, e)
        finally:
            reader_task.cancel()
            # 连接断开时回收可能仍在跑的可见轮子进程(run_claude 的 send 未触发 disconnect 的窗口)
            proc = inflight_procs.pop(ws, None)
            if proc is not None and proc.returncode is None:
                proc.kill()
            stopped_conns.discard(ws)
            # 权限透传:挂起的授权请求全部默拒并清表,注销 token(完成标准 2:断连清队默拒)
            permission_broker.deny_all(ws)
            permission_broker.unregister(ws)
            archive_task.cancel()
            if session_id:
                await _try_archive(session_id, model=model, effort=effort)

    # -----------------------------------------------------------------------
    # Frontend（demo 前端或调用方传入的 static_dir）
    # -----------------------------------------------------------------------

    @app.get("/manifest.json")
    async def manifest():
        return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(static_dir / "sw.js", media_type="application/javascript")

    @app.get("/icon-{size}.png")
    async def icon(size: str):
        icon_path = static_dir / f"icon-{size}.png"
        if not icon_path.exists():
            raise HTTPException(status_code=404, detail="icon not found")
        return FileResponse(icon_path, media_type="image/png")

    @app.get("/fonts/{filename}")
    async def font_asset(filename: str):
        # 自托管字体(JetBrains Mono / Tabler 图标子集 woff2)。只放行 woff2,把解析后的
        # 路径限制在 fonts/ 目录内,挡路径穿越(../ 等)——与 /themes 同一套白名单式防护。
        if not filename.endswith(".woff2"):
            raise HTTPException(status_code=404, detail="font not found")
        fonts_root = (static_dir / "fonts").resolve()
        target = (fonts_root / filename).resolve()
        if fonts_root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="font not found")
        return FileResponse(target, media_type="font/woff2")

    @app.get("/themes/{name}/{filename}")
    async def theme_asset(name: str, filename: str):
        # 主题资源:themes/<名>/theme.css（CSS 变量覆盖）或可选 theme.js（文案包/启动模块）。
        # 只放行这两个固定文件名,并把解析后的路径限制在 themes/ 目录内,挡路径穿越（../ 等）。
        if filename not in ("theme.css", "theme.js"):
            raise HTTPException(status_code=404, detail="theme asset not found")
        themes_root = (static_dir / "themes").resolve()
        target = (themes_root / name / filename).resolve()
        if themes_root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="theme asset not found")
        media = "text/css" if filename.endswith(".css") else "application/javascript"
        return FileResponse(target, media_type=media)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        p = static_dir / "index.html"
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>index.html not found</h1>"

    return app
