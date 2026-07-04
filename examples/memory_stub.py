"""极简参考记忆服务 —— 演示 HttpMemoryProvider 的 4 端点契约。

纯内存 dict 存储，无向量、无 LLM，仅供理解契约 + 端到端冒烟测试。
真实部署请换成你自己的记忆引擎（实现同样 4 个端点即可，任意语言/技术栈）。

启动：
    python example/memory_stub.py            # 默认 127.0.0.1:8780
然后让 bridge 指向它：
    MEMORY_SERVICE_URL=http://127.0.0.1:8780  python server.py
"""

import os

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="memory-stub")

# 进程内存储：一堆记忆正文。重启即清空。
_MEMORIES: list[str] = []


class RecallIn(BaseModel):
    query: str


class MessagesIn(BaseModel):
    messages: list[dict] = []
    force: bool = False


class ArchiveIn(BaseModel):
    raw: str


@app.post("/session_context")
def session_context() -> dict:
    """L0/L1：把已存的记忆全列出来（真实实现应做筛选/摘要）。"""
    if not _MEMORIES:
        return {"context": ""}
    body = "\n".join(f"- {m}" for m in _MEMORIES[-20:])
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
    hits = [m for m in _MEMORIES if any(k and k in m for k in keys)]
    if not hits:
        return {"context": ""}
    body = "\n".join(f"- {m}" for m in hits[:3])
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
    import json as _json

    text = inp.raw.strip()
    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        return {"stored": 0}
    if not data.get("worthy"):
        return {"stored": 0}
    content = (data.get("content") or "").strip()
    if len(content) < 4:
        return {"stored": 0}
    _MEMORIES.append(content)
    return {"stored": 1, "total": len(_MEMORIES)}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "memories": len(_MEMORIES)}


if __name__ == "__main__":
    port = int(os.environ.get("MEMORY_STUB_PORT", "8780"))
    uvicorn.run(app, host="127.0.0.1", port=port)
