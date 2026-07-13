"""examples/import_md.py：文件夹 Markdown 迁入脚本的解析 + 端到端入库/召回。

覆盖 Task 2/3 追加验收：含 frontmatter 与不含 frontmatter 的混合文件夹，全部入库可召回。
端到端不起真 socket——用脚本的 build_items 解析文件夹，再经 stub 的 TestClient 打
/memory/import，最后 /recall 断言，链路完整且 hermetic。
"""

from fastapi.testclient import TestClient

from examples import import_md, memory_stub


def _write(folder, name, text):
    p = folder / name
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_frontmatter_and_defaults(tmp_path):
    with_fm = _write(tmp_path, "about.md",
                     "---\ntitle: 关于我\nklass: preference\ndate: 2025-01-01\n---\n"
                     "我偏好简洁直接的回答。")
    without_fm = _write(tmp_path, "2026-06-25-note.md", "今天决定用 Go 重写后端服务。")

    fm_items = import_md.build_items(with_fm, default_klass="fact", do_split=False)
    assert len(fm_items) == 1
    assert fm_items[0]["klass"] == "preference"          # 取自 frontmatter
    assert fm_items[0]["origin_date"] == "2025-01-01"    # 取自 frontmatter

    plain_items = import_md.build_items(without_fm, default_klass="daily", do_split=False)
    assert len(plain_items) == 1
    assert plain_items[0]["klass"] == "daily"            # 无 frontmatter → 默认 klass
    assert plain_items[0]["origin_date"] == "2026-06-25"  # 从文件名提取日期
    assert "content" in plain_items[0]


def test_split_sections_chunks_by_h2(tmp_path):
    doc = _write(tmp_path, "long.md",
                 "## 第一节\n" + "这是第一节的内容，足够长以通过阈值。" * 2 +
                 "\n\n## 第二节\n" + "这是第二节的内容，也足够长以通过阈值。" * 2)
    whole = import_md.build_items(doc, default_klass="fact", do_split=False)
    assert len(whole) == 1                                # 默认整篇一条
    split = import_md.build_items(doc, default_klass="fact", do_split=True)
    assert len(split) == 2                                # --split-sections → 按 ## 切两条


def test_mixed_folder_imports_and_recalls(tmp_path):
    _write(tmp_path, "identity.md",
           "---\nklass: fact\n---\n我叫 Alex，是个后端工程师，主力语言 Go。")
    _write(tmp_path, "pets.md", "养了一只叫麻薯的橘猫。")  # 无 frontmatter
    sub = tmp_path / "notes"
    sub.mkdir()
    _write(sub, "pref.md", "偏好简洁直接的回答，不要寒暄。")  # 递归子目录

    # 解析整个文件夹（含子目录）
    items = []
    for f in sorted(tmp_path.rglob("*.md")):
        items.extend(import_md.build_items(f, default_klass="fact", do_split=False))
    assert len(items) == 3

    # 经 stub 入库，再逐条召回验证
    memory_stub._STORE_PATH = None
    memory_stub._MEMORIES.clear()
    memory_stub._NEXT_ID = 1
    client = TestClient(memory_stub.app)
    try:
        resp = client.post("/memory/import", json=items)
        assert resp.json()["stored"] == 3

        assert "Alex" in client.post("/recall", json={"query": "Go"}).json()["context"]
        assert "麻薯" in client.post("/recall", json={"query": "橘猫"}).json()["context"]
        assert "寒暄" in client.post("/recall", json={"query": "简洁"}).json()["context"]
    finally:
        memory_stub._MEMORIES.clear()
        memory_stub._NEXT_ID = 1
