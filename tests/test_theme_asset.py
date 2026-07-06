"""主题资源路由 /themes/<名>/theme.css：只放行固定文件名、正确 MIME、缺失即 404。

不覆盖 STATIC_DIR → 用包内 static/，命中 themes/_smoke（机制验证用主题）。"""

from fastapi.testclient import TestClient

from pando import create_app


def _config(tmp_path):
    return {
        "CLAUDE_EXE": "claude",
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }


def test_theme_css_served_with_css_mime(tmp_path):
    """_smoke 主题存在 → 200 + text/css + 覆盖了 --accent。"""
    c = TestClient(create_app(_config(tmp_path)))
    r = c.get("/themes/_smoke/theme.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "--accent" in r.text


def test_theme_rejects_non_whitelisted_filename(tmp_path):
    """只放行 theme.css / theme.js，其它文件名直接 404（不落磁盘查找）。"""
    c = TestClient(create_app(_config(tmp_path)))
    assert c.get("/themes/_smoke/evil.js").status_code == 404


def test_theme_missing_theme_returns_404(tmp_path):
    """白名单文件名但主题目录不存在 → 404（is_file 兜底）。"""
    c = TestClient(create_app(_config(tmp_path)))
    assert c.get("/themes/nonexistent/theme.css").status_code == 404
