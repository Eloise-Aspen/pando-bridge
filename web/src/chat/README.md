# 轮次门卫对照

当前 `web/` 只放状态逻辑和工程骨架，生产仍由 `static/index.html` 伺服。下表供两份实现核账。

| spec 规则 | 旧前端 `static/index.html` | 新 reducer `reducer.ts` |
|---|---|---|
| B.6 活跃轮、最大 seq、最近 20 个已结束轮 | `activeTurnId`、`lastTurnSeq`、`endedTurnIds` | `ChatState.activeTurnId`、`lastSeq`、`endedTurnIds` |
| B.7 已结束轮与重复 seq 丢弃 | `acceptTurnFrame()` | `acceptFrame()` |
| B.8 `turn_start` 设轮；`inflight` 仅不同轮清本地 | `onmessage` 的 `turn_start`、`inflight` 分支 | `chatReducer` 的 `turn_start`、`inflight` 分支 |
| B.9 `error` 清 pending、计时与轮次，仍独立显示 | `onmessage` 的 `error` 分支 | `error` 分支产出 `systems`，外壳负责计时器 |
| B.10 `session_switched` 立即采纳，`status`/`session_expired` 无操作 | 三个显式 `case` | 三个显式 `case` |
| B.11 无 `turn_id` 的旧帧继续处理 | `acceptTurnFrame()` 首行 | `acceptFrame()` 首行 |
| A.4 只认最新 `check_id` | `_latestCheckId` 与 `inflight`/`no_inflight` 分支 | `latestCheckId` 与 `inflight`/`no_inflight` 分支 |

`reducer.test.ts` 将盘点报告的 WS 11 条、LS 8 条、FG 7 条、RC 11 条逐条命名并标类别。LS 用例只测刷新后的初始状态，实际 localStorage 读写属于后续外壳。D-05 已修，reducer 的 RC-11 按修复后行为丢弃旧会话历史；D-04 已修，RC-07 以 `historyReloadSessionId` 告知外壳补拉历史。

停止兜底视为轮次结束，迟到 result 丢弃；完整内容以刷新或重进会话后的历史为准。
