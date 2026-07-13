"""把一整个文件夹的 Markdown 笔记迁进记忆库 —— 新用户「接着上次」的一键导入脚本。

遍历 `<文件夹>` 下所有 `*.md`，解析可选的 YAML frontmatter，按需切块，批量 POST 到记忆
服务的 `/memory/import` 端点。**零第三方依赖**（纯标准库），随仓库开箱即用。

用法：
    # 先起记忆服务（参考实现，重启不丢）：
    python examples/memory_stub.py                 # 127.0.0.1:8780

    # 再把你的笔记文件夹导进去：
    python examples/import_md.py ./my-notes
    python examples/import_md.py ./my-notes --url http://127.0.0.1:8780
    python examples/import_md.py ./my-notes --klass daily        # 无 frontmatter 时的默认分类
    python examples/import_md.py ./my-notes --split-sections     # 长文按 ## 标题切成多条
    python examples/import_md.py ./my-notes --dry-run            # 只解析打印，不写入

frontmatter（可选，逐文件）——认这几个键，缺了就用默认：
    ---
    title: 关于我
    date: 2025-01-01            # 也可从文件名里的日期自动提取，如 2026-06-25.md
    klass: preference          # 记忆分类；缺省用 --klass（默认 fact）
    importance: 7              # 透传给记忆服务（stub 忽略，真实引擎可据此排序）
    ---
    正文……

设计取舍：只认「通用 Markdown + 简单 frontmatter」，不替你解析任意平台的导出格式
（claude.ai 等导出格式多变）。Claude Code 的 auto-memory 文件天然就是「带 frontmatter 的
Markdown」，所以它们直接就能被这个脚本覆盖——把那个 memory 文件夹指给本脚本即可。
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 文件名里的日期（可选带 -HHmm 时间，如 2026-06-25-0358）——从命名提取 origin_date。
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:-(\d{2})(\d{2}))?")
# ## 二级标题切块（--split-sections 时用）。
_SECTION_SPLIT = re.compile(r"^##\s+", re.MULTILINE)
_BATCH = 50  # 每批 POST 的条数


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """提取 `--- ... ---` 之间的简单 frontmatter，返回 (元数据 dict, 正文)。

    有意只支持标量与简单列表——避免为一个导入脚本引入 YAML 依赖。不认识的键忽略。
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, flags=re.DOTALL)
    if not m:
        return {}, text

    data: dict = {}
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("\"'")
    return data, text[m.end():]


def extract_date(path: Path) -> str | None:
    """从文件名里抓日期（可选时间），没有就返回 None。"""
    m = _DATE_RE.search(path.stem)
    if not m:
        return None
    date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if m.group(4) and m.group(5):
        return f"{date}T{m.group(4)}:{m.group(5)}"
    return date


def split_sections(body: str) -> list[str]:
    """按 ## 标题切块；过短的块（<20 字）丢弃，超长块（>2000 字）再按空行拆。"""
    chunks: list[str] = []
    for part in _SECTION_SPLIT.split(body):
        part = part.strip()
        if len(part) < 20:
            continue
        if len(part) <= 2000:
            chunks.append(part)
            continue
        buf = ""
        for para in re.split(r"\n\n+", part):
            if len(buf) + len(para) > 1500 and len(buf) > 50:
                chunks.append(buf.strip())
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if len(buf.strip()) > 20:
            chunks.append(buf.strip())
    return chunks


def build_items(md_file: Path, default_klass: str, do_split: bool) -> list[dict]:
    """解析单个 .md 文件，产出待导入条目列表（{content, klass, origin_date?}）。"""
    try:
        text = md_file.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  跳过 {md_file.name}: {e}")
        return []

    frontmatter, body = parse_frontmatter(text)
    klass = frontmatter.get("klass") or default_klass
    origin_date = frontmatter.get("date") or extract_date(md_file)
    body = body.strip()
    if not body:
        return []

    contents = split_sections(body) if do_split else [body]
    items = []
    for content in contents:
        if len(content) < 4:
            continue
        item = {"content": content, "klass": klass}
        if origin_date:
            item["origin_date"] = origin_date
        items.append(item)
    return items


def post_import(base_url: str, items: list[dict]) -> dict:
    """POST 一批条目到 {url}/memory/import，返回响应 JSON。"""
    url = base_url.rstrip("/") + "/memory/import"
    payload = json.dumps(items, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="把一个文件夹的 Markdown 笔记导入记忆库")
    parser.add_argument("folder", help="要导入的文件夹（递归找 *.md）")
    parser.add_argument("--url", default="http://127.0.0.1:8780",
                        help="记忆服务地址（默认 http://127.0.0.1:8780）")
    parser.add_argument("--klass", default="fact",
                        help="无 frontmatter klass 时的默认分类（默认 fact）")
    parser.add_argument("--split-sections", action="store_true",
                        help="长文按 ## 标题切成多条（默认整篇一条）")
    parser.add_argument("--dry-run", action="store_true", help="只解析打印，不写入")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"错误：找不到文件夹 {folder}")
        return 1

    md_files = sorted(folder.rglob("*.md"))
    if not md_files:
        print(f"{folder} 下没有 .md 文件。")
        return 0

    all_items: list[dict] = []
    for f in md_files:
        items = build_items(f, args.klass, args.split_sections)
        all_items.extend(items)
        print(f"  {f.relative_to(folder)}: {len(items)} 条")

    print(f"\n共解析出 {len(all_items)} 条，来自 {len(md_files)} 个文件。")
    if args.dry_run:
        print("[dry-run] 未写入，退出。")
        return 0
    if not all_items:
        return 0

    stored = skipped = 0
    try:
        for i in range(0, len(all_items), _BATCH):
            resp = post_import(args.url, all_items[i:i + _BATCH])
            stored += resp.get("stored", 0)
            skipped += resp.get("skipped", 0)
    except urllib.error.URLError as e:
        print(f"\n导入失败：连不上 {args.url}（记忆服务起了吗？）\n  {e}")
        return 1

    print(f"\n导入完成：写入 {stored} 条，跳过 {skipped} 条。重启记忆服务后数据仍在。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
