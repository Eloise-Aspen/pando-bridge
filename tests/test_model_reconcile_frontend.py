"""执行模型菜单真实函数的离线回归。"""
import shutil
import subprocess
from pathlib import Path
import pytest

@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_model_reconcile_frontend():
    subprocess.run(["node", str(Path(__file__).with_name("frontend_model_reconcile.cjs"))],
                   check=True, timeout=15)
