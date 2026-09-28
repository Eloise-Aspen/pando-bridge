"""真实前端分发片段的无依赖 Node 离线回归。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_frontend_turn_gate():
    script = Path(__file__).with_name("frontend_turn_gate.cjs")
    subprocess.run(["node", str(script)], check=True, timeout=15)
