"""主题路由：/themes 列举可用主题，/themes/<名>/theme.css 伺服资源。

2026-09-14 改为自造夹具：原先依赖包内 `static/themes/_smoke` 真实存在，
用户删掉那个样例主题后测试即挂——**测试不该依赖仓里恰好有某个主题**。
现在每个用例用 tmp_path 覆盖 STATIC_DIR 并自己造，删任何样例都不再波及。
"""

from fastapi.testclient import TestClient

from pando import create_app


def _config(tmp_path, static_dir=None):
    cfg = {
        "CLAUDE_EXE": "claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }
    if static_dir is not None:
        cfg["STATIC_DIR"] = str(static_dir)
    return cfg


def _static_with_theme(tmp_path, name="demo", body=":root { --accent: #3F6B55; }"):
    """造一个含单个主题的 static 目录，返回该目录。"""
    static_dir = tmp_path / "static"
    theme_dir = static_dir / "themes" / name
    theme_dir.mkdir(parents=True)
    (theme_dir / "theme.css").write_text(body, encoding="utf-8")
    return static_dir


def test_theme_css_served_with_css_mime(tmp_path):
    """主题存在 → 200 + text/css + 内容原样送达。"""
    static_dir = _static_with_theme(tmp_path)
    c = TestClient(create_app(_config(tmp_path, static_dir)))
    r = c.get("/themes/demo/theme.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "--accent" in r.text


def test_theme_rejects_non_whitelisted_filename(tmp_path):
    """只放行 theme.css / theme.js，其它文件名直接 404（不落磁盘查找）。"""
    static_dir = _static_with_theme(tmp_path)
    c = TestClient(create_app(_config(tmp_path, static_dir)))
    assert c.get("/themes/demo/evil.js").status_code == 404


def test_theme_missing_theme_returns_404(tmp_path):
    """白名单文件名但主题目录不存在 → 404（is_file 兜底）。"""
    static_dir = _static_with_theme(tmp_path)
    c = TestClient(create_app(_config(tmp_path, static_dir)))
    assert c.get("/themes/nonexistent/theme.css").status_code == 404


def test_theme_list_enumerates_available_themes(tmp_path):
    """/themes 列出含 theme.css 的子目录名，按名排序。"""
    static_dir = _static_with_theme(tmp_path, name="zeta")
    (static_dir / "themes" / "alpha").mkdir()
    (static_dir / "themes" / "alpha" / "theme.css").write_text(":root{}", encoding="utf-8")
    # 没有 theme.css 的目录不算一个主题，不应出现在列表里
    (static_dir / "themes" / "empty-dir").mkdir()
    c = TestClient(create_app(_config(tmp_path, static_dir)))
    assert c.get("/themes").json() == ["alpha", "zeta"]


def test_theme_list_empty_when_dir_absent(tmp_path):
    """themes/ 目录整个不存在 → []，不是 500（用户删光样例后的真实状态）。"""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    c = TestClient(create_app(_config(tmp_path, static_dir)))
    assert c.get("/themes").json() == []
