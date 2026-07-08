"""Task 3：PermissionBroker 挂起队列/超时/断连/排队 的纯异步单测（完成标准 2、3）。

不经 WS/HTTP，直接驱动 broker 的状态机——传输已在 wiring 测试与真机 E2E 覆盖。
每个用例用 asyncio.run 起独立事件循环，避免依赖 pytest-asyncio 的全局配置。"""

import asyncio

from pando.server import PermissionBroker


class _FakeWS:
    """占位连接对象；broker 只把它当 key 用，不调用其方法。"""
    pass


def test_allow_roundtrip():
    async def scenario():
        broker = PermissionBroker(timeout=5)
        ws = _FakeWS()
        broker.register("tok", ws)
        captured = {}

        async def send(rid):
            captured["rid"] = rid  # 拿到 request_id 后模拟前端点「允许」

        # 并发：request 阻塞等决策，另一协程读到 rid 后 resolve(allow)
        task = asyncio.create_task(broker.request("tok", send))
        await asyncio.sleep(0.01)
        broker.resolve(captured["rid"], allow=True)
        out = await task
        assert out["decision"] == "allow"

    asyncio.run(scenario())


def test_deny_roundtrip_carries_message():
    async def scenario():
        broker = PermissionBroker(timeout=5)
        ws = _FakeWS()
        broker.register("tok", ws)
        cap = {}

        async def send(rid):
            cap["rid"] = rid

        task = asyncio.create_task(broker.request("tok", send))
        await asyncio.sleep(0.01)
        broker.resolve(cap["rid"], allow=False, message="用户拒绝")
        out = await task
        assert out == {"decision": "deny", "message": "用户拒绝"}

    asyncio.run(scenario())


def test_unknown_token_defaults_deny():
    async def scenario():
        broker = PermissionBroker(timeout=5)

        async def send(rid):
            raise AssertionError("不该推送:token 无效应直接默拒")

        out = await broker.request("ghost", send)
        assert out["decision"] == "deny"

    asyncio.run(scenario())


def test_timeout_defaults_deny():
    async def scenario():
        broker = PermissionBroker(timeout=0.2)  # 快速超时（完成标准 2：120s 不操作→默拒）
        ws = _FakeWS()
        broker.register("tok", ws)

        async def send(rid):
            pass  # 推送了但永不 resolve → 触发超时

        out = await broker.request("tok", send)
        assert out["decision"] == "deny"
        assert out["message"] == "timed out"

    asyncio.run(scenario())


def test_send_failure_defaults_deny():
    async def scenario():
        broker = PermissionBroker(timeout=5)
        ws = _FakeWS()
        broker.register("tok", ws)

        async def send(rid):
            raise RuntimeError("ws send failed")

        out = await broker.request("tok", send)
        assert out["decision"] == "deny"
        assert out["message"] == "failed to reach client"

    asyncio.run(scenario())


def test_disconnect_denies_all_pending():
    async def scenario():
        broker = PermissionBroker(timeout=5)
        ws = _FakeWS()
        broker.register("tok", ws)
        rids = []

        async def send(rid):
            rids.append(rid)

        # 同一连接挂起两个请求，都不 resolve
        t1 = asyncio.create_task(broker.request("tok", send))
        t2 = asyncio.create_task(broker.request("tok", send))
        await asyncio.sleep(0.02)
        assert len(rids) == 2
        broker.deny_all(ws)  # 模拟断连清队
        o1, o2 = await asyncio.gather(t1, t2)
        assert o1["decision"] == "deny" and o2["decision"] == "deny"
        assert "connection closed" in (o1["message"], o2["message"])

    asyncio.run(scenario())


def test_multiple_requests_no_crosstalk():
    """完成标准 3：多个授权请求排队不丢失、不串扰——各自 request_id 独立解算。"""
    async def scenario():
        broker = PermissionBroker(timeout=5)
        ws = _FakeWS()
        broker.register("tok", ws)
        rids = []

        async def send(rid):
            rids.append(rid)

        t1 = asyncio.create_task(broker.request("tok", send))
        t2 = asyncio.create_task(broker.request("tok", send))
        t3 = asyncio.create_task(broker.request("tok", send))
        await asyncio.sleep(0.02)
        assert len(set(rids)) == 3  # 三个不同 request_id
        # 乱序解算：t2 allow、t1 deny、t3 allow，互不影响
        broker.resolve(rids[1], allow=True)
        broker.resolve(rids[0], allow=False, message="no")
        broker.resolve(rids[2], allow=True)
        o1, o2, o3 = await asyncio.gather(t1, t2, t3)
        assert o1["decision"] == "deny"
        assert o2["decision"] == "allow"
        assert o3["decision"] == "allow"

    asyncio.run(scenario())


def test_resolve_unknown_request_id_is_noop():
    async def scenario():
        broker = PermissionBroker(timeout=5)
        # 未知 request_id（已超时清理/伪造）不应抛错
        broker.resolve("nonexistent", allow=True)
        broker.resolve(None, allow=False)

    asyncio.run(scenario())


def test_unregister_clears_token():
    broker = PermissionBroker(timeout=5)
    ws = _FakeWS()
    broker.register("tok", ws)
    assert broker.token_for(ws) == "tok"
    assert broker.ws_for_token("tok") is ws
    broker.unregister(ws)
    assert broker.token_for(ws) is None
    assert broker.ws_for_token("tok") is None
