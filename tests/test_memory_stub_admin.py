"""memory_stub 管理面软约定端点(provider-contract-v2.md 第 5/6 节):
stats / list / search / delete。这些不属于 4 端点契约,是旁路管理链路。"""

import json

from fastapi.testclient import TestClient

from examples import memory_stub


def _client():
    memory_stub._MEMORIES.clear()  # 每个测试独立;_NEXT_ID 递增不影响断言(不假设 id 值)
    return TestClient(memory_stub.app)


def _seed(client, *contents):
    for c in contents:
        client.post("/archive", json={"raw": json.dumps({"worthy": True, "content": c})})


def test_stats_reflects_count_and_chars():
    # 包含式断言:stats 契约允许附加字段(如 log_total),只校验本测试关心的键,
    # 不用全字典相等——否则契约每加一个字段就误伤此测试(fix-launch-smoke 的教训)。
    client = _client()
    empty = client.get("/memory/stats").json()
    assert empty["count"] == 0 and empty["total_chars"] == 0
    _seed(client, "我很喜欢猫", "喜欢下雨天写代码")
    stats = client.get("/memory/stats").json()
    assert stats["count"] == 2
    assert stats["total_chars"] == len("我很喜欢猫") + len("喜欢下雨天写代码")


def test_list_newest_first_with_ids():
    client = _client()
    _seed(client, "第一条记忆", "第二条记忆")
    data = client.get("/memory/list").json()
    assert data["total"] == 2
    assert [m["content"] for m in data["items"]] == ["第二条记忆", "第一条记忆"]
    assert all(isinstance(m["id"], int) for m in data["items"])


def test_search_substring_and_empty_query():
    client = _client()
    _seed(client, "很喜欢猫这种动物", "今天写了很多代码")
    hit = client.get("/memory/search", params={"q": "猫"}).json()
    assert len(hit["items"]) == 1 and "猫" in hit["items"][0]["content"]
    miss = client.get("/memory/search", params={"q": "xyz不相关"}).json()
    assert miss["items"] == []
    empty = client.get("/memory/search", params={"q": ""}).json()
    assert empty["items"] == []


def test_delete_by_id_then_404():
    client = _client()
    _seed(client, "待删除的记忆", "要保留的记忆")
    items = client.get("/memory/list").json()["items"]
    target = next(m for m in items if m["content"] == "待删除的记忆")
    resp = client.delete(f"/memory/{target['id']}")
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    remaining = [m["content"] for m in client.get("/memory/list").json()["items"]]
    assert remaining == ["要保留的记忆"]
    # 再删同一 id → 404(已不存在)
    assert client.delete(f"/memory/{target['id']}").status_code == 404
