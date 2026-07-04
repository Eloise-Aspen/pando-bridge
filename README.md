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
    # 0.0.0.0 binds all interfaces so LAN devices (e.g. your phone) can reach it —
    # pair this with the security note in "Reach it from your phone" below
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BRIDGE_PORT", 8765)))
```

```bash
python run.py
```

> **Windows note** — everything above works as-is in PowerShell (verified on
> Windows 11, Python 3.10). Two platform quirks: write `CLAUDE_CWD` with forward
> slashes (`"C:/Users/you/project"` — backslashes are escape characters in Python
> strings), and set environment variables the PowerShell way:
> `$env:BRIDGE_PORT = "8899"; python run.py`.

Open <http://127.0.0.1:8765> — you get the demo PWA frontend wired to the `/ws`
WebSocket. Send a message and Claude Code answers, streaming thinking/text/tool-use
as it goes. That's the whole loop. Default port is `8765`; if it's already taken,
set `BRIDGE_PORT` in the environment — `BRIDGE_PORT=8899 python run.py` on
Linux/macOS, `$env:BRIDGE_PORT = "8899"; python run.py` in PowerShell — or edit the
`run.py` snippet above instead of touching the package source. To use it from your
phone — which is the whole point — see
[📱 Reach it from your phone](#-reach-it-from-your-phone).

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
设置环境变量 `BRIDGE_PORT`——Linux/macOS 写 `BRIDGE_PORT=8899 python run.py`，
PowerShell 写 `$env:BRIDGE_PORT = "8899"; python run.py`——或改上面 `run.py`
示例里的端口即可，不用改包源码。想在手机上用，见下方
[📱 手机接入（中文）](#-手机接入中文)。

> **Windows 注记**——以上步骤在 PowerShell 里照抄即可（Windows 11 + Python 3.10
> 实测通过）。两个平台差异：`CLAUDE_CWD` 用正斜杠写（`"C:/Users/you/project"`，
> 反斜杠在 Python 字符串里是转义符）；环境变量用上面的 PowerShell 写法设置。

---

## 📱 Reach it from your phone

Pando is a *mobile* gateway — this section takes you from "works on `127.0.0.1`"
to "installed as a PWA on your phone". The recommended path is **Tailscale serve**:
a couple of commands, and you get a valid HTTPS URL reachable only from devices
in your own private Tailscale network (your *tailnet*).

### Recommended: Tailscale serve

1. **Install Tailscale on both ends** — the machine running Pando
   ([download](https://tailscale.com/download)) and your phone
   (App Store / Google Play).

2. **Log in on both ends** with the same account, so they join the same tailnet.
   On a Linux machine:

   ```bash
   sudo tailscale up
   ```

   On Windows or macOS there's no `sudo` step — sign in through the Tailscale
   app (system tray / menu bar), or run `tailscale login` to get a browser
   sign-in link. On the phone, sign in through the Tailscale app.

3. **Serve Pando over HTTPS** — on the machine running Pando:

   ```bash
   tailscale serve --bg 8765
   ```

   Tailscale proxies `https://<machine-name>.<tailnet-name>.ts.net` to
   `localhost:8765` and provisions a **valid HTTPS certificate automatically**
   (on first use it walks you through enabling HTTPS for your tailnet).
   `--bg` keeps it serving in the background; check the exact URL any time with
   `tailscale serve status`. If the command complains about permissions (Linux),
   either prefix `sudo` or run `sudo tailscale set --operator=$USER` once — on
   Windows it works from a regular PowerShell prompt, no elevation needed.

4. **Open it on your phone**: visit `https://<machine-name>.<tailnet-name>.ts.net`,
   send a message, then use the browser's *Add to Home Screen* to install the PWA.
   Because this is real HTTPS, everything browsers gate behind a secure context —
   PWA install, notifications, microphone — is available.

### Alternative: Cloudflare Tunnel

If Tailscale isn't an option, [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
(`cloudflared`) can map `localhost:8765` to a hostname behind Cloudflare with valid
HTTPS. Note the resulting URL is **publicly reachable**, so an access policy in
front (e.g. [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/))
is mandatory — see the security note below. Follow the official docs; not expanded here.

### Plain LAN HTTP — what works, what doesn't

With the quickstart `run.py` binding `0.0.0.0`, devices on the same LAN can open
`http://<LAN-IP>:8765` directly. **Chat works fine.** But browsers restrict
"powerful features" to HTTPS secure contexts, so over plain HTTP there is **no PWA
install, no notifications, no microphone**. Good enough for a quick test from the
couch; use Tailscale serve for the real thing.

### ⚠️ Security

Pando ships **no authentication** — anyone who can reach the port can drive the
`claude` CLI inside your `CLAUDE_CWD`. Expose it only through your tailnet or a
trusted private network. **Never** port-forward it or map it directly to the public
internet; if you use Cloudflare Tunnel, put an access policy in front.

### 📱 手机接入（中文）

推荐路径 **Tailscale serve**——几条命令拿到只有自己 tailnet 内设备可达的有效 HTTPS 地址：

1. **两端安装 Tailscale**：跑 Pando 的机器（[下载](https://tailscale.com/download)）
   和手机（App Store / 应用商店）。
2. **两端登录同一账号**，加入同一个 tailnet。Linux 机器执行 `sudo tailscale up`；
   Windows/macOS 没有 `sudo` 这一步，直接在 Tailscale 应用（系统托盘/菜单栏）里
   登录，或跑 `tailscale login` 拿浏览器登录链接。手机在 Tailscale App 里登录。
3. **把 Pando 发布为 HTTPS**——在跑 Pando 的机器上：

   ```bash
   tailscale serve --bg 8765
   ```

   Tailscale 会把 `https://<机器名>.<tailnet名>.ts.net` 反代到 `localhost:8765`，
   并**自动签发有效的 HTTPS 证书**（首次使用会引导你为 tailnet 开启 HTTPS）。
   `--bg` 表示后台常驻，随时用 `tailscale serve status` 查看确切地址；
   Linux 上提示权限不足时加 `sudo`，或先跑一次
   `sudo tailscale set --operator=$USER`；Windows 普通 PowerShell 直接能跑，
   无需管理员权限。
4. **手机打开** `https://<机器名>.<tailnet名>.ts.net`，发一条消息确认能聊，
   再用浏览器的"添加到主屏幕"安装 PWA。因为是真 HTTPS，浏览器限制在安全上下文
   里的能力——PWA 安装、通知、麦克风——全部可用。

**备选：Cloudflare Tunnel。**
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)（`cloudflared`）
也能把 `localhost:8765` 映射到 Cloudflare 后面的域名并带有效 HTTPS，但该地址
**公网可达**，必须前置访问策略（如 [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)），
此处不展开，照官方文档走。

**纯局域网 HTTP 的边界。** `run.py` 绑 `0.0.0.0` 后，同一局域网的设备可以直接打开
`http://<局域网IP>:8765`——**聊天没问题**，但浏览器把 PWA 安装、通知、麦克风这类
能力限制在 HTTPS 安全上下文里，纯 HTTP 用不了。临时测试够用，正经用走 Tailscale serve。

**⚠️ 安全提示。** Pando **没有任何认证**——能连上端口的人就能驱动你机器上
`CLAUDE_CWD` 里的 `claude` CLI。只应通过 tailnet 或可信内网暴露，**切勿**端口转发
或直接映射到公网；用 Cloudflare Tunnel 必须前置访问策略。

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
