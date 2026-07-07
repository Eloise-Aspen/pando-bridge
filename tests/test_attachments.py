"""附件上传端点（feat-attachment-upload Task 2）：
正常落盘 + 白名单/大小反例 + 启动清理旧件。存储目录用 UPLOAD_DIR 显式指向 tmp，
不碰真实 CLAUDE_CWD。"""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from pando import create_app


def _make_config(tmp_path):
    return {
        "CLAUDE_EXE": "/nonexistent/claude",  # 不真起子进程，仅测 HTTP 端点
        "CLAUDE_CWD": str(tmp_path),
        "DATA_DIR": str(tmp_path / "data"),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "MEMORY_SERVICE_URL": "",
        "PLUGINS": [],
        "ARCHIVE_INTERVAL": 600,
    }


def test_upload_png_ok(tmp_path):
    """正常用例：上传 png → 200，返回的 path 落在 upload_dir 内、文件真实存在、内容一致。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        blob = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"
        resp = client.post("/attachments", files={"file": ("shot.png", blob, "image/png")})
        assert resp.status_code == 200, resp.text
        path = resp.json()["path"]
        # UUID 重命名：文件名不再是原始名，扩展名保留
        p = Path(path)
        assert p.suffix == ".png"
        assert p.name != "shot.png"
        # 落在约定的 upload_dir/YYYYMM/ 下
        upload_dir = tmp_path / "uploads"
        assert upload_dir in p.parents
        assert p.exists()
        assert p.read_bytes() == blob


def test_upload_rejects_bad_type(tmp_path):
    """反例：扩展名不在白名单 → 400，且不落盘。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/attachments", files={"file": ("evil.exe", b"MZ...", "application/octet-stream")})
        assert resp.status_code == 400, resp.text
        # 未创建任何月份目录（没有落盘）
        upload_dir = tmp_path / "uploads"
        assert not any(upload_dir.rglob("*")) if upload_dir.exists() else True


def test_upload_rejects_oversize(tmp_path):
    """反例：超过 10MB → 413。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        big = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post("/attachments", files={"file": ("big.pdf", big, "application/pdf")})
        assert resp.status_code == 413, resp.text


def test_upload_rejects_empty(tmp_path):
    """反例：空文件 → 400。"""
    app = create_app(_make_config(tmp_path))
    with TestClient(app) as client:
        resp = client.post("/attachments", files={"file": ("empty.txt", b"", "text/plain")})
        assert resp.status_code == 400, resp.text


def test_startup_cleans_old_attachments(tmp_path):
    """启动清理：8 天前的旧件被删，新件保留。"""
    upload_dir = tmp_path / "uploads" / "202601"
    upload_dir.mkdir(parents=True)
    old = upload_dir / "old.png"
    fresh = upload_dir / "fresh.png"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    # 把 old 的 mtime 拨到 8 天前
    eight_days_ago = time.time() - 8 * 86400
    import os
    os.utime(old, (eight_days_ago, eight_days_ago))

    app = create_app(_make_config(tmp_path))
    with TestClient(app):  # 进入上下文触发 startup → _cleanup_old_attachments
        pass
    assert not old.exists()
    assert fresh.exists()
