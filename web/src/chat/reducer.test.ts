import { describe, expect, it } from 'vitest'
import { chatReducer, initialChatState, type ChatEvent, type ChatState, type Frame } from './reducer'

const f = (frame: Frame): ChatEvent => ({ type: 'frame', frame })
const run = (events: ChatEvent[], start: ChatState = initialChatState) => events.reduce(chatReducer, start)
const send: ChatEvent = { type: 'send', text: '问' }
const started = (id = 'T'): ChatEvent => f({ type: 'turn_start', turn_id: id })
const tagged = (type: string, seq: number, extra: Partial<Frame> = {}): ChatEvent =>
  f({ type, turn_id: 'T', seq, ...extra })
type Case = { name: string; events: ChatEvent[]; start?: ChatState; check: (state: ChatState) => void }
const base = (extra: Partial<ChatState>): ChatState => ({ ...initialChatState, ...extra })

// 盘点报告的 37 个编号逐一列出。标签区分沿用现状、修正项与第 4 期遗留。
const cases: Case[] = [
  { name: 'WS-01 复刻现状 hello 同步记忆能力', events: [f({ type: 'hello', has_memory: true })], check: s => expect(s.hasMemory).toBe(true) },
  { name: 'WS-02 复刻现状 session 改会话指针', events: [f({ type: 'session', session_id: 'S' })], check: s => expect(s.sessionId).toBe('S') },
  { name: 'WS-03 复刻现状 memory_recall 等待结果', events: [f({ type: 'memory_recall', context: '片段' })], check: s => expect(s.pendingRecall).toBe('片段') },
  { name: 'WS-04 复刻现状 thinking 累加', events: [started(), tagged('thinking', 1, { text: 'a' }), tagged('thinking', 2, { text: 'b' })], check: s => expect(s.thinking).toBe('ab') },
  { name: 'WS-05 复刻现状 tool_use 计数', events: [started(), tagged('tool_use', 1, { tool: 'Read' })], check: s => expect(s.tools).toEqual(['Read']) },
  { name: 'WS-06 复刻现状 text 增量', events: [started(), tagged('text', 1, { text: '甲' }), tagged('text', 2, { text: '乙' })], check: s => expect(s.text).toBe('甲乙') },
  { name: 'WS-07 复刻现状 result 权威全文收尾', events: [send, started(), tagged('text', 1, { text: 'a' }), tagged('result', 2, { text: 'abc' })], check: s => { expect(s.messages.at(-1)).toBe('assistant:abc'); expect(s.busy).toBe(false) } },
  { name: 'WS-08 修正 D-02 error 完整收尾', events: [send, started(), tagged('memory_recall', 1, { context: 'R' }), tagged('error', 2, { message: '失败' })], check: s => { expect(s.systems).toEqual(['⚠ 失败']); expect(s.pendingRecall).toBeNull(); expect(s.busy).toBe(false) } },
  { name: 'WS-09 修正 D-03 inflight 同轮保留', events: [started(), tagged('text', 1, { text: '已画' }), { type: 'reconnect_open', checkId: 1 }, f({ type: 'inflight', turn_id: 'T', check_id: 1 })], check: s => expect(s.text).toBe('已画') },
  { name: 'WS-10 修正 RC-08 no_inflight 只认当前对账', events: [send, f({ type: 'no_inflight', check_id: 1 })], check: s => expect(s.busy).toBe(true) },
  { name: 'WS-11 复刻现状 permission_request 入队', events: [started(), tagged('permission_request', 1, { request_id: 'P' })], check: s => expect(s.permissions).toEqual(['P']) },

  { name: 'LS-01 复刻现状 刷新恢复会话指针而非页面', events: [{ type: 'refresh', sessionId: 'S' }], check: s => { expect(s.sessionId).toBe('S'); expect(s.view).toBe('home') } },
  { name: 'LS-02 复刻现状 刷新保留界面偏好快照', events: [{ type: 'refresh', sessionId: null, preferences: { model: 'opus', nickname: 'N' } }], check: s => expect(s.preferences.nickname).toBe('N') },
  { name: 'LS-03 复刻现状 本机开关保留', events: [{ type: 'refresh', sessionId: null, preferences: { enterToSend: '0', autoMemory: '0', theme: 'smoke' } }], check: s => expect(s.preferences.autoMemory).toBe('0') },
  { name: 'LS-04 复刻现状 语音设置属于快照', events: [{ type: 'refresh', sessionId: null, preferences: { voiceSettings: '{"speed":1}' } }], check: s => expect(s.preferences.voiceSettings).toContain('speed') },
  { name: 'LS-05 复刻现状 刷新丢轮内存态', events: [{ type: 'refresh', sessionId: 'S' }], start: base({ busy: true, text: '半截', thinking: '想' }), check: s => { expect(s.busy).toBe(false); expect(s.text).toBe('') } },
  { name: 'LS-06 复刻现状 刷新丢草稿附件', events: [{ type: 'refresh', sessionId: null }], start: base({ draft: '稿', attachments: ['a'] }), check: s => { expect(s.draft).toBe(''); expect(s.attachments).toEqual([]) } },
  { name: 'LS-07 复刻现状 刷新丢 recall 权限路由', events: [{ type: 'refresh', sessionId: null }], start: base({ pendingRecall: 'R', permissions: ['P'], view: 'chat' }), check: s => { expect(s.pendingRecall).toBeNull(); expect(s.permissions).toEqual([]); expect(s.view).toBe('home') } },
  { name: 'LS-08 第 4 期修 D-07 刷新丢换窗进行态', events: [{ type: 'refresh', sessionId: 'S' }], start: base({ forgeMode: 'clean' }), check: s => expect(s.forgeMode).toBeNull() },

  { name: 'FG-01 复刻现状 基座新对话清屏', events: [{ type: 'new_chat' }], start: base({ sessionId: 'S', messages: ['assistant:x'] }), check: s => { expect(s.sessionId).toBeNull(); expect(s.messages).toEqual([]) } },
  { name: 'FG-02 复刻现状 插件覆写以 forge_start 表示', events: [{ type: 'forge_start', mode: 'clean' }], check: s => expect(s.forgeMode).toBe('clean') },
  { name: 'FG-03 复刻现状 不满足 forge 条件走原新对话', events: [{ type: 'new_chat' }], start: base({ forgeMode: null }), check: s => expect(s.forgeMode).toBeNull() },
  { name: 'FG-04 复刻现状 clean 等 forged 后清屏', events: [{ type: 'forge_start', mode: 'clean' }, f({ type: 'forged', clean: true })], start: base({ messages: ['assistant:old'] }), check: s => expect(s.messages).toEqual([]) },
  { name: 'FG-05 复刻现状 carryover 原地换 ID', events: [{ type: 'forge_start', mode: 'carryover' }, f({ type: 'forged', carryover: true, session_id: 'B' })], start: base({ sessionId: 'A', messages: ['assistant:old'] }), check: s => { expect(s.sessionId).toBe('B'); expect(s.messages).toEqual(['assistant:old']) } },
  { name: 'FG-06 复刻现状 自动 forged 失败清屏', events: [f({ type: 'forged', auto: true, carryover: false })], start: base({ messages: ['assistant:old'] }), check: s => expect(s.messages).toEqual([]) },
  { name: 'FG-07 复刻现状 旧 result 回顶由链尾纠偏', events: [started(), tagged('result', 1, { text: '旧', session_id: 'A' })], start: base({ sessionId: 'A', chainTail: 'B' }), check: s => expect(s.sessionId).toBe('B') },

  { name: 'RC-01 修正 D-01 result 后迟到 text 丢弃', events: [started(), tagged('result', 1, { text: '完成' }), tagged('text', 2, { text: '尾' })], check: s => expect(s.messages).toEqual(['assistant:完成']) },
  { name: 'RC-02 修正 D-01 重复 result 不重画', events: [started(), tagged('result', 1, { text: '完成' }), tagged('result', 2, { text: '完成' })], check: s => expect(s.messages).toEqual(['assistant:完成']) },
  { name: 'RC-03 修正 D-02 error 后迟到帧丢弃', events: [started(), tagged('error', 1, { message: '错' }), tagged('text', 2, { text: '尾' })], check: s => { expect(s.text).toBe(''); expect(s.systems).toHaveLength(1) } },
  { name: 'RC-04 修正 D-01 停止兜底后迟到结果丢弃', events: [started(), tagged('text', 1, { text: '半' }), { type: 'stop_timeout' }, tagged('result', 2, { text: '全文' })], check: s => expect(s.messages).toEqual([]) },
  { name: 'RC-05 修正 D-03 未刷新重连回放去重', events: [started(), tagged('tool_use', 1, { tool: 'Read' }), { type: 'reconnect_open', checkId: 2 }, f({ type: 'inflight', turn_id: 'T', check_id: 2 }), tagged('tool_use', 1, { tool: 'Read' })], check: s => expect(s.tools).toEqual(['Read']) },
  { name: 'RC-06 复刻现状 刷新后 inflight 回放重建', events: [{ type: 'refresh', sessionId: 'S' }, { type: 'reconnect_open', checkId: 3 }, f({ type: 'inflight', turn_id: 'T', check_id: 3 }), tagged('text', 1, { text: '重建' })], check: s => expect(s.text).toBe('重建') },
  { name: 'RC-07 复刻现状 离线完结 no_inflight 解忙', events: [{ type: 'reconnect_open', checkId: 4 }, f({ type: 'no_inflight', check_id: 4 })], start: base({ busy: true, text: '半' }), check: s => { expect(s.busy).toBe(false); expect(s.text).toBe('') } },
  { name: 'RC-08 修正 D-01 旧 no_inflight 不清新发送', events: [{ type: 'reconnect_open', checkId: 5 }, send, f({ type: 'no_inflight', check_id: 5 })], check: s => expect(s.busy).toBe(true) },
  { name: 'RC-09 复刻现状 forge pending 仍收普通帧', events: [{ type: 'forge_start', mode: 'carryover' }, started(), tagged('text', 1, { text: '输出' })], check: s => { expect(s.forgeMode).toBe('carryover'); expect(s.text).toBe('输出') } },
  { name: 'RC-10 复刻现状 forged 后旧 result 由链尾恢复', events: [started(), tagged('result', 1, { text: '尾', session_id: 'A' })], start: base({ sessionId: 'A', chainTail: 'B' }), check: s => expect(s.sessionId).toBe('B') },
  { name: 'RC-11 复刻现状 快速切会话仍可能混入旧历史', events: [{ type: 'switch_session', sessionId: 'A' }, { type: 'switch_session', sessionId: 'B' }, { type: 'history_loaded', sessionId: 'A', messages: ['旧'] }], check: s => { expect(s.sessionId).toBe('B'); expect(s.messages).toEqual(['history:旧']) } },
]

describe('聊天状态盘点 37 条', () => {
  it.each(cases)('$name', ({ events, start, check }) => check(run(events, start)))
})
