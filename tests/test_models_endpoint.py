"""`GET /models` 三级取值契约：插件供数优先 → config.MODELS → 内置策展列表。

外部依赖降级规范：插件返回空/抛错都不能让端点报错，前端永远有东西可显示。
"""

from fastapi.testclient import TestClient

from pando import create_app
from pando.server import DEFAULT_MODELS, assign_primary, model_family, normalize_models


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

def test_shape_is_list_of_id_label_primary(tmp_path):
    body = _get_models(tmp_path)
    assert isinstance(body, list) and body
    for item in body:
        assert set(item) == {"id", "label", "primary"}
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["label"], str) and item["label"]
        assert isinstance(item["primary"], bool)


def test_created_at_never_leaks_into_contract(tmp_path):
    """created_at 只是算分层的中间字段，不出契约。"""
    body = _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.DatedModelProviderPlugin",
    ])
    assert all("created_at" not in m for m in body)


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
    assert body == [{"id": "cfg-model", "label": "Cfg Model", "primary": True}]


def test_plugin_wins_over_config(tmp_path):
    body = _get_models(
        tmp_path,
        plugins=["tests.fixtures.model_plugins.ModelProviderPlugin"],
        models=[("cfg-model", "Cfg Model")],
    )
    assert body == [{"id": "plugin-model-a", "label": "Plugin A", "primary": True}]


def test_first_nonempty_plugin_wins(tmp_path):
    """前一个插件返回空 → 继续问下一个插件。"""
    body = _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.EmptyModelProviderPlugin",
        "tests.fixtures.model_plugins.ModelProviderPlugin",
    ])
    assert body == [{"id": "plugin-model-a", "label": "Plugin A", "primary": True}]


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
    assert body == [{"id": "cfg-model", "label": "Cfg Model", "primary": True}]


def test_broken_plugin_falls_back_to_builtin(tmp_path):
    assert _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.BrokenModelProviderPlugin",
    ]) == DEFAULT_MODELS


def test_plugin_loose_shapes_are_normalized(tmp_path):
    body = _get_models(tmp_path, plugins=[
        "tests.fixtures.model_plugins.LooseShapeModelProviderPlugin",
    ])
    assert body == [
        {"id": "claude-opus-5", "label": "Opus 5", "primary": True},
        {"id": "claude-sonnet-5", "label": "claude-sonnet-5", "primary": True},
        {"id": "claude-fable-5", "label": "claude-fable-5", "primary": True},
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


def test_normalize_models_carries_primary_and_created_at():
    assert normalize_models([
        {"id": "a", "primary": True, "created_at": "2026-01-01"},
        ("b", "B", False),
        {"id": "c", "created_at": 123},     # 非字符串 created_at → 丢弃
    ]) == [
        {"id": "a", "label": "a", "primary": True, "created_at": "2026-01-01"},
        {"id": "b", "label": "B", "primary": False},
        {"id": "c", "label": "c"},
    ]


# --------------------------------------------------------------------------
# 分层规则（Task 5 / 补裁 5：每族取最新为主线，不写死名单）
# --------------------------------------------------------------------------

def test_model_family_parsing():
    assert model_family("claude-opus-4-5-20251101") == "opus"
    assert model_family("claude-3-5-haiku-20241022") == "haiku"
    assert model_family("claude-sonnet-5") == "sonnet"
    assert model_family("claude-fable-5") == "fable"
    # 不写死族名单：没见过的族照样解析得出
    assert model_family("claude-newfamily-1") == "newfamily"
    # 解析不出族 → None（归非主线）
    for bad in (None, "", "2026-01-01", "claude", "---"):
        assert model_family(bad) is None


def _primary_ids(models):
    return [m["id"] for m in assign_primary(models) if m["primary"]]


def test_assign_primary_newest_per_family():
    """实测形态：10 条动态列表按 created_at 降序，各族最新为主线。"""
    models = [
        {"id": "claude-opus-5", "created_at": "2026-06-01"},
        {"id": "claude-sonnet-5", "created_at": "2026-06-01"},
        {"id": "claude-fable-5", "created_at": "2026-05-01"},
        {"id": "claude-opus-4-8", "created_at": "2026-02-01"},
        {"id": "claude-opus-4-7", "created_at": "2026-01-01"},
        {"id": "claude-sonnet-4-6", "created_at": "2025-12-01"},
        {"id": "claude-opus-4-6", "created_at": "2025-12-01"},
        {"id": "claude-opus-4-5-20251101", "created_at": "2025-11-01"},
        {"id": "claude-haiku-4-5-20251001", "created_at": "2025-10-01"},
        {"id": "claude-sonnet-4-5-20250929", "created_at": "2025-09-29"},
    ]
    assert _primary_ids(models) == [
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001",
    ]


def test_assign_primary_ignores_list_order_when_dated():
    """乱序输入也照 created_at 判，不是「谁在前谁赢」。"""
    models = [
        {"id": "claude-opus-4-6", "created_at": "2025-12-01"},
        {"id": "claude-opus-5", "created_at": "2026-06-01"},
    ]
    assert _primary_ids(models) == ["claude-opus-5"]


def test_assign_primary_single_generation_family():
    """边界一：某族只有一代 → 那一代就是主线（不能因为「没得比」被漏掉）。"""
    models = [
        {"id": "claude-opus-5", "created_at": "2026-06-01"},
        {"id": "claude-opus-4-8", "created_at": "2026-02-01"},
        {"id": "claude-fable-5", "created_at": "2026-05-01"},   # fable 独苗
    ]
    assert _primary_ids(models) == ["claude-opus-5", "claude-fable-5"]


def test_assign_primary_without_created_at_falls_back_to_list_order():
    """边界二：旧版缓存文件只有 {id,label}，缺 created_at → 退化为「每族取列表首个」。

    官方列表本就新→旧返回，缓存也照原序存，故这条退化路径与真实分层等价。
    """
    models = [
        {"id": "claude-opus-5", "label": "Opus 5"},
        {"id": "claude-sonnet-5", "label": "Sonnet 5"},
        {"id": "claude-fable-5", "label": "Fable 5"},
        {"id": "claude-opus-4-8", "label": "Opus 4.8"},
        {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
    ]
    assert _primary_ids(models) == [
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001",
    ]


def test_assign_primary_mixed_missing_created_at_loses_to_dated():
    """半旧半新的缓存：有 created_at 的一律优先于没有的，不至于让缺字段的老条目篡位。"""
    models = [
        {"id": "claude-opus-4-6"},                                # 缺 created_at，且排在前
        {"id": "claude-opus-5", "created_at": "2026-06-01"},
    ]
    assert _primary_ids(models) == ["claude-opus-5"]


def test_assign_primary_unparseable_family_is_never_primary():
    models = [{"id": "claude-opus-5", "created_at": "2026-06-01"}, {"id": "2026"}]
    assert _primary_ids(models) == ["claude-opus-5"]


def test_assign_primary_respects_explicit_annotation():
    """静态策展表手工标注 primary → 原样尊重，不再自动算。"""
    models = [
        {"id": "claude-opus-5", "primary": True},
        {"id": "claude-fable-5", "primary": False},   # 手工判它非主线就是非主线
    ]
    assert _primary_ids(models) == ["claude-opus-5"]


def test_builtin_table_primary_is_the_four_mainline(tmp_path):
    """内置兜底表（降级到公开仓策展表时）分层同样正确。"""
    body = _get_models(tmp_path)
    assert [m["id"] for m in body if m["primary"]] == [
        "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5",
    ]
