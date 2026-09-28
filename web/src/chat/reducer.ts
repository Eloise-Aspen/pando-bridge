export type Frame = {
  type: string
  turn_id?: string
  seq?: number
  check_id?: number
  session_id?: string | null
  source_session?: string
  text?: string
  thinking?: string
  tool?: string
  context?: string
  message?: string
  detail?: string
  stopped?: boolean
  clean?: boolean
  carryover?: boolean
  auto?: boolean
  superseded?: boolean
  request_id?: string
  has_memory?: boolean
}

export type ChatEvent =
  | { type: 'frame'; frame: Frame }
  | { type: 'send'; text: string }
  | { type: 'stop' }
  | { type: 'stop_timeout' }
  | { type: 'switch_session'; sessionId: string | null }
  | { type: 'new_chat' }
  | { type: 'reconnect_open'; checkId: number }
  | { type: 'refresh'; sessionId: string | null; preferences?: Record<string, string> }
  | { type: 'forge_start'; mode: 'clean' | 'carryover' }
  | { type: 'forge_timeout' }
  | { type: 'history_loaded'; sessionId: string; messages: string[] }
  | { type: 'route'; view: string }
  | { type: 'draft'; text: string }
  | { type: 'attachment'; name: string }

export type ChatState = {
  sessionId: string | null
  view: string
  busy: boolean
  activeTurnId: string | null
  lastSeq: number
  endedTurnIds: string[]
  text: string
  thinking: string
  tools: string[]
  messages: string[]
  systems: string[]
  pendingRecall: string | null
  permissions: string[]
  stopped: boolean
  latestCheckId: number | null
  awaitingCheck: boolean
  forgeMode: 'clean' | 'carryover' | null
  chainTail: string | null
  preferences: Record<string, string>
  draft: string
  attachments: string[]
  needsRefresh: boolean
  hasMemory: boolean
}

export const initialChatState: ChatState = {
  sessionId: null, view: 'home', busy: false, activeTurnId: null, lastSeq: 0,
  endedTurnIds: [], text: '', thinking: '', tools: [], messages: [], systems: [],
  pendingRecall: null, permissions: [], stopped: false, latestCheckId: null,
  awaitingCheck: false, forgeMode: null, chainTail: null, preferences: {},
  draft: '', attachments: [], needsRefresh: false, hasMemory: false,
}

function resetRound(state: ChatState): ChatState {
  return { ...state, text: '', thinking: '', tools: [], pendingRecall: null, stopped: false }
}

function finishTurn(state: ChatState, id?: string): ChatState {
  if (!id) return state
  const ended = state.endedTurnIds.includes(id) ? state.endedTurnIds : [...state.endedTurnIds, id].slice(-20)
  return { ...state, endedTurnIds: ended,
    activeTurnId: state.activeTurnId === id ? null : state.activeTurnId,
    lastSeq: state.activeTurnId === id ? 0 : state.lastSeq }
}

function acceptFrame(state: ChatState, frame: Frame): ChatState | null {
  if (!frame.turn_id) return state // 服务端升级期间兼容旧帧
  if (state.endedTurnIds.includes(frame.turn_id)) return null
  if (state.activeTurnId && state.activeTurnId !== frame.turn_id) return null
  if (frame.seq !== undefined && frame.seq <= state.lastSeq) return null
  return { ...state, activeTurnId: frame.turn_id,
    lastSeq: frame.seq ?? state.lastSeq }
}

