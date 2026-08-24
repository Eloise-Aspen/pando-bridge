"""MEMORY_SERVICE_HEADERS —— 记忆服务要求鉴权时，固定请求头要一路带到 HTTP 调用上。

契约四端点走 HttpMemoryProvider，管理面走 MemoryPlugin 的 /memory-admin 代理，
两处共用 provider 上的同一份 headers（凭证只配一遍，且只从服务端配置来）。
"""
from pando.providers import get_provider
from pando.providers.http import HttpMemoryProvider

HEADERS = {"X-Memory-Token": "unit-test-token"}


class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"context": "ok"}


def test_provider_sends_configured_headers(monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _FakeResp()

    monkeypatch.setattr("pando.providers.http.requests.post", fake_post)
    provider = HttpMemoryProvider("http://127.0.0.1:9/", headers=HEADERS)
    assert provider.build_session_context() == "ok"
    assert seen["headers"] == HEADERS


def test_provider_without_headers_sends_none(monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen["headers"] = kwargs.get("headers")
        return _FakeResp()

    monkeypatch.setattr("pando.providers.http.requests.post", fake_post)
    assert HttpMemoryProvider("http://127.0.0.1:9/").build_recall_context("q") == "ok"
    assert seen["headers"] is None


def test_finalize_archive_passes_session(monkeypatch):
    """/archive body 透传可选 session 字段（feat-forge-receipt 裁决 8）。"""
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return _FakeResp()

    monkeypatch.setattr("pando.providers.http.requests.post", fake_post)
    provider = HttpMemoryProvider("http://127.0.0.1:9/")
    provider.finalize_archive("raw text", session="sess-1")
    assert seen["url"].endswith("/archive")
    assert seen["json"] == {"raw": "raw text", "session": "sess-1"}
    # 不带 session 时 body 不出现该字段（向后兼容旧服务）
    provider.finalize_archive("raw text")
    assert seen["json"] == {"raw": "raw text"}


def test_get_provider_passes_headers_through():
    provider = get_provider("http://127.0.0.1:9/", headers=HEADERS)
    assert provider.headers == HEADERS
    # 不配时是空 dict，代理层据此不注入任何头
    assert get_provider("http://127.0.0.1:9/").headers == {}
