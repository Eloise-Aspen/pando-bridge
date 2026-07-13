"""feat-memory-onboarding 关键裁决②③：stub 文件持久化 + /memory/import 批量迁入。

- 重启不丢：写入后清空内存态、再从盘 _load 回来，记忆仍在且可召回。
- /memory/import：批量写入，短内容跳过，写入后可被 /recall 命中。
"""

import pytest
from fastapi.testclient import TestClient

from examples import memory_stub


@pytest.fixture
def stub(tmp_path):
    # 配置落盘目录并清空内存态；teardown 把全局态还原成「无持久化」，
    # 避免污染 contract/admin 等假设纯内存的测试模块。
    memory_stub._STORE_PATH = None
    memory_stub._MEMORIES.clear()
    memory_stub._NEXT_ID = 1
    memory_stub._configure(tmp_path)
    memory_stub._MEMORIES.clear()
    memory_stub._NEXT_ID = 1
    yield TestClient(memory_stub.app)
    memory_stub._MEMORIES.clear()
    memory_stub._NEXT_ID = 1
    memory_stub._STORE_PATH = None


def test_import_batch_stored_skipped_and_recallable(stub):
    resp = stub.post("/memory/import", json=[
        {"content": "喜欢在下雨天写代码"},
        {"content": "养了一只叫麻薯的猫", "klass": "fact", "origin_date": "2025-01-01T00:00:00+00:00"},
        {"content": "x"},  # 太短，应跳过
    ])
    body = resp.json()
    assert body["stored"] == 2
    assert body["skipped"] == 1
    assert body["total"] == 2

    hit = stub.post("/recall", json={"query": "麻薯"})
    assert "养了一只叫麻薯的猫" in hit.json()["context"]

    # origin_date 应作为 created_at 保留
    listed = stub.get("/memory/list").json()["items"]
    cat = next(m for m in listed if "麻薯" in m["content"])
    assert cat["created_at"] == "2025-01-01T00:00:00+00:00"
    assert cat["klass"] == "fact"


def test_data_survives_restart(stub):
    stub.post("/memory/import", json=[{"content": "重启不该丢的记忆"}])

    # 模拟进程重启：清空内存热副本，确认此刻召回为空。
    memory_stub._MEMORIES.clear()
    memory_stub._NEXT_ID = 1
    assert stub.post("/recall", json={"query": "重启"}).json()["context"] == ""

    # 从盘 _load 回来——记忆应重现。
    memory_stub._load()
    assert "重启不该丢的记忆" in stub.post("/recall", json={"query": "重启"}).json()["context"]


def test_delete_persists(stub):
    stub.post("/memory/import", json=[{"content": "待删除的记忆条目"}])
    mem_id = stub.get("/memory/list").json()["items"][0]["id"]
    stub.delete(f"/memory/{mem_id}")

    # 重启后确认删除也落了盘（不是只改内存）。
    memory_stub._MEMORIES.clear()
    memory_stub._load()
    assert stub.get("/memory/stats").json()["count"] == 0
