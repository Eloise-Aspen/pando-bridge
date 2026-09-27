"""欢迎屏的可选在线天数：前端只消费 /health 下发的中性日期。"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"
NODE = shutil.which("node")


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_welcome_uses_optional_health_since_date():
    src = _source()
    boot = src.split("async function boot(", 1)[1].split("boot();", 1)[0]
    assert "daysSince(health&&health.since_date)" in boot
    assert "if(days) parts.push(`在线 ${days} 天`)" in boot
    assert "const A=new Date(" not in src


@pytest.mark.skipif(NODE is None, reason="需要 node 跑前端日期替身")
def test_days_since_uses_local_calendar_days_and_hides_bad_values():
    src = _source()
    match = re.search(r"function daysSince\(.*?\n\}", src, re.S)
    assert match, "daysSince 未定义"
    script = f"""
{match.group(0)}
const out = [
  daysSince('2020-01-01', new Date(2020, 0, 1, 23, 59)),
  daysSince('2020-01-01', new Date(2020, 0, 2, 0, 1)),
  daysSince(undefined, new Date(2020, 0, 2)),
  daysSince('2020-02-30', new Date(2020, 2, 1)),
];
console.log(JSON.stringify(out));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, 2, None, None]
