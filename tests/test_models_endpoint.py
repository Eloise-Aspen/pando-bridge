"""`GET /models` 三级取值契约：插件供数优先 → config.MODELS → 内置策展列表。

外部依赖降级规范：插件返回空/抛错都不能让端点报错，前端永远有东西可显示。
"""

from fastapi.testclient import TestClient

from pando import create_app
from pando.server import DEFAULT_MODELS, normalize_models


def _make_config(tmp_path, plugins=None, models=None):
    cfg = {
        "CLAUDE_EXE": "/nonexistent/claude",  # 只验证 HTTP 端点，不起 claude 子进程
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": plugins or [],
        "ARCHIVE_INTERVAL": 600,
    }
    if models is not None:
        cfg["MODELS"] = models
    return cfg


def _get_models(tmp_path, **kw):
    client = TestClient(create_app(_make_config(tmp_path, **kw)))
    with client:
        r = client.get("/models")
    assert r.status_code == 200
    return r.json()


# --------------------------------------------------------------------------
# 形状
# --------------------------------------------------------------------------

def test_shape_is_list_of_id_label(tmp_path):
    body = _get_models(tmp_path)
    assert isinstance(body, list) and body
    for item in body:
        assert set(item) == {"id", "label"}
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["label"], str) and item["label"]


# --------------------------------------------------------------------------
# 三级取值
# --------------------------------------------------------------------------

def test_no_plugin_no_config_falls_back_to_builtin(tmp_path):
    """公开仓单独跑（无私有插件、无配置表）：返回内置策展列表，不报错。"""
    assert _get_models(tmp_path) == DEFAULT_MODELS


def test_builtin_contains_opus5_and_sonnet5(tmp_path):
    ids = {m["id"] for m in _get_models(tmp_path)}
    assert {"claude-opus-5", "claude-sonnet-5"} <= ids


def test_config_models_override_builtin(tmp_path):
    body = _get_models(tmp_path, models=[("cfg-model", "Cfg Model")])
    assert body == [{"id": "cfg-model", "label": "Cfg Model"}]


def test_plugin_wins_over_config(tmp_path):
    body = _get_models(
        tmp_path,
        plugins=["tests.fixtures.model_plugins.ModelProviderPlugin"],
        models=[("cfg-model", "Cfg Model")],
    )
    assert body == [{"id": "plugin-model-a", "label": "Plugin A"}]


def test_first_nonempty_plugin_wins(tmp_path):
    """前一个插件返回空 → 继续问下一个插件。"""
    body = _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.EmptyModelProviderPlugin",
        "tests.fixtures.model_plugins.ModelProviderPlugin",
    ])
    assert body == [{"id": "plugin-model-a", "label": "Plugin A"}]


def test_empty_plugin_falls_back_to_builtin(tmp_path):
    assert _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.EmptyModelProviderPlugin",
    ]) == DEFAULT_MODELS


def test_broken_plugin_falls_back_to_config(tmp_path):
    """插件抛错被 _call_hook 吞掉，端点仍 200 并回落配置表。"""
    body = _get_models(
        tmp_path,
        plugins=["tests.fixtures.model_plugins.BrokenModelProviderPlugin"],
        models=[("cfg-model", "Cfg Model")],
    )
    assert body == [{"id": "cfg-model", "label": "Cfg Model"}]


def test_broken_plugin_falls_back_to_builtin(tmp_path):
    assert _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.BrokenModelProviderPlugin",
    ]) == DEFAULT_MODELS


def test_plugin_loose_shapes_are_normalized(tmp_path):
    body = _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.LooseShapeModelProviderPlugin",
    ])
    assert body == [
        {"id": "claude-opus-5", "label": "Opus 5"},
        {"id": "claude-sonnet-5", "label": "claude-sonnet-5"},
        {"id": "claude-fable-5", "label": "claude-fable-5"},
    ]


# --------------------------------------------------------------------------
# normalize_models 单元
# --------------------------------------------------------------------------

def test_normalize_models_rejects_non_list():
    for bad in (None, {}, "claude-opus-5", 42):
        assert normalize_models(bad) == []


def test_normalize_models_dedupes_and_defaults_label():
    assert normalize_models(["a", ("a", "A"), {"id": "b"}]) == [
        {"id": "a", "label": "a"},
        {"id": "b", "label": "b"},
    ]
