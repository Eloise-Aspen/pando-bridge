"""Pando 启动脚本示例 —— 复制成 run.py 改两行路径即可跑。

    cp run.example.py run.py      # Linux/macOS
    copy run.example.py run.py    # Windows
    python run.py

只有 CLAUDE_CWD 必须改（指向一个已存在的目录）。其余按需取消注释。
每个配置项的完整说明见 .env.example 与 README。
"""

import os

import uvicorn

from pando import create_app

app = create_app({
    # ── 必需 ────────────────────────────────────────────────────────────
    "CLAUDE_EXE": "claude",                  # Claude Code CLI：PATH 里的命令名或绝对路径
    "CLAUDE_CWD": "/path/to/your/project",   # 必须已存在——你想让 Claude 工作的目录
    #   Windows 用正斜杠写："C:/Users/you/project"（反斜杠是 Python 转义符）
    "DATA_DIR": "./data",                    # chat.db 存这里（默认也是 ./data，可省略）

    # ── 记忆（可选，可插拔）─────────────────────────────────────────────
    # 取消下一行注释即接上记忆服务：设了 URL，MemoryPlugin 会自动挂载，
    # 无需再手动往 PLUGINS 里写它（README「记忆契约」有详解）。
    # 先起参考实现：python examples/memory_stub.py（默认 127.0.0.1:8780，重启不丢）。
    # "MEMORY_SERVICE_URL": "http://127.0.0.1:8780",

    # ── 插件（可选，高级用法）───────────────────────────────────────────
    # 声明式插件列表：一串 import 路径，经 importlib 加载（见 README「插件钩子 API」）。
    # 记忆插件在设了 MEMORY_SERVICE_URL 时会自动加入，通常不必在此重复声明；
    # 这里留作你挂自己插件的位置。
    # "PLUGINS": [
    #     "your_package.your_plugin.YourPlugin",
    # ],
})

if __name__ == "__main__":
    # 0.0.0.0 绑定所有网卡，好让局域网设备（比如你的手机）能连上——
    # 配合 README「手机接入」里的安全提示一起看（Pando 不带认证）。
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("BRIDGE_PORT", 8765)))
