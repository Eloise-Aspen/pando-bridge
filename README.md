# Pando

**Pando** is named after the largest known aspen clonal colony — a single organism
that spreads underground and surfaces as a whole forest. The name is the Latin
*pando*, "I spread." One root system, many trunks: one hookified core, many bridges.

Pando is a **self-hosted mobile gateway for the Claude Code CLI**. Run it on your
own machine, reach it from your phone (PWA, reverse proxy, or a private tunnel like
Tailscale/Cloudflare), and talk to Claude Code from anywhere. The core is a thin
FastAPI app that shells out to your locally-installed `claude` binary and streams
the result over WebSocket — no API keys in the app, no data leaving your box.

The core ships **zero memory logic**. Memory is an optional, pluggable external
service (see the [4-endpoint contract](#memory-contract)); plugins extend behavior
through [documented hooks](#plugin-hook-api). Out of the box, with no memory service
configured, Pando is simply a clean remote terminal for Claude Code.

---

## Why Pando? (vs. official remote access)

Anthropic ships first-party web/mobile access to Claude Code. Pando is not trying to
replace it — it exists for people who want to **own the whole stack**. In early 2026,
third-party tools riding on a Claude *subscription* via OAuth were shut off; Pando
sidesteps that entirely by driving your own already-installed, already-authenticated
`claude` CLI. Nothing brokers your credentials.

| | Pando | First-party remote |
|---|---|---|
| **Hosting** | Self-hosted on your machine | Anthropic-hosted |
| **Data** | Chat history in your own SQLite; memory in your own service | Vendor-managed |
| **Memory** | Pluggable — bring any engine behind a 4-endpoint HTTP contract | Fixed |
| **Cost model** | Uses your existing Claude Code CLI / subscription | Per product terms |
| **Extensibility** | Plugin hooks for injection, routes, session sources | Closed |

If you just want the official experience, use the official product. If you want to
self-host, own your data, and plug in your own memory — that's what this is.

---

## Quick Start

Pure terminal, no memory service, ~5 minutes. You need **Python 3.10+** and the
**Claude Code CLI** installed and authenticated (`claude` on your `PATH`).

Before anything else, check the CLI actually works:

```bash
claude --version
claude -p "hi"
```

If the CLI isn't authenticated, `claude -p "hi"` will hang, prompt for login, or
error out instead of printing a normal reply — run `claude` interactively once to
finish authentication before continuing.

```bash
git clone https://github.com/Eloise-Aspen/pando-bridge.git
cd pando-bridge
pip install -e .
```

Create `run.py`:

```python
import os

import uvicorn
from pando import create_app

app = create_app({
    "CLAUDE_EXE": "claude",                  # or an absolute path to the CLI
    "CLAUDE_CWD": "/path/to/your/project",   # must already exist — the directory you want Claude to work in
    "DATA_DIR": "./data",                    # chat.db lives here
    # MEMORY_SERVICE_URL omitted -> NullMemoryProvider = pure terminal
})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("BRIDGE_PORT", 8765)))
```

```bash
python run.py
```

Open <http://127.0.0.1:8765> — you get the demo PWA frontend wired to the `/ws`
WebSocket. Send a message and Claude Code answers, streaming thinking/text/tool-use
as it goes. That's the whole loop. Default port is `8765`; if it's already taken,
set `BRIDGE_PORT` in the environment (or edit the `run.py` snippet above) instead of
touching the package source. To expose it to your phone, put it behind a
reverse proxy or a private tunnel (TLS terminated there, or set `SSL_CERTFILE` /
`SSL_KEYFILE` for direct HTTPS).

See [`.env.example`](.env.example) for every configuration knob.

### 中文快速开始

面向自建党的最短路径：需要 **Python 3.10+** 和已登录的 **Claude Code CLI**（`claude`
在 `PATH` 里）。

开始之前先自检一下 CLI 是否真的能用：

```bash
claude --version
claude -p "hi"
```

如果 CLI 还没认证，`claude -p "hi"` 会卡住不返回、提示登录，或者直接报错，而不是正常
输出一句回复——先交互式跑一次 `claude` 完成登录认证，再继续下面的步骤。

```bash
git clone https://github.com/Eloise-Aspen/pando-bridge.git
cd pando-bridge
pip install -e .
```

按上面的 `run.py` 写一个启动脚本（`CLAUDE_CWD` 必须是一个已经存在的目录，建议指向你想
让 Claude 工作的项目目录；不配 `MEMORY_SERVICE_URL` 就是纯终端模式，默认
`NullMemoryProvider` 什么记忆都不做），然后：

```bash
python run.py
```

浏览器打开 <http://127.0.0.1:8765> 即是自带的 PWA 前端。默认端口 `8765`，被占用时
设置环境变量 `BRIDGE_PORT`（或改上面 `run.py` 示例里的端口）即可，不用改包源码。
想在手机上用，就套一层反向代理或私有隧道（Tailscale / Cloudflare Tunnel 都行），
TLS 在那层终结，或直接配 `SSL_CERTFILE` / `SSL_KEYFILE` 让 Pando 自己起 HTTPS。

---

## Memory Contract

The core never implements memory — it talks to a provider that satisfies the
`MemoryProvider` protocol (`pando/memory_provider.py`). Two providers ship in-box:

- **`NullMemoryProvider`** (default): every method is a no-op. Pando is a plain terminal.
- **`HttpMemoryProvider`**: set `MEMORY_SERVICE_URL` and the four methods become HTTP
  calls to your external memory service. Any network/parse error **degrades silently**
  (returns `""` / `None` / `{"stored": 0}`) and never blocks the chat.

Your memory service can be written in any language/stack; it only has to answer these
four endpoints (v2 contract):

```
POST {url}/session_context  {}                                    -> {"context": str}
POST {url}/recall           {"query": str}                        -> {"context": str}
POST {url}/archive_prompt   {"messages": [{role, content}, ...],
                             "force": bool}                        -> {"prompt": str | null}
POST {url}/archive          {"raw": str}                          -> {"stored": int, ...}
```

- **`/session_context`** — long-term context (L0+L1), injected as the system prompt on
  the first message of a new session. Called once per new session.
- **`/recall`** — episodic recall (L2) for a single user message, prepended to it.
  Called on every user message. Return `""` for no hit.
- **`/archive_prompt`** — on an archive trigger, decide *whether* to archive and *what*
  the model should write. Return a prompt string, or `null` to skip. The core runs that
  prompt inside the **live** Claude session (reusing context, saving an LLM call).
- **`/archive`** — receives the model's raw output from the archive prompt; you parse and
  persist it. The core does not interpret this text.

The design rule: **archiving belongs to the memory engine; the core only lends the live
session.** The core holds no prompts, no parsing, no storage.

A minimal reference service (in-memory, no vectors, no LLM) lives in
[`examples/memory_stub.py`](examples/memory_stub.py) — read it to understand the contract
end-to-end:

```bash
python examples/memory_stub.py                        # 127.0.0.1:8780
# then point the bridge at it via MEMORY_SERVICE_URL=http://127.0.0.1:8780
```

---

## Plugin Hook API

Plugins are declared as a list of import paths in the `PLUGINS` config and loaded via
`importlib`. Each plugin is constructed with the core's `MemoryProvider` instance if its
`__init__` accepts one, otherwise with no arguments. A plugin that fails to import or
construct is **skipped** — it never blocks other plugins or app startup.

All hooks are optional (define only what you need). Exceptions are caught, logged, and
swallowed — a hook that raises never bubbles into the chat. If a plugin's `on_startup`
raises, that plugin is disabled for **all** subsequent hooks.

| Hook | Signature | When |
|---|---|---|
| `on_startup` | `(app, config_dict) -> None` | Once at startup. Failure disables the plugin. |
| `register_routes` | `(app) -> None` | At startup, to add FastAPI routes. |
| `register_session_source` | `(registry) -> None` | At startup (session-source registration; shape reserved for a future spec). |
| `on_user_message` | `(session_id, text, is_new_session) -> str` | On every user message. Return text is injected (system prompt when `is_new_session`, otherwise recall prepended to the message). Runs in a thread pool. |
| `on_archive` | `(session_id, messages, force) -> None` | Before an archive prompt is built. |

The built-in [`MemoryPlugin`](pando/plugins/memory.py) is a worked example: its
`on_user_message` does context/recall injection, and its `register_routes` mounts a
`/memory-admin/*` pass-through proxy to the memory service's admin API.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE).
