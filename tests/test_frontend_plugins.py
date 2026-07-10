"""前端插件 manifest 与资源伺服（feat-frontend-plugin-arch）。

/api/plugins：未配置目录 → []（demo 行为）；配置后 = 目录内实际 *.js 白名单。
/plugin-assets/<文件>：只放行 .js、挡路径穿越、缺失 404。
"""

from fastapi.testclient import TestClient

from pando import create_app


def _config(tmp_path, plugins_dir=None):
    cfg = {
        "CLAUDE_EXE": "claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }
    if plugins_dir is not None:
        cfg["FRONTEND_PLUGINS_DIR"] = str(plugins_dir)
    return cfg


def test_manifest_empty_when_unconfigured(tmp_path):
    """demo/公开仓不配置 FRONTEND_PLUGINS_DIR → manifest 恒为 []。"""
    c = TestClient(create_app(_config(tmp_path)))
    r = c.get("/api/plugins")
    assert r.status_code == 200
    assert r.json() == []


def test_manifest_lists_js_files_sorted(tmp_path):
    """manifest = 目录内实际 *.js 文件（按名排序），非 .js 文件不入清单。"""
    pdir = tmp_path / "fe-plugins"
    pdir.mkdir()
    (pdir / "b-two.js").write_text("//2", encoding="utf-8")
    (pdir / "a-one.js").write_text("//1", encoding="utf-8")
    (pdir / "server_side.py").write_text("# 不是前端插件", encoding="utf-8")
    c = TestClient(create_app(_config(tmp_path, pdir)))
    # src 带 ?v=<mtime> 版本参数(缓存失效);断言路径前缀与排序,版本值随文件系统变化
    items = c.get("/api/plugins").json()
    assert [m["id"] for m in items] == ["a-one", "b-two"]
    for m in items:
        path, _, ver = m["src"].partition("?v=")
        assert path == f"/plugin-assets/{m['id']}.js"
        assert ver.isdigit()


def test_plugin_asset_served_and_traversal_blocked(tmp_path):
    """资源路由：命中 .js → 200 + JS MIME；路径穿越与非 .js → 404。"""
    pdir = tmp_path / "fe-plugins"
    pdir.mkdir()
    (pdir / "a-one.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.js").write_text("no", encoding="utf-8")
    c = TestClient(create_app(_config(tmp_path, pdir)))
    r = c.get("/plugin-assets/a-one.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert c.get("/plugin-assets/missing.js").status_code == 404
    assert c.get("/plugin-assets/..%2Fsecret.js").status_code == 404


def test_plugin_asset_404_when_unconfigured(tmp_path):
    """未配置插件目录时资源路由一律 404。"""
    c = TestClient(create_app(_config(tmp_path)))
    assert c.get("/plugin-assets/a.js").status_code == 404
