"""examples/memory_stub.py 的 4 端点契约往返（v2）：
session_context / recall / archive_prompt / archive。"""

from fastapi.testclient import TestClient

from examples import memory_stub


def _client():
    memory_stub._MEMORIES.clear()  # 每个测试独立，不依赖其他测试留下的状态
    return TestClient(memory_stub.app)


def test_session_context_empty_then_after_archive():
    client = _client()

    resp = client.post("/session_context", json={})
    assert resp.status_code == 200
    assert resp.json() == {"context": ""}

    archive_resp = client.post("/archive", json={
        "raw": '{"worthy": true, "content": "喜欢在下雨天写代码"}',
    })
    assert archive_resp.json()["stored"] == 1

    resp2 = client.post("/session_context", json={})
    assert "喜欢在下雨天写代码" in resp2.json()["context"]


def test_recall_matches_substring():
    client = _client()
    client.post("/archive", json={"raw": '{"worthy": true, "content": "很喜欢猫这种动物"}'})

    hit = client.post("/recall", json={"query": "猫"})
    assert "很喜欢猫这种动物" in hit.json()["context"]

    miss = client.post("/recall", json={"query": "完全不相关的词组xyz"})
    assert miss.json()["context"] == ""


def test_archive_prompt_gates_on_length_unless_forced():
    client = _client()

    short = client.post("/archive_prompt", json={"messages": [{"role": "user", "content": "hi"}], "force": False})
    assert short.json()["prompt"] is None

    forced = client.post("/archive_prompt", json={"messages": [{"role": "user", "content": "hi"}], "force": True})
    assert isinstance(forced.json()["prompt"], str) and forced.json()["prompt"]

    long_msgs = [{"role": "user", "content": "x" * 250}]
    long_resp = client.post("/archive_prompt", json={"messages": long_msgs, "force": False})
    assert isinstance(long_resp.json()["prompt"], str)


def test_archive_rejects_not_worthy_and_malformed():
    client = _client()

    not_worthy = client.post("/archive", json={"raw": '{"worthy": false}'})
    assert not_worthy.json()["stored"] == 0

    malformed = client.post("/archive", json={"raw": "not json at all"})
    assert malformed.json()["stored"] == 0
