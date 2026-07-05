# Pando

[![test](https://github.com/Eloise-Aspen/pando-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/Eloise-Aspen/pando-bridge/actions/workflows/test.yml)

**简体中文 | [English](#english)**

> Pando 得名于已知最大的白杨无性系群落——单一有机体在地下蔓延，地表长成整片森林。
> 一套根系，多个枝干：一个 hook 化的内核，多座桥。

---

<a id="简体中文"></a>

**Pando** 是一个**自托管的 Claude Code CLI 移动网关**。跑在你自己的机器上，从手机访问
（PWA、反向代理，或 Tailscale/Cloudflare 这类私有隧道），随时随地和 Claude Code 对话。
内核是一个薄薄的 FastAPI 应用，调用你本地装好的 `claude` 二进制，把结果经 WebSocket
流式吐回——应用里没有 API key，数据不出你的机器。

内核自带**零记忆逻辑**。记忆是可选、可插拔的外部服务（见 [4 端点契约](#记忆契约)）；
插件通过[文档化的钩子](#插件钩子-api)扩展行为。开箱即用、不配记忆服务时，Pando 就是一个
干净的 Claude Code 远程终端。

---

## 为什么用 Pando？（vs. 官方远程访问）

Anthropic 提供了第一方的 Web/移动端 Claude Code 访问。Pando 不想取代它——它是给那些想
**掌控整条栈**的人准备的。2026 年初，通过 OAuth 骑在 Claude *订阅*上的第三方工具被切断；
Pando 完全绕开这点，直接驱动你自己已安装、已认证的 `claude` CLI。没有任何一方经手你的凭证。

| | Pando | 第一方远程 |
|---|---|---|
| **托管** | 自托管在你的机器上 | Anthropic 托管 |
| **数据** | 聊天记录在你自己的 SQLite；记忆在你自己的服务里 | 厂商管理 |
| **记忆** | 可插拔——任何引擎接入 4 端点 HTTP 契约即可 | 固定 |
| **成本模型** | 复用你现有的 Claude Code CLI / 订阅 | 按产品条款 |
| **可扩展性** | 注入、路由、会话源的插件钩子 | 封闭 |

如果你只想要官方体验，用官方产品就好。如果你想自托管、掌控自己的数据、接入自己的
记忆——这就是它的用途。

---

## 快速开始

纯终端，不接记忆服务，约 5 分钟。你需要 **Python 3.10+** 和已安装并认证的
**Claude Code CLI**（`claude` 在 `PATH` 里）。

动手前先确认 CLI 真的能用：

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

写一个 `run.py`（`CLAUDE_CWD` 必须是一个已经存在的目录，建议指向你想让 Claude 工作的
项目目录；不配 `MEMORY_SERVICE_URL` 就是纯终端模式，默认 `NullMemoryProvider` 什么记忆
都不做）：

```python
import os

import uvicorn
from pando import create_app

app = create_app({
    "CLAUDE_EXE": "claude",                  # 或 CLI 的绝对路径
    "CLAUDE_CWD": "/path/to/your/project",   # 必须已存在——你想让 Claude 工作的目录
    "DATA_DIR": "./data",                    # chat.db 存这里
    # 省略 MEMORY_SERVICE_URL -> NullMemoryProvider = 纯终端
})

if __name__ == "__main__":
    # 0.0.0.0 绑定所有网卡,好让局域网设备(比如你的手机)能连上——
    # 配合下方"手机接入"里的安全提示一起看
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BRIDGE_PORT", 8765)))
```

```bash
python run.py
```

> **Windows 注记**——以上步骤在 PowerShell 里照抄即可（Windows 11 + Python 3.10
> 实测通过）。两个平台差异：`CLAUDE_CWD` 用正斜杠写（`"C:/Users/you/project"`，
> 反斜杠在 Python 字符串里是转义符）；环境变量用 PowerShell 写法设置：
> `$env:BRIDGE_PORT = "8899"; python run.py`。

浏览器打开 <http://127.0.0.1:8765> 即是自带的 PWA 前端，接到 `/ws` WebSocket。发一条
消息，Claude Code 就会回，流式吐出 thinking/text/tool-use。这就是全部循环。默认端口
`8765`，被占用时设置环境变量 `BRIDGE_PORT`——Linux/macOS 写 `BRIDGE_PORT=8899 python run.py`，
PowerShell 写 `$env:BRIDGE_PORT = "8899"; python run.py`——或改上面 `run.py` 示例里的
端口即可，不用改包源码。想在手机上用——这才是重点——见 [📱 手机接入](#-手机接入)。

见 [`.env.example`](.env.example) 了解每一个配置开关。

---

## 📱 手机接入

Pando 是个*移动*网关——这一节带你从"能在 `127.0.0.1` 上跑"走到"作为 PWA 装在手机上"。
推荐路径是 **Tailscale serve**：几条命令，就能拿到一个只有你自己私有 Tailscale 网络
（你的 *tailnet*）内设备可达的有效 HTTPS 地址。

### 推荐：Tailscale serve

1. **两端安装 Tailscale**：跑 Pando 的机器（[下载](https://tailscale.com/download)）
   和你的手机（App Store / 应用商店）。
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
   里的能力——PWA 安装、通知、麦克风——全部可用。若 `tailscale serve status`
   正常但手机超时（`ERR_CONNECTION_TIMED_OUT`），检查机器端 Tailscale 是否开了
   拦截入站连接：`tailscale set --shields-up=false`（客户端界面里的
   *Block incoming connections* 开关）。

### 备选：Cloudflare Tunnel

如果用不了 Tailscale，[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)（`cloudflared`）
也能把 `localhost:8765` 映射到 Cloudflare 后面的域名并带有效 HTTPS，但该地址
**公网可达**，必须前置访问策略（如 [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)），
此处不展开，照官方文档走。

### 纯局域网 HTTP 的边界

`run.py` 绑 `0.0.0.0` 后，同一局域网的设备可以直接打开 `http://<局域网IP>:8765`——
**聊天没问题**，但浏览器把 PWA 安装、通知、麦克风这类能力限制在 HTTPS 安全上下文里，
纯 HTTP 用不了。临时测试够用，正经用走 Tailscale serve。

### ⚠️ 安全

Pando **没有任何认证**——能连上端口的人就能驱动你机器上 `CLAUDE_CWD` 里的 `claude`
CLI。只应通过 tailnet 或可信内网暴露，**切勿**端口转发或直接映射到公网；用 Cloudflare
Tunnel 必须前置访问策略。

---

## 记忆契约

内核从不实现记忆——它只跟一个满足 `MemoryProvider` 协议（`pando/memory_provider.py`）的
提供方对话。内置两个：

- **`NullMemoryProvider`**（默认）：每个方法都是空操作。Pando 就是个纯终端。
- **`HttpMemoryProvider`**：设置 `MEMORY_SERVICE_URL`，四个方法就变成对你外部记忆服务的
  HTTP 调用。任何网络/解析错误**静默降级**（返回 `""` / `None` / `{"stored": 0}`），
  绝不阻塞聊天。

你的记忆服务可以用任何语言/技术栈写，只需回应这四个端点（v2 契约）：

```
POST {url}/session_context  {}                                    -> {"context": str}
POST {url}/recall           {"query": str}                        -> {"context": str}
POST {url}/archive_prompt   {"messages": [{role, content}, ...],
                             "force": bool}                        -> {"prompt": str | null}
POST {url}/archive          {"raw": str}                          -> {"stored": int, ...}
```

- **`/session_context`** —— 长期上下文（L0+L1），在新会话第一条消息时作为 system prompt
  注入。每个新会话调用一次。
- **`/recall`** —— 针对单条用户消息的情景回忆（L2），前置到消息前。每条用户消息都调用。
  无命中返回 `""`。
- **`/archive_prompt`** —— 触发归档时，决定*是否*归档、以及模型该写*什么*。返回一个
  prompt 字符串，或 `null` 跳过。内核在**活着的** Claude 会话里跑这个 prompt（复用上下文，
  省一次 LLM 调用）。
- **`/archive`** —— 接收模型跑归档 prompt 的原始输出；你来解析并持久化。内核不解释这段文本。

设计原则：**归档属于记忆引擎；内核只出借活着的会话。**内核不持有 prompt、不解析、不存储。

一个最小参考服务（内存态，无向量，无 LLM）在
[`examples/memory_stub.py`](examples/memory_stub.py)——通读它就能端到端理解契约：

```bash
python examples/memory_stub.py                        # 127.0.0.1:8780
# 然后用 MEMORY_SERVICE_URL=http://127.0.0.1:8780 把 bridge 指过去
```

---

## 插件钩子 API

插件在 `PLUGINS` 配置里声明为一串 import 路径，经 `importlib` 加载。每个插件如果 `__init__`
接受 `MemoryProvider` 就用内核的实例构造，否则无参构造。导入或构造失败的插件会被**跳过**——
绝不阻塞其他插件或应用启动。

所有钩子都是可选的（只定义你需要的）。异常被捕获、记录、吞掉——抛异常的钩子绝不冒泡进
聊天。如果某插件的 `on_startup` 抛异常，该插件在**后续所有**钩子中都被禁用。

| 钩子 | 签名 | 时机 |
|---|---|---|
| `on_startup` | `(app, config_dict) -> None` | 启动时一次。失败则禁用该插件。 |
| `register_routes` | `(app) -> None` | 启动时，添加 FastAPI 路由。 |
| `register_session_source` | `(registry) -> None` | 启动时（会话源注册；形态为未来 spec 预留）。 |
| `on_user_message` | `(session_id, text, is_new_session) -> str` | 每条用户消息。返回文本被注入（`is_new_session` 时作 system prompt，否则作为 recall 前置到消息）。在线程池里跑。 |
| `on_archive` | `(session_id, messages, force) -> None` | 归档 prompt 构建前。 |

内置的 [`MemoryPlugin`](pando/plugins/memory.py) 是个完整示例：它的 `on_user_message` 做
上下文/回忆注入，`register_routes` 挂一个 `/memory-admin/*` 透传代理到记忆服务的管理 API。

---

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 许可

[MIT](LICENSE)。

<br>

---
---

<a id="english"></a>

# Pando (English)

**[简体中文](#简体中文) | English**

> **Pando** is named after the largest known aspen clonal colony — a single organism
> that spreads underground and surfaces as a whole forest. The name is the Latin
> *pando*, "I spread." One root system, many trunks: one hookified core, many bridges.

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
   If the phone times out (`ERR_CONNECTION_TIMED_OUT`) even though
   `tailscale serve status` looks right, check that the machine's Tailscale
   client isn't blocking inbound connections: `tailscale set --shields-up=false`
   (the *Block incoming connections* toggle in the client UI).
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
