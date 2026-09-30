"""回复工具条显示本轮 token，不显示 CLI 的累计美元成本。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="需要 node 跑前端 DOM 替身")
def test_usage_footer_shows_available_round_values_without_cost():
    src = INDEX.read_text(encoding="utf-8")
    fmt = "function fmtTok" + src.split("function fmtTok", 1)[1].split("// 三档水位色", 1)[0]
    usage = "function usageParts" + src.split("function usageParts", 1)[1].split("// 助手工具条", 1)[0]
    toolbar = "function addToolbar" + src.split("function addToolbar", 1)[1].split("function addSystem", 1)[0]
    script = f"""
{fmt}
{usage}
{toolbar}
const IC={{copy:'',read:''}}, _pandoMsgActions=[];
const document={{createElement:tag=>({{tag,children:[],appendChild(child){{this.children.push(child)}}}})}};
function render(meta) {{
  const row={{children:[],appendChild(child){{this.children.push(child)}}}};
  addToolbar(row,'历史回复',meta);
  return row.children[0].children.filter(child=>child.className==='a-usage').map(child=>child.textContent);
}}
console.log(JSON.stringify([
  render({{cost_usd:12.3456,usage:{{input_tokens:0,cache_read_input_tokens:92700,cache_creation_input_tokens:0,output_tokens:131,cache_hit_pct:100}}}}),
  render({{usage:{{input_tokens:20,cache_read:80,cache_create:0,output_tokens:2}}}}),
  render({{usage:{{input_tokens:10,output_tokens:3}}}}),
  render({{cost_usd:12.3456,usage:{{}}}}),
  render({{cost_usd:12.3456}}),
  render({{usage:{{input_tokens:0,output_tokens:0}}}})
]));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        ["输入 92.7K · 输出 131 · 缓存 100%"],
        ["输入 100 · 输出 2 · 缓存 80%"],
        ["输入 10 · 输出 3"],
        [],
        [],
        ["输入 0 · 输出 0"],
    ]


def test_no_dead_dollar_footer_helper():
    src = INDEX.read_text(encoding="utf-8")
    assert "function addUsage" not in src
    assert "'$'+" not in src
