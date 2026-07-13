"""极简参考记忆服务 —— 演示 HttpMemoryProvider 的 4 端点契约 + 可选管理端点。

**定位**：契约参考实现，不是产品级记忆引擎。存储是一个 JSON 文件 + 朴素子串召回，
无向量、无语义检索、无 LLM。真实部署请换成你自己的记忆引擎（实现同样端点即可，
任意语言/技术栈）。

**持久化**（feat-memory-onboarding 关键裁决②）：记忆落一个 JSON 文件到 `--data`
指定的目录（默认 `./stub_data`），零新依赖。**重启不丢**——这是发布帖「基础记忆库」
承诺成立的底线；召回仍是关键词/朴素匹配，如实注明，别指望它替代真正的向量检索。

启动：
    python examples/memory_stub.py                    # 默认 127.0.0.1:8780，数据落 ./stub_data
    python examples/memory_stub.py --data ./mydata    # 自定义数据目录
    python examples/memory_stub.py --port 8790         # 自定义端口
然后让 bridge 指向它（配了 URL 就会自动启用记忆，见 README）：
    MEMORY_SERVICE_URL=http://127.0.0.1:8780  python run.py
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="memory-stub")

# 进程内存储：每条记忆一个 dict {"id", "content", "created_at", (可选)"klass"}。
# 用 dict 而非裸字符串，是为了给管理面(list/delete)提供稳定的 id——按下标删会因
# 删除后下标平移而错删，稳定 id 才能让前端删对条目。
# 这份列表是「热副本」，每次变更后 _save() 落盘；进程启动时 _load() 从盘读回。
_MEMORIES: list[dict] = []
_NEXT_ID = 1

# 落盘路径。None = 未配置持久化（如 pytest 直接 import app 时），此时 _save()/_load()
# 均为 no-op，测试保持内存态、互不干扰。_configure() 在 __main__ 里按 --data 设定它。
_STORE_PATH: Path | None = None


def _configure(data_dir: str | os.PathLike) -> None:
    """设定数据目录并从盘加载已有记忆（进程启动时调用一次）。"""
    global _STORE_PATH
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    _STORE_PATH = d / "memories.json"
    _load()


def _load() -> None:
    """从 JSON 文件读回记忆与自增游标；文件不存在则保持空态。"""
    global _MEMORIES, _NEXT_ID
    if not _STORE_PATH or not _STORE_PATH.exists():
        return
    data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    _MEMORIES = data.get("memories", [])
    # 游标取「已存最大 id + 1」，即便文件里 next_id 缺失也不会分配到重复 id。
    _NEXT_ID = max((m["id"] for m in _MEMORIES), default=0) + 1


def _save() -> None:
    """原子落盘（先写 .tmp 再 replace），避免写一半崩了留下半个坏文件。"""
    if not _STORE_PATH:
        return
    payload = json.dumps({"memories": _MEMORIES, "next_id": _NEXT_ID}, ensure_ascii=False, indent=2)
    tmp = _STORE_PATH.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(_STORE_PATH)


def _add_memory(content: str, klass: str | None = None, origin_date: str | None = None) -> dict:
    """追加一条记忆并分配稳定自增 id，随后落盘。

    origin_date 若给出则作为 created_at（迁入历史记忆时保留原始时间）；klass 若给出则附带。
    """
    global _NEXT_ID
    mem = {
        "id": _NEXT_ID,
        "content": content,
        "created_at": origin_date or datetime.now(timezone.utc).isoformat(),
    }
    if klass:
        mem["klass"] = klass
    _NEXT_ID += 1
    _MEMORIES.append(mem)
    _save()
    return mem


class RecallIn(BaseModel):
    query: str


class MessagesIn(BaseModel):
    messages: list[dict] = []
    force: bool = False


class ArchiveIn(BaseModel):
    raw: str


class ImportItem(BaseModel):
    """迁入一条记忆。content 必填；klass/origin_date 可选（真实引擎可据此分层/排序）。"""
    content: str
    klass: str | None = None
    origin_date: str | None = None


@app.post("/session_context")
def session_context() -> dict:
    """L0/L1：把已存的记忆全列出来（真实实现应做筛选/摘要）。"""
    if not _MEMORIES:
        return {"context": ""}
    body = "\n".join(f"- {m['content']}" for m in _MEMORIES[-20:])
    return {"context": f"【已知记忆】\n{body}"}


@app.post("/recall")
def recall(inp: RecallIn) -> dict:
    """L2：朴素子串匹配（真实实现请用向量检索）。

    匹配 = 空格分词 token 命中，或任意 2 字窗口命中（让中文这种无空格的也能召回）。
    """
    q = inp.query.strip()
    if not q:
        return {"context": ""}
    keys = set(q.split()) | {q[i:i + 2] for i in range(len(q) - 1)}
    hits = [m for m in _MEMORIES if any(k and k in m["content"] for k in keys)]
    if not hits:
        return {"context": ""}
    body = "\n".join(f"- {m['content']}" for m in hits[:3])
    return {"context": f"【相关记忆】\n{body}"}


@app.post("/archive_prompt")
def archive_prompt(inp: MessagesIn) -> dict:
    """对话够长就给一段通用存档指令，让 bridge 在 live 会话里跑。
    force=True（换窗场景）跳过"对话够不够长"的门槛。"""
    convo_len = sum(len(m.get("content", "")) for m in inp.messages)
    if not inp.force and convo_len < 200:
        return {"prompt": None}
    return {"prompt": (
        "[系统指令：记忆存档]\n\n"
        "回顾刚才这段对话，如果有值得长期记住的事实/决定/偏好，写一段 80-150 字的记忆，"
        "只返回 JSON：{\"worthy\": true, \"content\": \"记忆正文\"}；"
        "没有则返回 {\"worthy\": false}。"
    )}


@app.post("/archive")
def archive(inp: ArchiveIn) -> dict:
    """v2：接收模型针对 archive_prompt 写出的原始 JSON 文本，自行解析 worthy/content 后落库
    （真实实现请做更健壮的 JSON 提取，这里只演示契约，不做容错兜底）。"""
    text = inp.raw.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"stored": 0}
    if not data.get("worthy"):
        return {"stored": 0}
    content = (data.get("content") or "").strip()
    if len(content) < 4:
        return {"stored": 0}
    _add_memory(content)
    return {"stored": 1, "total": len(_MEMORIES)}


# ---------------------------------------------------------------------------
# 管理面软约定端点（provider-contract-v2.md 第 5/6 节）
#
# 这些端点**不属于** MemoryProvider Protocol 的 4 端点契约，而是旁路管理链路：
# bridge 通过 MemoryPlugin 的 /memory-admin/* 透传代理转发到这里（前端调
# /memory-admin/memory/stats → 代理去掉前缀 → 命中本服务 /memory/stats）。
# 路径按生产 memory_service 的 /memory/* 形状对齐;真实实现换成自己的存储即可。
# ---------------------------------------------------------------------------


@app.post("/memory/import")
def memory_import(items: list[ImportItem]) -> dict:
    """批量迁入历史记忆（feat-memory-onboarding 关键裁决③）。

    请求体 = `[{content, klass?, origin_date?}, ...]`；逐条写入，content 太短(<4)跳过。
    返回 `{"stored": 本次成功条数, "skipped": 跳过条数, "total": 库内总条数}`。

    **契约定位**：可选管理端点，provider 可不实现、前端不依赖——它只服务「命令行把
    已有记忆搬进来」这一次性迁入场景（README「把已有记忆搬进来」小节给了可照抄的 curl）。
    """
    stored = 0
    skipped = 0
    for it in items:
        content = (it.content or "").strip()
        if len(content) < 4:
            skipped += 1
            continue
        _add_memory(content, klass=it.klass, origin_date=it.origin_date)
        stored += 1
    return {"stored": stored, "skipped": skipped, "total": len(_MEMORIES)}


@app.get("/memory/stats")
def memory_stats() -> dict:
    """统计头。total = 记忆条数（不含 klass='log' 的时间轴日志）;log_total = 日志条数。
    注:本响应是**可加字段**契约——新增键(如 log_total)只增不减,消费方/测试须按需取键,
    勿用全字典相等断言。"""
    return {"count": len(_MEMORIES), "total_chars": sum(len(m["content"]) for m in _MEMORIES),
            "log_total": 0}


@app.get("/memory/list")
def memory_list(limit: int = 50, offset: int = 0) -> dict:
    """按新到旧列出记忆(分页)。total 为全量条数,供前端显示总数/翻页。"""
    ordered = list(reversed(_MEMORIES))
    page = ordered[offset:offset + limit]
    return {"items": page, "total": len(_MEMORIES)}


@app.get("/memory/search")
def memory_search(q: str = "") -> dict:
    """子串搜索(真实实现请用向量检索)。q 为空返回空列表。"""
    q = q.strip()
    if not q:
        return {"items": [], "total": 0}
    hits = [m for m in reversed(_MEMORIES) if q in m["content"]]
    return {"items": hits, "total": len(hits)}


@app.delete("/memory/{mem_id}")
def memory_delete(mem_id: int) -> dict:
    """按稳定 id 删除单条;不存在返回 404。删除后落盘。"""
    for i, m in enumerate(_MEMORIES):
        if m["id"] == mem_id:
            _MEMORIES.pop(i)
            _save()
            return {"deleted": 1, "id": mem_id}
    raise HTTPException(status_code=404, detail="memory not found")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "memories": len(_MEMORIES)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pando 参考记忆服务（文件持久化 stub）")
    parser.add_argument("--data", default="./stub_data",
                        help="记忆 JSON 文件所在目录（默认 ./stub_data，重启保留）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MEMORY_STUB_PORT", "8780")),
                        help="监听端口（默认 8780，或环境变量 MEMORY_STUB_PORT）")
    args = parser.parse_args()

    _configure(args.data)
    print(f"  memory-stub data → {Path(args.data).resolve() / 'memories.json'} "
          f"({len(_MEMORIES)} memories loaded)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)
