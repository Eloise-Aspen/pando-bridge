"""markdown 子集渲染的结构性不变量（feat-md-render）。

渲染本身是浏览器里的 JS，逐条语法的正确性由浏览器固定输入集验收，这里不重复。
本文件只钉住一条**安全**不变量：renderMd 的第一步必须是对全文 escHtml。
只要这一步在，后续所有模式替换都作用在已转义文本上，原文里的 HTML 标签
就不可能变成活标签——这是整套渲染的安全前提，回归代价最高，所以自动化守住。
"""

import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_render_md_defined_and_exposed():
    """renderMd 有定义，且挂到 window 供插件复用。"""
    src = _source()
    assert "function renderMd(" in src
    assert "window.renderMd" in src


def test_render_md_escapes_whole_input_first():
    """renderMd 函数体的第一条语句就是对全文 escHtml —— 安全第一序。"""
    src = _source()
    body = src.split("function renderMd(", 1)[1]
    # 取函数签名后的第一条语句
    first = body.split("{", 1)[1].split(";", 1)[0]
    assert "escHtml(" in first, f"renderMd 第一步不是 escHtml：{first!r}"


def test_render_md_only_allows_http_links():
    """链接放行判据必须是 ^https?:// —— 挡掉 javascript:/data:。"""
    src = _source()
    assert re.search(r"\^https\?:\\/\\/", src), "缺少 http(s) 链接白名单判据"


def test_no_third_party_markdown_library():
    """不引入任何第三方 markdown 库（含拷贝进仓）。"""
    src = _source().lower()
    for name in ("marked.min.js", "markdown-it", "micromark", "dompurify"):
        assert name not in src, f"意外出现第三方库：{name}"