/** 单一纯入口：每个事件都只从输入与旧 state 计算新 state。 */
export function chatReducer(state: ChatState, event: ChatEvent): ChatState {
  if (event.type === 'send') return { ...resetRound(state), busy: true, stopped: false,
    awaitingCheck: false, latestCheckId: null, draft: '',
    messages: [...state.messages, `user:${event.text}`] }
  if (event.type === 'stop') return { ...state, stopped: true }
  if (event.type === 'stop_timeout') return finishTurn({ ...resetRound(state), busy: false }, state.activeTurnId ?? undefined)
  if (event.type === 'switch_session') return { ...resetRound(state), sessionId: event.sessionId,
    view: 'chat', busy: false, messages: [], activeTurnId: null, lastSeq: 0 }
  if (event.type === 'new_chat') return { ...initialChatState, preferences: state.preferences }
  if (event.type === 'reconnect_open') return { ...state, latestCheckId: event.checkId, awaitingCheck: true }
  if (event.type === 'refresh') return { ...initialChatState, sessionId: event.sessionId,
    preferences: event.preferences ?? state.preferences }
  if (event.type === 'forge_start') return { ...state, forgeMode: event.mode }
  if (event.type === 'forge_timeout') return state.forgeMode === 'clean'
    ? { ...initialChatState, preferences: state.preferences }
    : { ...state, forgeMode: null }
  if (event.type === 'history_loaded') return { ...state,
    messages: [...state.messages, ...event.messages.map(value => `history:${value}`)] }
  if (event.type === 'route') return { ...state, view: event.view }
  if (event.type === 'draft') return { ...state, draft: event.text }
  if (event.type === 'attachment') return { ...state, attachments: [...state.attachments, event.name] }

  const frame = event.frame
  if (frame.type === 'turn_start') {
    if (!frame.turn_id || state.endedTurnIds.includes(frame.turn_id)) return state
    return { ...state, activeTurnId: frame.turn_id, lastSeq: 0 }
  }
  if (frame.type === 'inflight' || frame.type === 'no_inflight') {
    if (!state.awaitingCheck || (frame.check_id !== undefined && frame.check_id !== state.latestCheckId)) return state
    const base = { ...state, awaitingCheck: false, latestCheckId: null }
    if (frame.type === 'no_inflight') return finishTurn({ ...resetRound(base), busy: false }, state.activeTurnId ?? undefined)
    if (frame.turn_id && frame.turn_id !== state.activeTurnId) {
      return { ...resetRound(base), busy: true, activeTurnId: frame.turn_id, lastSeq: 0 }
    }
    return { ...base, busy: true }
  }
  const next = acceptFrame(state, frame)
  if (!next) return state
  switch (frame.type) {
    case 'hello': return { ...next, hasMemory: !!frame.has_memory }
    case 'session':
    case 'session_switched': return { ...next, sessionId: frame.session_id ?? null }
    case 'session_expired':
    case 'status': return next
    case 'session_updated': return { ...next, needsRefresh: true }
    case 'memory_recall': return frame.context && !frame.context.startsWith('【context injected】')
      ? { ...next, pendingRecall: frame.context } : next
    case 'thinking': return { ...next, thinking: next.thinking + (frame.text ?? '') }
    case 'tool_use': return { ...next, tools: [...next.tools, frame.tool ?? ''] }
    case 'text': return { ...next, text: next.text + (frame.text ?? '') }
    case 'result': {
      const text = frame.text || next.text
      let done = { ...resetRound(next), busy: false, messages: text
        ? [...next.messages, `assistant:${text}`] : next.messages,
      sessionId: frame.session_id ?? next.sessionId, stopped: !!frame.stopped }
      if (next.chainTail && frame.session_id === next.sessionId) done = { ...done, sessionId: next.chainTail }
      return finishTurn(done, frame.turn_id)
    }
    case 'error': return finishTurn({ ...resetRound(next), busy: false,
      systems: [...next.systems, `⚠ ${frame.message ?? 'error'}${frame.detail ? `\n${frame.detail}` : ''}`] }, frame.turn_id)
    case 'permission_request': return { ...next, permissions: [...next.permissions, frame.request_id ?? ''] }
    case 'forged':
      if (frame.auto || next.forgeMode) {
        if (frame.clean || (!frame.carryover && frame.auto)) return { ...initialChatState, preferences: next.preferences }
        if (frame.session_id) return { ...next, sessionId: frame.session_id,
          chainTail: frame.session_id, forgeMode: null }
      }
      return next
    default: return next
  }
}
