#!/usr/bin/env python3
"""Pando 权限透传 MCP server —— 由 CC 通过 `--mcp-config` + `--permission-prompt-tool` 拉起。

背景（Task 1 spike 实测，CC 2.1.202）：`--print` 模式无终端弹权限确认，CC 需要门控
工具时默拒。官方机制是把一个 stdio MCP server 的某个 tool 指定为 permission-prompt-tool，
CC 每次要授权就调用它，tool 返回 `{"behavior":"allow"|"deny", ...}` 决定放行/拦截。

本 server 就是那个 tool 的宿主。它把授权请求 POST 到 bridge 的回调端点、阻塞等 bridge
返回用户决策，再翻译成 CC 要的形状返回。

**零依赖（仅标准库）**：本进程由 claude.exe 独立拉起，不保证 pando 已安装在它的解释器里，
因此绝不 import pando，只用 stdlib（json / os / sys / urllib）。这也满足 CONSTRAINTS 的
「外部依赖无降级」红线。

**默认拒绝哲学**：连不上 bridge、超时、返回不合法、缺配置——任何异常一律 deny，绝不放行。

协议 = MCP over stdio = 换行分隔的 JSON-RPC 2.0。握手顺序（实测）：
    initialize → notifications/initialized → tools/list → tools/call

env（由 bridge 在 mcp-config 的 env 段注入）：
    PANDO_PERMISSION_CALLBACK_URL   bridge 回调端点，必需；缺失则一切请求默拒
    PANDO_PERMISSION_TOKEN          关联发起本轮的 WS 连接的令牌，随 POST 带上供 bridge 路由
    PANDO_PERMISSION_TOOL_NAME      暴露的 tool 名（默认 "approve"），须与 --permission-prompt-tool 对齐
    PANDO_PERMISSION_HTTP_TIMEOUT   HTTP 等待上限秒（默认 150，略大于 bridge 内部 120s 超时，
                                    让 bridge 的默拒先触发；本端超时是兜底，同样默拒）
"""
import json
import os
import sys
import urllib.request

# ---- 配置（进程启动即读一次，缺失不致命，落到默拒路径）-----------------------------
CALLBACK_URL = os.environ.get("PANDO_PERMISSION_CALLBACK_URL", "")
TOKEN = os.environ.get("PANDO_PERMISSION_TOKEN", "")
TOOL_NAME = os.environ.get("PANDO_PERMISSION_TOOL_NAME", "approve")
try:
    HTTP_TIMEOUT = float(os.environ.get("PANDO_PERMISSION_HTTP_TIMEOUT", "150"))
except ValueError:
    HTTP_TIMEOUT = 150.0

PROTOCOL_FALLBACK = "2025-11-25"  # 实测 CC 2.1.202 用的版本；实际以 client 请求为准


def _send(msg: dict) -> None:
    """写一帧 JSON-RPC 到 stdout（换行分隔），立即 flush。"""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _deny(message: str) -> dict:
    """构造 CC 要的 deny 载荷。"""
    return {"behavior": "deny", "message": message}


def _allow(updated_input: dict) -> dict:
    """构造 CC 要的 allow 载荷；updatedInput 原样回传目标工具入参。"""
    return {"behavior": "allow", "updatedInput": updated_input}


def request_decision(tool_name: str, tool_input: dict, tool_use_id: str) -> dict:
    """把一次授权请求 POST 给 bridge，阻塞等决策，返回 CC 形状的 {behavior:...}。

    bridge 回调契约：
        请求体 {token, tool_name, input, tool_use_id}
        响应体 {"decision": "allow"|"deny", "message"?: str}
    任何偏差（网络错误、非 200、JSON 不合法、decision 非法）都翻译成 deny——默认拒绝。
    """
    if not CALLBACK_URL:
        return _deny("permission bridge not configured (no callback url)")

    body = json.dumps({
        "token": TOKEN,
        "tool_name": tool_name,
        "input": tool_input,
        "tool_use_id": tool_use_id,
    }).encode("utf-8")
    req = urllib.request.Request(
        CALLBACK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
    except OSError:
        # 连不上 / 超时 / 连接中途被重置（bridge 挂了、本端等超了、读一半断了）——默拒。
        # URLError 是 OSError 子类；ConnectionResetError/socket.timeout 亦然，一网打尽。
        return _deny("permission request failed to reach bridge")
    except (ValueError, json.JSONDecodeError):
        return _deny("permission bridge returned malformed response")

    decision = data.get("decision")
    if decision == "allow":
        return _allow(tool_input)
    if decision == "deny":
        return _deny(data.get("message") or "denied by user")
    # 未知 decision 值——默拒
    return _deny("permission bridge returned unknown decision")


def handle(req: dict):
    """分发一条 JSON-RPC 消息。返回要回写的响应 dict，或 None（通知类不回）。"""
    method = req.get("method")
    mid = req.get("id")

    if method == "initialize":
        proto = req.get("params", {}).get("protocolVersion", PROTOCOL_FALLBACK)
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pando-permission", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None  # 通知无 id、不回
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "tools": [{
                    "name": TOOL_NAME,
                    "description": "Ask the human (via the phone bridge) whether to allow a tool call.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "input": {"type": "object"},
                            "tool_use_id": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                }],
            },
        }
    if method == "tools/call":
        params = req.get("params", {})
        args = params.get("arguments", {})
        payload = request_decision(
            args.get("tool_name", ""),
            args.get("input", {}),
            args.get("tool_use_id", ""),
        )
        # CC 约定：permission-prompt-tool 的返回值是 content[0].text 里的 JSON 字符串
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    # 其它带 id 的请求：回空结果避免 CC 卡住；通知（无 id）忽略
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return None


def main() -> None:
    """主循环：逐行读 stdin，分发，回写。stdin EOF 即退出。"""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
