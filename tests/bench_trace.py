"""进程内确定性 trace 基准；显式运行，不属于 pytest 的 test_*.py 收集。

复跑：python -m tests.bench_trace --output <results.json>
仅使用临时目录、模拟 CLI 和假 WebSocket，不监听/请求端口。
"""
import argparse
import asyncio
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from starlette.websockets import WebSocketDisconnect

import pando.server as server
from pando import create_app
from pando.trace import TraceStore
from tests.test_carryover_persona_prompt import FakeCLI, _FakeStdout, _config, _enc


class InProcessSocket:
    # 时间戳与真正收帧/发帧位置相邻；不把网络或测试客户端线程调度混进基准。
    def __init__(self):
        self.received_at = None
        self.spawned_at = None
        self.text_event_at = None
        self.text_frame_at = None
        self.finished = asyncio.Event()
        self.frames = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self.received_at is None:
            payload = json.dumps({"text": "neutral benchmark message", "archive": False})
            self.received_at = time.perf_counter_ns()
            return payload
        await self.finished.wait()
        raise WebSocketDisconnect()

    async def send_text(self, raw):
        sent_at = time.perf_counter_ns()
        frame = json.loads(raw)
        self.frames.append(frame)
        if frame.get("type") == "text" and self.text_frame_at is None:
            self.text_frame_at = sent_at
        if frame.get("type") in {"result", "error"}:
            self.finished.set()


class TimedStdout(_FakeStdout):
    def __init__(self, lines, socket):
        super().__init__(lines)
        self.socket = socket

    async def __anext__(self):
        line = await super().__anext__()
        if json.loads(line).get("type") == "stream_event" and self.socket.text_event_at is None:
            self.socket.text_event_at = time.perf_counter_ns()
        return line


def summary(samples):
    # p90 固定使用 nearest-rank：50 个样本按升序排列的第 45 个。
    return {"median_ms": statistics.median(samples),
            "p90_ms": sorted(samples)[math.ceil(len(samples) * .9) - 1],
            "samples_ms": samples}


async def measure(root):
    apps, endpoints, fakes = {}, {}, {}
    active = [None]
    async def spawn(*args, **kwargs):
        socket, enabled = active[0]
        socket.spawned_at = time.perf_counter_ns()
        proc = await fakes[enabled](*args, **kwargs)
        lines = list(proc.stdout._lines)
        # 首 delta 超过既有 40 字符节流门槛，测真实 first-text 分支而不等 100ms 定时。
        lines.insert(1, _enc({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {
                "type": "text_delta", "text": "neutral streaming benchmark text " * 2}}}))
        proc.stdout = TimedStdout(lines, socket)
        return proc

    async def one(enabled):
        socket = InProcessSocket()
        active[0] = (socket, enabled)
        await asyncio.wait_for(endpoints[enabled](socket), timeout=10)
        assert socket.spawned_at is not None and socket.text_frame_at is not None
        assert any(f.get("type") == "result" for f in socket.frames)
        apps[enabled].state.trace.flush()
        return ((socket.spawned_at - socket.received_at) / 1e6,
                (socket.text_frame_at - socket.text_event_at) / 1e6)

    # 两种设置都启动真实 app 生命周期及现有中性分层钩子，只替换 CLI 与 LAN 探测。
    # 开关交替，每个样本用新连接/新会话，覆盖首轮系统提示、L0 和 L2 路径。
    with patch.object(server.asyncio, "create_subprocess_exec", spawn), \
            patch.object(server, "_detect_lan_ip", lambda: None):
        for enabled in (False, True):
            app_root = root / ("enabled" if enabled else "disabled")
            apps[enabled] = create_app(_config(app_root, TRACE_ENABLED=enabled,
                                             CHAT_MODE_HINT="neutral mode hint"))
            endpoints[enabled] = next(r.endpoint for r in apps[enabled].routes if r.path == "/ws")
            fakes[enabled] = FakeCLI(app_root / "projects")
        async with apps[False].router.lifespan_context(apps[False]), \
                apps[True].router.lifespan_context(apps[True]):
            warmup = {"disabled": await one(False), "enabled": await one(True)}
            values = {False: {"A": [], "B": []}, True: {"A": [], "B": []}}
            for _ in range(50):
                for enabled in (False, True):
                    a, b = await one(enabled)
                    values[enabled]["A"].append(a)
                    values[enabled]["B"].append(b)
            result = {name: {"disabled": summary(values[False][name]),
                             "enabled": summary(values[True][name])} for name in ("A", "B")}
            result["warmup_ms"] = {key: {"A": pair[0], "B": pair[1]} for key, pair in warmup.items()}
        for name in ("A", "B"):
            result[name]["difference_ms"] = (result[name]["enabled"]["median_ms"]
                                              - result[name]["disabled"]["median_ms"])
    return result


def background(root):
    store = TraceStore(root / "background.db")
    try:
        # 后台写入计时排除一次建表；warmup 数字也保留，随后只测这一批 300 条。
        begun = time.perf_counter_ns()
        store.start("neutral-background")
        store.flush()
        warmup_ms = (time.perf_counter_ns() - begun) / 1e6
        begun = time.perf_counter_ns()
        for seq in range(300):
            store.event("neutral-background", 1, seq, {"type": "stream_event", "text": "neutral"})
        store.flush()
        elapsed_ms = (time.perf_counter_ns() - begun) / 1e6
        assert len(store.detail("neutral-background")["events"]) == 300
        assert store.failures == 0
        return {"events": 300, "elapsed_ms": elapsed_ms, "initialization_ms": warmup_ms,
                "timing": "before first event submission through flush commit completion"}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pando-trace-bench-") as directory:
        root = Path(directory)
        result = asyncio.run(measure(root))
        result["background"] = background(root)
    result["method"] = {"samples_per_setting": 50, "order": "alternating disabled/enabled",
                        "warmups_per_setting": 1, "p90": "nearest rank",
                        "real_cli_called": False, "network_requests": False}
    result["passed"] = (abs(result["A"]["difference_ms"]) < 5
                        and abs(result["B"]["difference_ms"]) < 5
                        and result["background"]["elapsed_ms"] < 200)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
