// 从生产 HTML 抽取轮次门卫及真实 WS 分发，使用无依赖的 DOM 替身验证边界。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'static', 'index.html'), 'utf8');
const helper = html.slice(html.indexOf('let _awaitingInflightCheck=false;'), html.indexOf('function connect(){'));
const connect = html.slice(html.indexOf('function connect(){'), html.indexOf('/* ===== permission ===== */'));
assert(helper && connect);

const seen = { systems: [], rendered: [], busy: null, session: null, reset: 0, clears: 0 };
const socket = { readyState: 1, send() {} };
const context = vm.createContext({
  console: { debug() {}, warn() {} }, location: { protocol: 'http:', host: 'localhost' },
  WebSocket: function () { return socket; }, ws: socket, setTimeout() {},
  currentSessionId: null, currentAssistantRow: null, assistantText: '',
  roundThinking: '', roundTools: [], busy: false, _pendingRecall: null,
  _pandoMsgRenderers: {}, _pandoEmit() {}, _hasMemory: false,
  _syncArchivePref() {}, $() { return null; }, setSessionId(id) { seen.session = id; },
  freezeLiveChip() {}, liveDur() { return 0; }, addSystem(text) { seen.systems.push(text); },
  resetRoundState() { seen.reset++; context.assistantText = ''; context.currentAssistantRow = null; context.roundTools = []; context.roundThinking = ''; },
  setBusy(value) { seen.busy = value; context.busy = value; }, ensureLiveChip() {},
  dropLiveChip() {}, addAssistant() { return {}; },
  renderAssistantBody(row, text) { seen.rendered.push(text); },
  assistantChipRow() { return { insertBefore() {}, firstChild: null }; },
  makeRecallChip() { return {}; }, addToolbar() {}, addStopped() {}, updateCtx() {},
  _clearStopFallback() { seen.clears++; }, reconcileSessionUpdates() {}, enqueuePermission() {},
  loadHistory() {}, view: 'chat', _pendingSessionUpdate: null,
});
vm.runInContext(helper + connect + 'connect();', context);
function frame(value) { socket.onmessage({ data: JSON.stringify(value) }); }
function check(code) { return vm.runInContext(code, context); }

// 正常结果收尾后，迟到 text/result 不得产生第二条回复；老式帧仍可进入旧逻辑。
frame({ type: 'turn_start', turn_id: 'A' });
frame({ type: 'text', turn_id: 'A', seq: 1, text: '好' });
frame({ type: 'result', turn_id: 'A', seq: 2, text: '好' });
const count = seen.rendered.length;
frame({ type: 'text', turn_id: 'A', seq: 3, text: '尾' });
frame({ type: 'result', turn_id: 'A', seq: 2, text: '好' });
assert.equal(seen.rendered.length, count);
frame({ type: 'text', text: '旧' });
assert.equal(seen.rendered.at(-1), '旧');

// 同轮重连保留本地文本，用原 seq 去重；新轮 inflight 则复位。
frame({ type: 'turn_start', turn_id: 'B' });
frame({ type: 'tool_use', turn_id: 'B', seq: 1, tool: 'Read' });
assert.equal(check('roundTools.length'), 1);
vm.runInContext('_awaitingInflightCheck=true; _latestCheckId=4;', context);
frame({ type: 'inflight', turn_id: 'B', check_id: 4 });
frame({ type: 'tool_use', turn_id: 'B', seq: 1, tool: 'Read' });
assert.equal(check('roundTools.length'), 1);
vm.runInContext('_awaitingInflightCheck=true; _latestCheckId=5;', context);
frame({ type: 'inflight', turn_id: 'C', check_id: 5 });
assert.equal(check('roundTools.length'), 0);

// 旧 check_id 不能清新轮；错误仍独立成行，并完整结束轮次。
vm.runInContext('_awaitingInflightCheck=true; _latestCheckId=6;', context);
frame({ type: 'no_inflight', check_id: 5 });
assert.equal(check('_awaitingInflightCheck'), true);
frame({ type: 'memory_recall', turn_id: 'C', seq: 1, context: '待显示' });
assert.equal(check('_pendingRecall'), '待显示');
frame({ type: 'error', turn_id: 'C', seq: 2, message: '失败' });
assert.equal(seen.systems.at(-1), '⚠ 失败');
assert.equal(seen.busy, false);
assert.equal(check('_pendingRecall'), null);
assert.equal(seen.clears, 2); // result 与 error 各清一次停止兜底
frame({ type: 'text', turn_id: 'C', seq: 3, text: '迟到' });
assert.equal(seen.rendered.at(-1), '旧');
frame({ type: 'session_switched', session_id: 'new-session' });
assert.equal(seen.session, 'new-session');
console.log('frontend turn gate: 7 assertions groups passed');
